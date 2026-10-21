# -*- coding: utf-8 -*-
"""按「深底白纹」风格筛选抓下来的图。

为什么要筛
----------
Bing 按「苗族纹样」搜出来的结果里混着大量**苗族服饰人像、白板、广告图**。
实测 211 张里只有约三成是真正的蜡染图案。不筛就把垃圾一起交付了。

判据只有两条，都直接对应"白线绣在深底上"这个视觉特征：
  1. 暗像素占比 ≥ 0.45     —— 画面以深底为主
  2. 亮像素占比 0.02~0.50  —— 有白色线条，但不是整片白（排除白底文档）

不按饱和度判：靛蓝本身饱和度很高（0.7 上下），用饱和度会把真正的蜡染误杀。

用法：
    python tools/filter_pattern_style.py --selftest
    python tools/filter_pattern_style.py            # 原地筛选，剔除的移到 _剔除/
    python tools/filter_pattern_style.py --dry-run   # 只统计不搬动
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import sys

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DARK_THR = 70          # 亮度低于这个算"暗"
BRIGHT_THR = 175       # 高于这个算"亮"
MIN_DARK = 0.45
MIN_BRIGHT = 0.02
MAX_BRIGHT = 0.50
MAX_SKIN = 0.03        # 肤色占比上限：蜡染图案里没有人
MAX_DARK_STD = 45.0    # 暗部亮度标准差：蜡染底布是平的，照片暗部有渐变
MAX_BRIGHT_SAT = 0.15  # 亮部（白线）饱和度上限：真蜡染的白纹接近中性色


def style_stats(path):
    """返回 (暗占比, 亮占比, 肤色占比, 暗部亮度标准差)。"""
    im = Image.open(path).convert("RGB")
    im.thumbnail((256, 256))
    a = np.asarray(im, dtype=np.float32)
    L = 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]
    dark_px = L[L < DARK_THR]
    dstd = float(dark_px.std()) if dark_px.size > 100 else 999.0
    hsv = np.asarray(im.convert("HSV"), dtype=np.float32)
    H = hsv[..., 0] * 360.0 / 255.0
    S = hsv[..., 1] / 255.0
    V = hsv[..., 2] / 255.0
    skin = float(((H > 5) & (H < 50) & (S > 0.2) & (S < 0.75)
                  & (V > 0.35)).mean())
    br = L > BRIGHT_THR
    bsat = float(S[br].mean()) if br.sum() > 50 else 9.0
    return (float((L < DARK_THR).mean()), float((L > BRIGHT_THR).mean()),
            skin, dstd, bsat)


def is_pattern_style(path):
    dark, bright, skin, dstd, bsat = style_stats(path)
    good = (dark >= MIN_DARK and MIN_BRIGHT <= bright <= MAX_BRIGHT
            and skin <= MAX_SKIN and dstd <= MAX_DARK_STD
            and bsat <= MAX_BRIGHT_SAT)
    return good, dark, bright


def selftest():
    """已知答案：深底白线的图要通过，白底文档和灰照片要拒掉。"""
    print("=== 自检 ===")
    ok = True

    def show(name, cond, extra=""):
        nonlocal ok
        ok = ok and cond
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))

    import tempfile
    tmp = tempfile.mkdtemp(prefix="filter_selftest_")
    n = 200
    yy, xx = np.mgrid[0:n, 0:n]

    # 深蓝底 + 白色曲线 → 应通过
    a = np.zeros((n, n, 3), np.uint8)
    a[..., 0], a[..., 1], a[..., 2] = 18, 26, 70
    line = (np.abs(np.sin(xx / 9.0) * 20 + 100 - yy) < 3)
    a[line] = (245, 245, 245)
    p1 = os.path.join(tmp, "batik.png")
    Image.fromarray(a).save(p1)
    v, d, b = is_pattern_style(p1)
    show("深底白线通过", v, "暗 %.2f 亮 %.2f" % (d, b))

    # 白底深字（文档）→ 应拒
    w = np.full((n, n, 3), 250, np.uint8)
    w[40:160, 30:170] = (30, 30, 30)
    p2 = os.path.join(tmp, "doc.png")
    Image.fromarray(w).save(p2)
    v2, d2, b2 = is_pattern_style(p2)
    show("白底文档被拒", not v2, "暗 %.2f 亮 %.2f" % (d2, b2))

    # 中间灰的照片 → 应拒
    g = np.full((n, n, 3), 130, np.uint8)
    g[::7, :] = 150
    p3 = os.path.join(tmp, "photo.png")
    Image.fromarray(g).save(p3)
    v3, d3, b3 = is_pattern_style(p3)
    show("中间灰照片被拒", not v3, "暗 %.2f 亮 %.2f" % (d3, b3))

    print()
    print("自检：%s" % ("通过" if ok else "有失败项"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(ROOT, "data", "crawl_miao"))
    ap.add_argument("--reject-dir", default="_剔除")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not os.path.isdir(args.dir):
        print("[失败] 找不到目录 %s" % args.dir)
        return 2

    files = sorted(f for f in os.listdir(args.dir)
                   if f.lower().endswith((".jpg", ".jpeg", ".png")))
    keep, reject = [], []
    for f in files:
        try:
            good, d, b = is_pattern_style(os.path.join(args.dir, f))
        except Exception:
            reject.append(f)
            continue
        (keep if good else reject).append(f)

    print("总计 %d 张：符合深底白纹 %d 张，剔除 %d 张"
          % (len(files), len(keep), len(reject)))

    if args.dry_run:
        return 0

    rd = os.path.join(args.dir, args.reject_dir)
    os.makedirs(rd, exist_ok=True)
    moved = 0
    for f in reject:
        src = os.path.join(args.dir, f)
        dst = os.path.join(rd, f)
        if os.path.exists(src):
            shutil.move(src, dst)
            moved += 1
    print("已把 %d 张移到 %s" % (moved, rd))
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    raise SystemExit(main())
