# -*- coding: utf-8 -*-
"""把黑白线稿补成一幅完整的对称纹样。

为什么要这一步
--------------
用户要的是「完整」：一幅填满画框、四边闭合、对称的纹样。
数据集里的照片多数是**斜着拍的布片局部**，直接转黑白，出来的是一条斜条，
四边都是断的 —— 看着就不完整。

补齐的办法是**镜像四方**：拿中心正方形当作一个单元，左右镜像一次、上下镜像
一次，拼成 2×2。这样：
  - 四边天然闭合，不再有"被裁断"的边缘
  - 图案自带左右、上下对称，符合苗绣最常见的对称构图
  - 单元在四个角各出现一次，中间形成一个自然的对称花心

这是**几何补全**，不是 AI 重绘：不凭空造不存在的针法，
只是把已有的图案按对称关系延伸。要更自由的补全得走生成模型，
那属于 ② 页的活。

用法：
    python tools/patterns_tile.py --selftest
    python tools/patterns_tile.py --limit 8 --sheet
    python tools/patterns_tile.py
"""

from __future__ import annotations

import argparse
import io
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATS = ["byx", "dx", "mx", "szmwx", "qt"]
BG = 8            # 底色的灰度值（近黑，和样例的深蓝底观感一致）
PAD = 0.06        # 单元四周留白比例，避免图案贴边


def _content_bbox(arr, thr=40):
    ys, xs = np.nonzero(arr > thr)
    if len(ys) == 0:
        return None
    return xs.min(), ys.min(), xs.max() + 1, ys.max() + 1


def make_tile(path, unit=512, bg=BG, pad=PAD):
    """单张线稿 → 完整对称方图（2×2 镜像）。"""
    im = Image.open(path).convert("L")
    a = np.asarray(im)
    box = _content_bbox(a)
    if box is None:
        return Image.new("L", (unit, unit), bg)

    x0, y0, x1, y1 = box
    # 按内容外框直接取矩形，**不**强行补成正方形。
    # 强行补方会在短边留出空带，镜像之后空带叠在缝上，
    # 画面正中就出现一个黑色十字（byx/250、mx/672 都是这样）。
    canvas = im.crop((x0, y0, x1, y1))
    cw, ch = canvas.size
    if cw < 8 or ch < 8:
        return Image.new("L", (unit, unit), bg)

    # 2×2 镜像后是 2cw × 2ch，再从中间裁一个正方形：
    # 这样四边都是图案、没有空带，也不会把图案拉变形
    tile = Image.new("L", (cw * 2, ch * 2), bg)
    tile.paste(canvas, (0, 0))
    tile.paste(canvas.transpose(Image.FLIP_LEFT_RIGHT), (cw, 0))
    tile.paste(canvas.transpose(Image.FLIP_TOP_BOTTOM), (0, ch))
    tile.paste(canvas.transpose(Image.ROTATE_180), (cw, ch))

    side = min(cw * 2, ch * 2)
    left = (cw * 2 - side) // 2
    top = (ch * 2 - side) // 2
    tile = tile.crop((left, top, left + side, top + side))

    inner = int(unit * (1 - 2 * pad))
    out = Image.new("L", (unit, unit), bg)
    out.paste(tile.resize((inner, inner), Image.LANCZOS),
              ((unit - inner) // 2, (unit - inner) // 2))
    return out


def selftest():
    """已知答案：左半边有图案、右半边空 → 补齐后右半边必须也有图案。"""
    print("=== 自检 ===")
    ok = True

    def show(name, cond, extra=""):
        nonlocal ok
        ok = ok and cond
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))

    import tempfile
    tmp = tempfile.mkdtemp(prefix="tile_selftest_")
    n = 256
    a = np.full((n, n), BG, np.uint8)
    a[60:200, 30:120] = 255          # 只有左半边有图案
    p = os.path.join(tmp, "half.png")
    Image.fromarray(a, "L").save(p)

    out = make_tile(p, unit=256)
    o = np.asarray(out)
    lh = (o[:, : n // 2] > 200).sum()
    rh = (o[:, n // 2:] > 200).sum()
    show("右边被补出图案", rh > 0, "左 %d / 右 %d" % (lh, rh))
    show("左右大致对称", abs(lh - rh) <= 0.25 * max(lh, rh),
         "差 %.0f%%" % (100.0 * abs(lh - rh) / max(lh, rh)))
    show("输出是正方形", out.size[0] == out.size[1], str(out.size))

    # 全空图不应崩
    blank = Image.new("L", (64, 64), BG)
    p2 = os.path.join(tmp, "blank.png")
    blank.save(p2)
    out2 = make_tile(p2, unit=128)
    show("全空图不报错", out2.size == (128, 128), str(out2.size))

    print()
    print("自检：%s" % ("通过" if ok else "有失败项"))
    return 0 if ok else 1


def make_sheet(items, path, cell=320, cols=4):
    rows = (len(items) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell * 2, rows * (cell + 16)), "white")
    dr = ImageDraw.Draw(sheet)
    for i, (bw, tile, label) in enumerate(items):
        r, c = i // cols, i % cols
        x, y = c * cell * 2, r * (cell + 16) + 14
        a = Image.open(bw).convert("RGB")
        a.thumbnail((cell - 8, cell - 8))
        sheet.paste(a, (x + 3, y))
        b = Image.open(tile).convert("RGB")
        b.thumbnail((cell - 8, cell - 8))
        sheet.paste(b, (x + cell + 3, y))
        dr.text((x + 3, y - 12), label[:30], fill="black")
    sheet.save(path)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(ROOT, "data", "motifs_bw"))
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "motifs_tile"))
    ap.add_argument("--unit", type=int, default=1024)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sheet", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not os.path.isdir(args.src):
        print("[失败] 找不到 %s" % args.src)
        return 2

    items, pairs = [], []
    for cat in CATS:
        d = os.path.join(args.src, cat)
        if not os.path.isdir(d):
            continue
        o = os.path.join(args.out, cat)
        os.makedirs(o, exist_ok=True)
        files = sorted(f for f in os.listdir(d) if f.endswith(".png"))
        if args.limit:
            files = files[:args.limit]
        for k, f in enumerate(files):
            src = os.path.join(d, f)
            dst = os.path.join(o, f)
            make_tile(src, unit=args.unit).save(dst, optimize=True)
            items.append(dst)
            if args.sheet and len(pairs) < 8:
                pairs.append((src, dst, "%s/%s" % (cat, os.path.splitext(f)[0])))
        print("  %-6s %d 张" % (cat, len(files)))
        if args.limit:
            break

    print()
    print("共生成 %d 张完整方图，尺寸 %d×%d" % (len(items), args.unit, args.unit))
    if pairs:
        p = os.path.join(args.out, "_对照拼图.png")
        make_sheet(pairs, p)
        print("对照拼图：%s" % p)
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    raise SystemExit(main())
