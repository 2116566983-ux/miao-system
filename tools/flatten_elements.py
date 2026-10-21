# -*- coding: utf-8 -*-
"""把元素处理成「纹身贴」那种干净的样子。

要解决的问题
------------
现在每个元素是从照片里裁出来的方块，里面混着三样不该有的东西：
  1. 原照片的噪点和光照渐变（放大看是一格格的灰）
  2. 相邻纹样的残片（裁切边距带进来的）
  3. 深浅不一的底布，每张颜色都不一样，放一起很花

做法
----
1. 二值化，只留白线
2. 轻微膨胀后取**连通块**，保留与画面中心重叠的那一块，其余全部清掉
3. 底色换成**同一个固定色**（所有元素一个色，这是"统一"的意思）
4. 线条统一成纯白
5. 同时输出两份：不透明（纯色底）和透明底（贴到任何地方都不带底色）

这样出来的图，底色是平的、边是硬的、没有噪点。

用法：
    python tools/flatten_elements.py --selftest
    python tools/flatten_elements.py --sheet
"""

from __future__ import annotations

import argparse
import io
import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from patterns_to_bw import _otsu  # noqa: E402

BASE_RGB = (26, 42, 74)     # 统一底色：深靛蓝
WHITE = (255, 255, 255)
MIN_SPEC = 24               # 小于这个像素数的碎点丢掉


def clean_mask(path):
    """取与画面中心重叠的那个连通块，去碎点，返回 bool 掩膜 + 原图尺寸。"""
    im = Image.open(path).convert("RGB")
    a = np.asarray(im, dtype=np.float32)
    L = 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]
    thr = max(_otsu(L.ravel()), 60.0)
    fg = L > thr
    if not fg.any():
        return fg, im.size

    h, w = fg.shape
    dil = ndimage.binary_dilation(fg, iterations=max(2, int(min(h, w) * 0.01)))
    lab, n = ndimage.label(dil)
    if n == 0:
        return fg, im.size

    cy, cx = h // 2, w // 2
    target = lab[cy, cx]
    if target == 0:
        # 中心点不在任何块上（图形偏在一侧），取面积最大的那块
        sizes = ndimage.sum(dil, lab, range(1, n + 1))
        target = int(np.argmax(sizes)) + 1

    keep = (lab == target) & fg          # 只在原来的白线上保留，不要把膨胀的边也留下
    # 去碎点
    lab2, n2 = ndimage.label(keep)
    if n2 > 1:
        sizes = ndimage.sum(keep, lab2, range(1, n2 + 1))
        big = np.zeros(n2 + 1, bool)
        for i, s in enumerate(sizes, start=1):
            if s >= MIN_SPEC:
                big[i] = True
        keep = big[lab2]
    return keep, im.size


