# -*- coding: utf-8 -*-
"""把抠好的透明纹样转成黑白图（白底黑纹）。

为什么单独一步而不重跑分割：
    抠图的形状已经在 alpha 通道里了，重新分割一遍既慢（19 分钟）
    又会因为随机性给出略微不同的结果。黑白化只取 alpha，
    形状和上一版严格一致，可追溯。

极性问题：
    默认 **白底黑纹**（像印章/剪纸），这是纹样最常用的呈现方式，
    也方便后面描矢量、做 ControlNet 线稿。
    要反相（黑底白纹）加 --invert。

输出：8 位灰度 PNG，只有 0 和 255 两个值。不用 1 位图是为了留 512 边长，
     1 位图边缘在缩放时锯齿更明显。

用法：
    python tools/patterns_to_bw.py --selftest
    python tools/patterns_to_bw.py
"""

from __future__ import annotations

import argparse
import io
import os
import sys

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATS = ["byx", "dx", "mx", "szmwx", "qt"]


def _otsu(vals, bins=128):
    """大津阈值（环境里没有 cv2，这里自己算）。"""
    hist, edges = np.histogram(vals, bins=bins, range=(0, 256))
    hist = hist.astype(np.float64)
    total = hist.sum()
    if total == 0:
        return 128.0
    w0 = np.cumsum(hist)
    w1 = total - w0
    mid = (edges[:-1] + edges[1:]) / 2.0
    cum = np.cumsum(hist * mid)
    m0 = np.where(w0 > 0, cum / np.maximum(w0, 1e-9), 0.0)
    m1 = np.where(w1 > 0, (cum[-1] - cum) / np.maximum(w1, 1e-9), 0.0)
    valid = (w0 > 0) & (w1 > 0)
    if not valid.any():
        return 128.0
    var = w0 * w1 * (m0 - m1) ** 2
    var[~valid] = -1
    return float(edges[int(np.argmax(var)) + 1])


def to_lineart(path, bg=0, fg=255, contrast_min=22.0):
    """把抠出的纹样转成「深底白线」的线稿。

    为什么要按亮度再分一次，而不是直接用 alpha 涂实心：
        用户要的是**绣线本身**（白线绣在深蓝布上那种），
        实心块会把针脚、花瓣轮廓、缠枝的走势全糊掉，看着像剪影。
        线稿才看得出纹样结构，也才能描矢量、做 ControlNet。

    在 alpha 内部按亮度做大津分割：亮的（绣线）→ 白，暗的（布底）→ 底。
    alpha 外面一律填底。

    contrast_min：alpha 内亮度极差太小（近乎单色）时，分不出"线"和"底"，
    这时退回把整个 alpha 当图案填白，至少不输出一张全黑的空图。
    """
    im = Image.open(path).convert("RGBA")
    rgb = np.asarray(im)[..., :3].astype(np.float32)
    a = np.asarray(im)[..., 3]
    inside = a > 127

    out = np.full(a.shape, bg, np.uint8)
    if not inside.any():
        return Image.fromarray(out, "L")

    gray = (0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2])
    vals = gray[inside]
    if float(np.percentile(vals, 95) - np.percentile(vals, 5)) < contrast_min:
        out[inside] = fg
        return Image.fromarray(out, "L")

    thr = _otsu(vals)
    out[inside & (gray >= thr)] = fg
    return Image.fromarray(out, "L")


def to_bw(path, invert=False, thr=127):
    """透明纹样 → 黑白剪影。alpha 判定为纹样的地方涂黑，其余涂白。"""
    im = Image.open(path).convert("RGBA")
    a = np.asarray(im)[..., 3]
    fg = a > thr
    v = np.where(fg, 0, 255).astype(np.uint8)
    if invert:
        v = 255 - v
    return Image.fromarray(v, "L")


