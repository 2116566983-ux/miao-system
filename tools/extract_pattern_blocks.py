# -*- coding: utf-8 -*-
"""整幅纹样提取：去掉照片背景，保留完整图案，绝不残缺。

为什么不用 `motif_extract.extract()`
------------------------------------
那套是给「底布上一个独立绣花」设计的，内部做了三件事都会**丢图案**：

1. `_largest_components(keep=6)` —— 只保留面积最大的 6 个连通块，
   满地绣的小花小枝直接消失；
2. `clean_mask` 的形态学开运算 —— 边缘被削掉一圈；
3. `crop_to_content` 按掩膜外接框裁 —— 掩膜不全时整块图案被截断。

结果就是用户看到的「像残缺的一块」。本工具反其道：
**只判断"哪里是照片背景"，其余全部原样保留**，不选块、不腐蚀、不填洞。

输出是方形图案块（不透明），可直接当素材/贴图用。

用法：
    python tools/extract_pattern_blocks.py --selftest
    python tools/extract_pattern_blocks.py --limit 12 --sheet
    python tools/extract_pattern_blocks.py --max-side 512
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time

import numpy as np
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from motif_extract import trim_black_bars, grow_background  # noqa: E402

DEFAULT_SRC = r"C:\Users\21165\WorkBuddy\2026-10-13-17-23-42\_aesthetic_work\basic"
CATS = ["byx", "dx", "mx", "szmwx", "qt"]

# 判定「边框是不是统一背景色」的容差（各通道差之和，0-765）
BG_TOL = 60
# 边框上至少这么多比例的像素接近该色，才认为存在照片背景
BG_MIN_FRAC = 0.55


def _ring_pixels(a):
    return np.concatenate([
        a[0, :, :].reshape(-1, 3), a[-1, :, :].reshape(-1, 3),
        a[:, 0, :].reshape(-1, 3), a[:, -1, :].reshape(-1, 3),
    ])


def detect_background(small):
    """判断小图上有没有「统一的照片背景」。返回 (bg_mask 或 None, 说明)。

    为什么要逐边判断，而不是把四条边混在一起算：
        实拍照片里绣片常常**顶到画面某一边**（比如布边压在照片下沿），
        四条边混起来一算，纯黑背景的占比就被拉低到 0.55 以下，
        于是整幅保留、背景留在图里。改成逐边：只要**两条以上**边的
        底色一致，就认定那是照片背景。
    """
    h, w, _ = small.shape
    edges = {
        "上": small[0, :, :].reshape(-1, 3),
        "下": small[-1, :, :].reshape(-1, 3),
        "左": small[:, 0, :].reshape(-1, 3),
        "右": small[:, -1, :].reshape(-1, 3),
    }
    uniform = {}
    for name, px in edges.items():
        px = px.astype(np.float32)
        med = np.median(px, axis=0)
        frac = float((np.abs(px - med).sum(axis=1) < BG_TOL).mean())
        if frac >= 0.80:
            uniform[name] = med

    # 取出现次数最多的那种底色（两条边以上一致才算）
    chosen = None
    for name, med in uniform.items():
        same = [n for n, m in uniform.items()
                if float(np.abs(m - med).sum()) < BG_TOL]
        if len(same) >= 2:
            chosen = np.median([uniform[n] for n in same], axis=0)
            break

    if chosen is None:
        return None, "没有两条一致的纯色边，按整幅图案处理"

    mask = grow_background(small, tol=BG_TOL, cloth_rgb=chosen)
    ratio = float(mask.mean())
    if ratio < 0.02 or ratio > 0.85:
        return None, "背景占比异常(%.2f)，按整幅图案处理" % ratio
    return mask, "去掉背景 %.0f%%" % (ratio * 100)


def whole_pattern(path, max_side=512, work=512):
    """返回 (方形 RGBA 图, 说明 dict)。不做任何选块/腐蚀，图案完整保留。"""
    im = Image.open(path).convert("RGB")
    full = np.asarray(im)
    arr, trimmed = trim_black_bars(full)
    h, w = arr.shape[:2]

    # 在小图上找背景，边界框再放大回原图 —— 大图上逐像素生长太慢
    s = work / float(max(h, w)) if max(h, w) > work else 1.0
    sw, sh = max(8, int(w * s)), max(8, int(h * s))
    small = np.asarray(Image.fromarray(arr).resize((sw, sh), Image.BILINEAR))
    bg, note = detect_background(small)

    if bg is None:
        x0, x1, y0, y1 = 0, w, 0, h
    else:
        fg = ~bg
        if not fg.any():
            x0, x1, y0, y1 = 0, w, 0, h
        else:
            ys, xs = np.nonzero(fg)
            pad = 2
            x0 = max(0, int(xs.min() / s) - pad)
            x1 = min(w, int(xs.max() / s) + pad + 1)
            y0 = max(0, int(ys.min() / s) - pad)
            y1 = min(h, int(ys.max() / s) + pad + 1)

    # 关键：裁剪框内**全部保留**，不乘掩膜、不挖洞，所以不会残缺
    cut = arr[y0:y1, x0:x1]
    if cut.size == 0:
        cut = arr
    ch, cw = cut.shape[:2]

    # 不补正方形。补方要靠边缘像素延展，横向延展几百像素就会拉出
    # 一条条彩色条纹（实测 basic_156/180 很明显），看着比残缺还糟。
    # 保持原始比例，需要方形的时候由下游自己决定怎么裁。
    out = Image.fromarray(cut).convert("RGBA")
    if max_side and max(ch, cw) > max_side:
        k = max_side / float(max(ch, cw))
        out = out.resize((max(1, int(cw * k)), max(1, int(ch * k))),
                         Image.LANCZOS)
    return out, {"note": note, "trimmed": bool(trimmed),
                 "bbox": (x0, y0, x1, y1)}


def collect(src):
    items = []
    if not os.path.isdir(src):
        return items
    subs = [d for d in sorted(os.listdir(src))
            if os.path.isdir(os.path.join(src, d))]
    if any(s in CATS for s in subs):
        for cat in subs:
            d = os.path.join(src, cat)
            for fn in sorted(os.listdir(d)):
                if fn.lower().endswith((".jpg", ".jpeg", ".png")):
                    items.append((cat, os.path.join(d, fn), fn))
    else:
        for fn in sorted(os.listdir(src)):
            if fn.lower().endswith((".jpg", ".jpeg", ".png")):
                items.append(("flat", os.path.join(src, fn), fn))
    return items


def _count_blobs(mask, min_area=40):
    """数连通块（4 邻接，BFS）。用来验证「分散的图案一个都没丢」。

    不数像素：输出会缩放，像素数跟着变，阈值很难写稳；
    数块数不受缩放影响，而且正好对应"图案有没有被丢掉"这个问题。
    """
    from collections import deque
    h, w = mask.shape
    seen = np.zeros_like(mask, bool)
    n = 0
    for y in range(h):
        row = mask[y]
        for x in np.nonzero(row & ~seen[y])[0]:
            if seen[y, x]:
                continue
            q = deque([(y, x)])
            seen[y, x] = True
            area = 0
            while q:
                cy, cx = q.popleft()
                area += 1
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] \
                            and not seen[ny, nx]:
                        seen[ny, nx] = True
                        q.append((ny, nx))
            if area >= min_area:
                n += 1
    return n


def selftest():
    """已知答案：底布上有一整块图案时，输出必须包含整块，不能只剩一角。"""
    print("=== 自检 ===")
    ok = True

    def show(name, cond, extra=""):
        nonlocal ok
        ok = ok and cond
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))

    import tempfile
    tmp = tempfile.mkdtemp(prefix="whole_selftest_")
    n = 400
    yy, xx = np.mgrid[0:n, 0:n]

    # 白底 + 五个分散的小方块（模拟满地绣）：一个都不许丢
    img = np.full((n, n, 3), 250, np.uint8)
    spots = [(80, 90), (250, 60), (150, 260), (300, 300), (60, 320)]
    for cy, cx in spots:
        img[cy - 18:cy + 18, cx - 18:cx + 18] = (40, 60, 160)
    p = os.path.join(tmp, "spots.png")
    Image.fromarray(img).save(p)
    out, meta = whole_pattern(p, max_side=256)
    a = np.asarray(out)[..., :3].astype(int)
    dark = a.sum(axis=2) < 500
    show("五个分散图案一个没丢",
         _count_blobs(dark) == 5, "连通块 %d 个" % _count_blobs(dark))
    show("输出有实际尺寸", min(out.size) > 32, str(out.size))

    # 纯色图：没有图案，也不许崩
    flat = np.full((n, n, 3), 200, np.uint8)
    p2 = os.path.join(tmp, "flat.png")
    Image.fromarray(flat).save(p2)
    out2, _ = whole_pattern(p2, max_side=128)
    show("纯色图不报错且有输出", min(out2.size) > 8, str(out2.size))

    print()
    print("自检：%s" % ("通过" if ok else "有失败项"))
    return 0 if ok else 1


def make_sheet(pairs, path, cell=210, cols=4):
    """原图 / 结果 成对拼图，供人眼复核。"""
    rows = (len(pairs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell * 2, rows * (cell + 18)), "white")
    dr = ImageDraw.Draw(sheet)
    for i, (src, out, label) in enumerate(pairs):
        r, c = i // cols, i % cols
        x = c * cell * 2
        y = r * (cell + 18) + 14
        o = Image.open(src).convert("RGB")
        o.thumbnail((cell - 6, cell - 6))
        sheet.paste(o, (x + 3, y))
        t = out.convert("RGB")
        t.thumbnail((cell - 6, cell - 6))
        sheet.paste(t, (x + cell + 3, y))
        dr.text((x + 3, y - 12), label[:26], fill="black")
    sheet.save(path)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_SRC)
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "patterns_whole"))
    ap.add_argument("--manifest", default=os.path.join(
        ROOT, "metadata", "整幅纹样清单.csv"))
    ap.add_argument("--max-side", type=int, default=512)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sheet", action="store_true", help="输出成对复核拼图")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    items = collect(args.root)
    if not items:
        print("[失败] %s 下没找到图片" % args.root)
        return 2
    if args.limit:
        items = items[:args.limit]
    print("待处理 %d 张 → %s" % (len(items), args.out))

    import csv
    rows = []
    pairs = []
    t0 = time.time()
    for i, (cat, path, fn) in enumerate(items, 1):
        try:
            out, meta = whole_pattern(path, max_side=args.max_side)
            d = os.path.join(args.out, cat)
            os.makedirs(d, exist_ok=True)
            fp = os.path.join(d, os.path.splitext(fn)[0] + ".png")
            out.save(fp, optimize=True)
            rows.append({"cat": cat, "file": "%s/%s" % (cat, fn),
                         "out": fp, "size": "%dx%d" % out.size,
                         "note": meta["note"],
                         "trimmed": int(meta["trimmed"])})
            if args.sheet and i <= 8:
                pairs.append((path, out, "%s/%s" % (cat, fn)))
        except Exception as e:
            rows.append({"cat": cat, "file": "%s/%s" % (cat, fn),
                         "out": "", "size": "", "note": "失败：%s" % e,
                         "trimmed": 0})
        if i % 100 == 0:
            print("  %d/%d，用时 %.0f 秒" % (i, len(items), time.time() - t0))

    os.makedirs(os.path.dirname(args.manifest), exist_ok=True)
    with io.open(args.manifest, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["cat", "file", "out", "size",
                                          "note", "trimmed"])
        w.writeheader()
        w.writerows(rows)

    good = [r for r in rows if r["out"]]
    print()
    print("完成 %d / %d，用时 %.1f 分钟" % (len(good), len(rows),
                                           (time.time() - t0) / 60))
    from collections import Counter
    print("处理方式：", dict(Counter(r["note"].split("(")[0] for r in rows)))
    if pairs:
        p = os.path.join(args.out, "_对照拼图.png")
        make_sheet(pairs, p)
        print("对照拼图：%s" % p)
    print("清单：%s" % args.manifest)
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    raise SystemExit(main())