def is_lineart(path, min_purity=0.35):
    """判断原图是不是"平底 + 线条"那种。

    先按颜色档数判过，太严：JPEG 压缩会让一张纯净的蜡染也散出十几档，
    误杀 122 个真纹样。改用**底色占比**——平底线稿有个压倒性的主色，
    照片（人像、实物）没有。
    """
    im = Image.open(path).convert("RGB")
    im.thumbnail((160, 160))
    a = np.asarray(im)
    q = (a // 24).astype(np.int32)
    key = q[..., 0] * 4096 + q[..., 1] * 64 + q[..., 2]
    _, cnt = np.unique(key, return_counts=True)
    return float(cnt.max()) / float(cnt.sum()) >= min_purity


def looks_like_text(img):
    """清理后的图长得像文字吗：又长又扁的一串，或者很小的单字。

    英文水印常常是 "treacherous" 这种长条，或者 "N""M" 这种单字母；
    真正的纹样元素很少又扁又碎。
    """
    w, h = img.size
    a = np.asarray(img.convert("L"))
    white = (a > 200)
    if not white.any():
        return True
    frac = white.mean()
    ar = max(w, h) / float(max(1, min(w, h)))
    if ar > 2.4 and 0.15 < frac < 0.6:
        return True
    if max(w, h) < 30:          # 太小的单字/logo，当噪点
        return True
    return False


def render(path, base=BASE_RGB, transparent=False, pad=0.06):
    mask, _ = clean_mask(path)
    if not mask.any():
        return None
    h, w = mask.shape
    rgb = np.zeros((h, w, 3), np.uint8)
    rgb[...] = WHITE
    if transparent:
        rgba = np.dstack([rgb, (mask.astype(np.uint8) * 255)])
        out = Image.fromarray(rgba, "RGBA")
    else:
        canvas = np.zeros((h, w, 3), np.uint8)
        canvas[...] = base
        canvas[mask] = WHITE
        out = Image.fromarray(canvas, "RGB")

    # 收紧到内容外框，四周留一点边
    ys, xs = np.nonzero(mask)
    px = max(2, int((xs.max() - xs.min()) * pad))
    py = max(2, int((ys.max() - ys.min()) * pad))
    box = (max(0, xs.min() - px), max(0, ys.min() - py),
           min(w, xs.max() + px + 1), min(h, ys.max() + py + 1))
    return out.crop(box)


def selftest():
    """已知答案：主体 + 一个远处碎块 → 碎块必须被清掉，底色必须纯。"""
    print("=== 自检 ===")
    ok = True

    def show(name, cond, extra=""):
        nonlocal ok
        ok = ok and cond
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))

    import tempfile
    tmp = tempfile.mkdtemp(prefix="flat_selftest_")
    n = 300
    a = np.zeros((n, n, 3), np.uint8)
    a[..., 0], a[..., 1], a[..., 2] = 30, 45, 80
    # 主体：中心一个方框
    a[120:190, 120:190] = (240, 240, 240)
    # 远处一个碎块（不该保留）
    a[20:40, 20:40] = (240, 240, 240)
    p = os.path.join(tmp, "t.png")
    Image.fromarray(a).save(p)

    out = render(p)
    show("有输出", out is not None)
    v = np.asarray(out)
    uniq = np.unique(v.reshape(-1, 3), axis=0)
    show("只有两种颜色（纯色底 + 纯白线）", len(uniq) == 2, str(len(uniq)))
    show("底色是统一的深靛蓝", any((u == np.array(BASE_RGB)).all() for u in uniq))
    # 碎块在左上角，裁紧后不该出现在结果里
    show("远处碎块被清掉", out.size[0] < 120 and out.size[1] < 120,
         "%dx%d" % out.size)

    tr = render(p, transparent=True)
    show("透明版 alpha 只有 0/255",
         set(np.unique(np.asarray(tr)[..., 3]).tolist()) <= {0, 255})

    print()
    print("自检：%s" % ("通过" if ok else "有失败项"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(ROOT, "data", "elements"))
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "elements_clean"))
    ap.add_argument("--sheet", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not os.path.isdir(args.src):
        print("[失败] 找不到 %s" % args.src)
        return 2
    os.makedirs(os.path.join(args.out, "透明版"), exist_ok=True)

    files = sorted(f for f in os.listdir(args.src)
                   if f.lower().endswith(".png") and not f.startswith("_"))
    done = skipped_photo = skipped_text = 0
    for f in files:
        try:
            if not is_lineart(os.path.join(args.src, f)):
                skipped_photo += 1
                continue
            img = render(os.path.join(args.src, f))
            tr = render(os.path.join(args.src, f), transparent=True)
        except Exception:                        # noqa: BLE001
            continue
        if img is None:
            continue
        if looks_like_text(img):
            skipped_text += 1
            continue
        img.save(os.path.join(args.out, f), optimize=True)
        tr.save(os.path.join(args.out, "透明版", f), optimize=True)
        done += 1
        if done % 100 == 0:
            print("  已处理 %d/%d" % (done, len(files)))

    print("%d/%d 个元素已处理；跳过 照片类 %d、文字类 %d"
          % (done, len(files), skipped_photo, skipped_text))
    print("输出：%s（含 透明版/ 子目录）" % args.out)
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    raise SystemExit(main())
