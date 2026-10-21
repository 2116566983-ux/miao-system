# -*- coding: utf-8 -*-
"""把纹样块再拆成单个图形元素（一朵花、一只鸟、一片叶）。

和上一层的区别
--------------
`segment_motifs.py` 切的是「一个纹样」（整幅团花、整段边饰）。
这里切的是纹样**内部的元素**，切完每个文件只有一个图形，
可以直接拿去配对、重组、做素材库。

关键在膨胀半径
--------------
一个元素由多笔线条组成（花瓣的轮廓、叶脉、眼睛），不膨胀会被打散成几十段。
但膨胀过头，相邻的两个元素又会被粘成一团。
所以半径取图幅的千分之四左右：只够把同一元素内部的断笔连上，
不够跨过元素之间的空白。

用法：
    python tools/split_elements.py --selftest
    python tools/split_elements.py --src data/motifs_split --sheet
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from patterns_to_bw import _otsu  # noqa: E402

WORK = 800
DILATE_FRAC = 0.004        # 千分之四：连断笔，不连邻居
MIN_AREA_FRAC = 0.0008     # 小于画面万分之八当噪点
MAX_AREA_FRAC = 0.60       # 大于 60% 当整幅（不再拆）
PAD = 0.05


def bg_purity(path):
    """背景纯度 = 出现最多的那档颜色占画面的比例。

    蜡染是平底，纯度普遍在 0.5 以上；墙面照、招牌照、带渐变的反光照片
    纯度掉到 0.1~0.2。拆元素前先过这道，能把"拍的是一面墙"的块挡在外面，
    否则墙上的汉字会被当成纹样元素拆出来。
    """
    im = Image.open(path).convert("RGB")
    im.thumbnail((200, 200))
    a = np.asarray(im)
    q = (a // 24).astype(np.int32)
    key = q[..., 0] * 4096 + q[..., 1] * 64 + q[..., 2]
    count = np.unique(key, return_counts=True)[1]
    return float(count.max()) / float(count.sum())


def bg_purity(path):
    """背景纯度 = 出现最多的那档颜色占画面的比例。

    蜡染是平底，纯度普遍在 0.5 以上；墙面照、招牌照、带渐变的反光照片
    纯度掉到 0.1~0.2。拆元素前先过这道，能把"拍的是一面墙"的块挡在外面，
    否则墙上的汉字会被当成纹样元素拆出来。
    """
    im = Image.open(path).convert("RGB")
    im.thumbnail((200, 200))
    a = np.asarray(im)
    q = (a // 24).astype(np.int32)
    key = q[..., 0] * 4096 + q[..., 1] * 64 + q[..., 2]
    count = np.unique(key, return_counts=True)[1]
    return float(count.max()) / float(count.sum())


def split_elements(path):
    """返回 [(crop, bbox, area_frac), ...]"""
    im = Image.open(path).convert("RGB")
    W, H = im.size
    s = WORK / float(max(W, H)) if max(W, H) > WORK else 1.0
    small = im.resize((max(16, int(W * s)), max(16, int(H * s))), Image.BILINEAR)
    a = np.asarray(small, dtype=np.float32)
    L = 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]

    thr = max(_otsu(L.ravel()), 60.0)
    fg = L > thr
    if fg.mean() < 0.002:                    # 没有可拆的东西，整块算一个元素
        return [(im.copy(), (0, 0, W, H), 1.0)]

    r = max(1, int(round(max(small.size) * DILATE_FRAC)))
    fg = ndimage.binary_dilation(fg, iterations=r)

    lab, n = ndimage.label(fg)
    total = float(fg.shape[0] * fg.shape[1])
    out = []
    for sl in ndimage.find_objects(lab):
        if sl is None:
            continue
        y0, y1 = sl[0].start, sl[0].stop
        x0, x1 = sl[1].start, sl[1].stop
        frac = float((lab[sl] > 0).sum()) / total
        if frac < MIN_AREA_FRAC or frac > MAX_AREA_FRAC:
            continue
        ox0, oy0 = int(x0 / s), int(y0 / s)
        ox1, oy1 = int(np.ceil(x1 / s)), int(np.ceil(y1 / s))
        px = max(3, int((ox1 - ox0) * PAD))
        py = max(3, int((oy1 - oy0) * PAD))
        bx0, by0 = max(0, ox0 - px), max(0, oy0 - py)
        bx1, by1 = min(W, ox1 + px), min(H, oy1 + py)
        if bx1 - bx0 < 16 or by1 - by0 < 16:
            continue
        out.append((im.crop((bx0, by0, bx1, by1)), (bx0, by0, bx1, by1), frac))

    if not out:                              # 全被筛掉时退回整块，不交白卷
        return [(im.copy(), (0, 0, W, H), 1.0)]
    out.sort(key=lambda t: (t[1][1], t[1][0]))
    return out


def selftest():
    """已知答案：一朵由三笔组成的"花" + 一只独立的"鸟" → 必须切出 2 个元素。"""
    print("=== 自检 ===")
    ok = True

    def show(name, cond, extra=""):
        nonlocal ok
        ok = ok and cond
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))

    import tempfile
    tmp = tempfile.mkdtemp(prefix="elem_selftest_")
    n = 600
    a = np.zeros((n, n, 3), np.uint8)
    a[..., 0], a[..., 1], a[..., 2] = 20, 30, 80

    # 花：三条相距 6px 的弧线（模拟花瓣轮廓，属于同一元素）
    for k in range(3):
        cy, cx, rr = 200, 200, 60 + k * 6
        yy, xx = np.mgrid[0:n, 0:n]
        ring = (np.abs(np.hypot(yy - cy, xx - cx) - rr) < 2)
        a[ring] = (245, 245, 245)
    # 鸟：右下方另一团（离花很远）
    for k in range(2):
        a[430:440, 380 + k * 20:500] = (245, 245, 245)

    p = os.path.join(tmp, "flower_bird.png")
    Image.fromarray(a).save(p)
    parts = split_elements(p)
    show("三笔组成的花 + 独立的鸟 = 2 个元素", len(parts) == 2,
         "切出 %d 个" % len(parts))

    # 纯色块没有元素可分，也不能崩
    b = np.full((200, 200, 3), 30, np.uint8)
    p2 = os.path.join(tmp, "blank.png")
    Image.fromarray(b).save(p2)
    show("空图不报错", len(split_elements(p2)) >= 1)

    print()
    print("自检：%s" % ("通过" if ok else "有失败项"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(ROOT, "data", "motifs_split"))
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "elements"))
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

    files = sorted(f for f in os.listdir(args.src)
                   if f.lower().endswith(".png") and not f.startswith("_"))
    if args.limit:
        files = files[:args.limit]

    rows, made, previews = [], 0, []
    for f in files:
        stem = os.path.splitext(f)[0]
        try:
            if bg_purity(os.path.join(args.src, f)) < 0.30:
                rows.append({"src": f, "element": "", "w": 0, "h": 0,
                             "note": "跳过：背景不平（疑似照片/招牌）"})
                print("  %-44s 跳过（背景不平）" % f[:44])
                continue
            parts = split_elements(os.path.join(args.src, f))
        except Exception as e:                        # noqa: BLE001
            rows.append({"src": f, "element": "", "w": 0, "h": 0,
                         "note": "失败：%s" % str(e)[:40]})
            continue
        for k, (crop, bbox, frac) in enumerate(parts):
            name = "%s__e%02d.png" % (stem, k)
            crop.save(os.path.join(args.out, name), optimize=True)
            rows.append({"src": f, "element": name, "w": crop.size[0],
                         "h": crop.size[1], "note": "占比 %.4f" % frac})
            made += 1
            if args.sheet and len(previews) < 24:
                previews.append(os.path.join(args.out, name))
        print("  %-44s → %d 元素" % (f[:44], len(parts)))

    with io.open(os.path.join(args.out, "元素清单.csv"), "w",
                 encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["src", "element", "w", "h", "note"])
        w.writeheader()
        w.writerows(rows)

    print()
    print("%d 块纹样 → %d 个元素（平均每块 %.1f 个）"
          % (len(files), made, made / max(1, len(files))))

    if previews:
        cell = 140
        cols = 8
        sheet = Image.new("RGB", (cols * cell,
                                  ((len(previews) + cols - 1) // cols) * cell),
                          "white")
        for i, p in enumerate(previews):
            im = Image.open(p).convert("RGB")
            im.thumbnail((cell - 6, cell - 6))
            sheet.paste(im, ((i % cols) * cell + 3, (i // cols) * cell + 3))
        sp = os.path.join(args.out, "_元素预览.png")
        sheet.save(sp)
        print("预览：%s" % sp)
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    raise SystemExit(main())