def selftest():
    """已知答案：实心圆 + 中间一个洞 → 黑白图里洞必须还在，外面必须全白。"""
    print("=== 自检 ===")
    ok = True

    def show(name, cond, extra=""):
        nonlocal ok
        ok = ok and cond
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))

    import tempfile
    tmp = tempfile.mkdtemp(prefix="bw_selftest_")
    n = 200
    yy, xx = np.mgrid[0:n, 0:n]
    rr = np.hypot(yy - n // 2, xx - n // 2)
    rgba = np.zeros((n, n, 4), np.uint8)
    rgba[..., :3] = (200, 60, 50)
    rgba[..., 3] = np.where((rr < 80) & (rr > 30), 255, 0)
    p = os.path.join(tmp, "ring.png")
    Image.fromarray(rgba, "RGBA").save(p)

    bw = to_bw(p)
    v = np.asarray(bw)
    show("只有黑白两种值", set(np.unique(v).tolist()) <= {0, 255},
         str(sorted(set(np.unique(v).tolist()))))
    show("环带是黑的", v[n // 2, n // 2 + 55] == 0, str(v[n // 2, n // 2 + 55]))
    show("中间的洞仍是白的", v[n // 2, n // 2] == 255,
         str(v[n // 2, n // 2]))
    show("四角是白的", v[0, 0] == 255 and v[-1, -1] == 255,
         "%d/%d" % (v[0, 0], v[-1, -1]))

    inv = np.asarray(to_bw(p, invert=True))
    show("反相后极性对调", inv[n // 2, n // 2] == 0 and inv[0, 0] == 0,
         "%d/%d" % (inv[n // 2, n // 2], inv[0, 0]))

    # 线稿模式：深蓝底 + 白色细线，输出必须是黑底白线
    m = 240
    yy2, xx2 = np.mgrid[0:m, 0:m]
    rgba2 = np.zeros((m, m, 4), np.uint8)
    rgba2[..., 0], rgba2[..., 1], rgba2[..., 2] = 26, 32, 74
    rgba2[..., 3] = 255
    line = (np.abs((yy2 % 40) - 20) < 3) & (np.abs((xx2 % 40) - 20) < 20)
    rgba2[line] = (245, 245, 245, 255)
    p2 = os.path.join(tmp, "line.png")
    Image.fromarray(rgba2, "RGBA").save(p2)
    la = np.asarray(to_lineart(p2))
    show("线稿：白线变成 255", la[line].mean() > 240,
         "%.0f" % la[line].mean())
    show("线稿：底布变成 0", la[~line].mean() < 15,
         "%.0f" % la[~line].mean())
    show("线稿：只有黑白两值", set(np.unique(la).tolist()) <= {0, 255},
         str(sorted(set(np.unique(la).tolist()))[:4]))

    print()
    print("自检：%s" % ("通过" if ok else "有失败项"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(ROOT, "data", "motifs_complete"))
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "motifs_bw"))
    ap.add_argument("--mode", choices=["lineart", "silhouette"],
                    default="lineart",
                    help="lineart=深底白线（默认）；silhouette=白底黑影")
    ap.add_argument("--invert", action="store_true", help="剪影模式下反相")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not os.path.isdir(args.src):
        print("[失败] 找不到 %s" % args.src)
        return 2

    n = 0
    empty = 0
    for cat in CATS:
        d = os.path.join(args.src, cat)
        if not os.path.isdir(d):
            continue
        o = os.path.join(args.out, cat)
        os.makedirs(o, exist_ok=True)
        for f in sorted(os.listdir(d)):
            if not f.endswith(".png"):
                continue
            if args.mode == "lineart":
                im = to_lineart(os.path.join(d, f))
            else:
                im = to_bw(os.path.join(d, f), invert=args.invert)
            im.save(os.path.join(o, f), optimize=True)
            if (np.asarray(im) == 255).mean() < 0.02:
                empty += 1
            n += 1
        print("  %-6s 完成" % cat)

    print()
    print("共 %d 张；其中 %d 张白色面积不足 2%%（等于没抠出东西）" % (n, empty))
    print("输出：%s" % args.out)
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    raise SystemExit(main())
