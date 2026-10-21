# -*- coding: utf-8 -*-
"""把蜡染图案切成一个个纹样单元。

这批图有两种形态：整幅一个纹样、以及一张拼版里排着十几个小纹样。
要拿去做生成，需要的是**单个纹样**，所以先切。

切法（针对白线深底的蜡染图案）
------------------------------
1. 二值化：亮度高的就是白线
2. **膨胀**：一个纹样由许多断开的线条组成，不膨胀的话每根线都是一个连通块，
   切出来全是碎条。膨胀半径按图幅取（约 1/40），让同一纹样的线粘成一团
3. 连通域标注，按面积筛：太小的当噪点，太大的当整幅背景
4. 按每个连通块的外框回原图裁剪（带 6% 边距）

跳过的情形：整张只有一个巨大连通块时，说明这张本身就是单幅完整纹样，
不切——直接整幅当作一个单元输出。

用法：
    python tools/segment_motifs.py --selftest
    python tools/segment_motifs.py --src data/crawl_miao --limit 8 --sheet
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from patterns_to_bw import _otsu

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = 700           # 分割在这个边长下做
DILATE_FRAC = 1 / 75.0   # 膨胀半径。太大会把拼版里相邻的小纹样连成一整块
MAX_ASPECT = 4.0         # 长宽比超过这个的块判为水印条/边框
SKIP_NAMES = ("抽看", "清单", "说明", "预览")  # 这些不是素材，是交付物自带的图
MIN_AREA_FRAC = 0.004   # 小于画面 0.4% 当噪点
MAX_AREA_FRAC = 0.92    # 大于 92% 当整幅
PAD = 0.06


def _binarize(a):
    """白线→True。用大津法，不用分位数。

    原来用 88 分位当阈值：图案一旦占到画面 12% 以上，分位数就落到白线上，
    阈值被抬到白线亮度，结果一个前景像素都分不出来（自检里"整幅纹样"
    那条就是这么挂的）。大津法按类间方差找分界，不依赖图案占比。
    """
    L = 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]
    thr = _otsu(L.ravel())
    thr = max(thr, 60.0)
    return L > thr


def segment(path, min_area_frac=MIN_AREA_FRAC, max_area_frac=MAX_AREA_FRAC):
    """返回 [(crop_img, bbox_in_original), ...]"""
    im = Image.open(path).convert("RGB")
    W, H = im.size
    s = WORK / float(max(W, H)) if max(W, H) > WORK else 1.0
    small = im.resize((max(16, int(W * s)), max(16, int(H * s))), Image.BILINEAR)
    arr = np.asarray(small, dtype=np.float32)

    fg = _binarize(arr)
    r = max(2, int(round(max(small.size) * DILATE_FRAC)))
    fg = ndimage.binary_dilation(fg, iterations=r)
    fg = ndimage.binary_closing(fg, iterations=max(2, r // 2))

    # 前景太少 = 这张没有可切的图案，整幅作为一个单元返回，不要交白卷
    if fg.mean() < 0.005:
        return [(_full(im), (0, 0, W, H), 1.0)]

    # 先试网格拼版：格子多、分离干净时优先按格子切
    cells = _grid_cells(fg)
    if cells:
        out = []
        for x0, y0, x1, y1 in cells:
            ox0, oy0 = int(x0 / s), int(y0 / s)
            ox1, oy1 = int(np.ceil(x1 / s)), int(np.ceil(y1 / s))
            crop = im.crop((ox0, oy0, ox1, oy1))
            cw, ch = crop.size
            # 网格里也可能混进版权条那一行/列，同样的筛法要用一遍
            if max(cw, ch) / float(max(1, min(cw, ch))) > MAX_ASPECT:
                continue
            if ch < H * 0.12 and cw < W * 0.5:
                continue
            g = np.asarray(crop.convert("L"), dtype=np.float32)
            if g.std() < 16:          # 空格子 / 版权条
                continue
            out.append((crop, (ox0, oy0, ox1, oy1),
                        float(fg[y0:y1, x0:x1].mean())))
        if len(out) >= 4:
            return out

    lab, n = ndimage.label(fg)
    if n == 0:
        return [(_full(im), (0, 0, W, H), 1.0)]
    total = float(fg.shape[0] * fg.shape[1])
    boxes = []
    for sl in ndimage.find_objects(lab):
        if sl is None:
            continue
        h = sl[0].stop - sl[0].start
        w = sl[1].stop - sl[1].start
        area = (lab[sl] > 0).sum()
        frac = area / total
        if frac < min_area_frac or frac > max_area_frac:
            continue
        boxes.append((sl[1].start, sl[0].start, sl[1].stop, sl[0].stop, frac))

    # 一个都没切出来
    if not boxes:
        # 横幅/竖条：整张就是一条版权信息，不该当纹样输出
        ar = W / float(H)
        if ar > 2.5 or ar < 0.4:
            return []
        return [(_full(im), (0, 0, W, H), 1.0)]
    if len(boxes) == 1 and boxes[0][4] > 0.5:
        return [(_full(im), (0, 0, W, H), boxes[0][4])]

    boxes.sort(key=lambda b: (b[1], b[0]))
    out = []
    for x0, y0, x1, y1, frac in boxes:
        ox0, oy0 = int(x0 / s), int(y0 / s)
        ox1, oy1 = int(np.ceil(x1 / s)), int(np.ceil(y1 / s))
        px = max(4, int((ox1 - ox0) * PAD))
        py = max(4, int((oy1 - oy0) * PAD))
        bx0, by0 = max(0, ox0 - px), max(0, oy0 - py)
        bx1, by1 = min(W, ox1 + px), min(H, oy1 + py)
        if bx1 - bx0 < 24 or by1 - by0 < 24:
            continue
        # 水印条：又长又扁的一条，通常是「昵图网 www.nipic.com」那种版权条，
        # 它不是纹样，切出来只会污染素材库
        w_, h_ = bx1 - bx0, by1 - by0
        if max(w_, h_) / float(min(w_, h_)) > MAX_ASPECT:
            continue
        # 版权条：贴着画面上下缘、又宽又薄的一条（昵图网那种）
        # 横贯画面的薄条基本都是版权条/说明文字，不要求它在边缘——
        # 昵图网的条经常压在图中间偏下
        if h_ < H * 0.22 and w_ > W * 0.50:
            continue
        if w_ < W * 0.14 and h_ > H * 0.55:
            continue
        # 对比度极低的块基本是版权条或纯色边框：米字纹样不会这么平
        sub = np.asarray(im.crop((bx0, by0, bx1, by1)).convert("L"),
                         dtype=np.float32)
        if float(sub.std()) < 16:
            continue
        # 对比度极低的块基本是版权条或纯色边框：米字纹样不会这么平
        sub = np.asarray(im.crop((bx0, by0, bx1, by1)).convert("L"),
                         dtype=np.float32)
        if float(sub.std()) < 16:
            continue
        out.append((im.crop((bx0, by0, bx1, by1)), (bx0, by0, bx1, by1), frac))
    return out or [(_full(im), (0, 0, W, H), 1.0)]


def _grid_cells(fg, min_gap_frac=0.008, max_cells=12):
    """用行列投影找空隙，把拼版切成格子。

    连通域切不开网格拼版：格子之间有边框线，整张会连成一个大块。
    但格与格之间的**空隙**在投影上是明显的低谷，按低谷切就行。
    返回 [(x0, y0, x1, y1), ...]（工作坐标系）或 None。
    """
    h, w = fg.shape
    cols = fg.sum(axis=0) / float(h)
    rows = fg.sum(axis=1) / float(w)
    min_gap = max(2, int(round(min(h, w) * min_gap_frac)))

    def cuts(profile):
        thr = profile.max() * 0.16
        low = profile < thr
        out, start = [], None
        for i, v in enumerate(low):
            if v and start is None:
                start = i
            elif not v and start is not None:
                if i - start >= min_gap:
                    out.append((start + i) // 2)
                start = None
        if start is not None and len(low) - start >= min_gap:
            out.append((start + len(low)) // 2)
        return out

    xs, ys = cuts(cols), cuts(rows)
    if len(xs) < 1 or len(ys) < 1:
        return None
    if (len(xs) + 1) * (len(ys) + 1) > max_cells * max_cells:
        return None
    xb = [0] + xs + [w]
    yb = [0] + ys + [h]
    cells = []
    for i in range(len(xb) - 1):
        for j in range(len(yb) - 1):
            x0, x1, y0, y1 = xb[i], xb[i + 1], yb[j], yb[j + 1]
            if x1 - x0 < w * 0.05 or y1 - y0 < h * 0.05:
                continue
            if fg[y0:y1, x0:x1].mean() < 0.01:
                continue
            cells.append((x0, y0, x1, y1))
    return cells if len(cells) >= 4 else None


def _full(im):
    return im.copy()


def selftest():
    """已知答案：白底四个分开的方块 → 必须切出 4 个；整幅单纹样 → 输出 1 个。"""
    print("=== 自检 ===")
    ok = True

    def show(name, cond, extra=""):
        nonlocal ok
        ok = ok and cond
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))

    import tempfile
    tmp = tempfile.mkdtemp(prefix="seg_selftest_")
    n = 600
    a = np.zeros((n, n, 3), np.uint8)
    a[..., 0], a[..., 1], a[..., 2] = 20, 30, 80
    # 四个白色方块，彼此相隔很远
    for cy, cx in [(100, 100), (100, 450), (450, 100), (450, 450)]:
        a[cy - 40:cy + 40, cx - 40:cx + 40] = (240, 240, 240)
    p = os.path.join(tmp, "four.png")
    Image.fromarray(a).save(p)
    parts = segment(p)
    show("四个分开的纹样被切成 4 块", len(parts) == 4, "切出 %d 块" % len(parts))

    # 整幅都是纹样（几乎铺满）→ 不应碎切
    b = np.zeros((n, n, 3), np.uint8)
    b[..., 0], b[..., 1], b[..., 2] = 20, 30, 80
    b[:, :] = (20, 30, 80)
    for k in range(0, n, 12):
        b[k:k + 4, 20:n - 20] = (235, 235, 235)
    p2 = os.path.join(tmp, "full.png")
    Image.fromarray(b).save(p2)
    parts2 = segment(p2)
    show("整幅纹样不被碎切", len(parts2) <= 2, "切出 %d 块" % len(parts2))

    # 纯空图不崩
    c = np.zeros((200, 200, 3), np.uint8)
    p3 = os.path.join(tmp, "blank.png")
    Image.fromarray(c).save(p3)
    show("纯空图不报错", len(segment(p3)) >= 1)

    print()
    print("自检：%s" % ("通过" if ok else "有失败项"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(ROOT, "data", "crawl_miao"))
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "motifs_split"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sheet", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not os.path.isdir(args.src):
        print("[失败] 找不到 %s" % args.src)
        return 2
    os.makedirs(args.out, exist_ok=True)

    files = []
    for f in sorted(os.listdir(args.src)):
        if not f.lower().endswith((".jpg", ".jpeg", ".png")):
            continue
        if any(k in f for k in SKIP_NAMES):
            continue
        try:
            w, h = Image.open(os.path.join(args.src, f)).size
        except Exception:
            continue
        # 横幅/细条不是纹样：导出图里的版权条、banner 全在这里挡掉
        if min(w, h) < 250 or max(w, h) / float(min(w, h)) > 4.0:
            continue
        files.append(f)
    if args.limit:
        files = files[:args.limit]

    rows, made, previews = [], 0, []
    for f in files:
        stem = os.path.splitext(f)[0]
        try:
            parts = segment(os.path.join(args.src, f))
        except Exception as e:                       # noqa: BLE001
            rows.append({"src": f, "part": "", "w": 0, "h": 0,
                         "note": "失败：%s" % str(e)[:40]})
            continue
        for k, (crop, bbox, frac) in enumerate(parts):
            name = "%s__%02d.png" % (stem, k)
            crop.save(os.path.join(args.out, name), optimize=True)
            rows.append({"src": f, "part": name, "w": crop.size[0],
                         "h": crop.size[1], "note": "面积占比 %.3f" % frac})
            made += 1
            if args.sheet and len(previews) < 16:
                previews.append(os.path.join(args.out, name))
        print("  %-40s → %d 块" % (f[:40], len(parts)))

    with io.open(os.path.join(args.out, "切块清单.csv"), "w",
                 encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["src", "part", "w", "h", "note"])
        w.writeheader()
        w.writerows(rows)

    print()
    print("%d 张原图 → %d 个纹样块" % (len(files), made))

    if previews:
        cell = 170
        cols = 4
        n = len(previews)
        sheet = Image.new("RGB", (cols * cell, ((n + cols - 1) // cols) * cell),
                          "white")
        for i, p in enumerate(previews):
            im = Image.open(p).convert("RGB")
            im.thumbnail((cell - 8, cell - 8))
            sheet.paste(im, ((i % cols) * cell + 4, (i // cols) * cell + 4))
        sp = os.path.join(args.out, "_切块预览.png")
        sheet.save(sp)
        print("预览：%s" % sp)
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    raise SystemExit(main())
