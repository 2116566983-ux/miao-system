# -*- coding: utf-8 -*-
"""输出「一个完整的纹样」：白线黑底、居中、不重复、不裁断。

和前一版的区别
--------------
前一版做了 2×2 镜像四方连续，用户明确否掉了：「你不需要是四方的，你需要完整」。
四方连续是把一个单元复制四遍，图案会重复出现四次；用户要的是**一个**完整图案。

这一版只做三件事：
1. 按纹样实际外框裁齐（四周留一点边距）
2. 保持原始长宽比（不强行补正方形——补方会留空带）
3. 外框之外填黑，纹样本身不动

不做镜像、不做重复、不做拉伸。

用法：
    python tools/patterns_final.py --selftest
    python tools/patterns_final.py --limit 8 --sheet
    python tools/patterns_final.py
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
BG = 8
MARGIN = 0.05      # 四周留白占图案边长的比例


def crop_complete(path, max_side=1024, margin=MARGIN, bg=BG, thr=40):
    """白线黑底图 → 按内容裁齐，保持原比例，不重复。"""
    im = Image.open(path).convert("L")
    a = np.asarray(im)
    ys, xs = np.nonzero(a > thr)
    if len(ys) == 0:
        return Image.new("L", (max_side, max_side), bg), {"empty": True}

    x0, x1 = int(xs.min()), int(xs.max()) + 1
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    w, h = x1 - x0, y1 - y0

    # 边距按较长边算，保证四边留白一致
    pad = int(round(max(w, h) * margin))
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(a.shape[1], x1 + pad), min(a.shape[0], y1 + pad)

    cut = im.crop((x0, y0, x1, y1))
    cw, ch = cut.size
    if max_side and max(cw, ch) > max_side:
        k = max_side / float(max(cw, ch))
        cut = cut.resize((max(1, int(cw * k)), max(1, int(ch * k))),
                         Image.LANCZOS)
    return cut, {"empty": False, "size": cut.size,
                 "aspect": round(cut.size[0] / float(cut.size[1]), 3)}


def selftest():
    """已知答案：
    1) 图案完整保留（白像素总量不因处理而增多 → 说明没复制重复）
    2) 长宽比保持（不做正方形化）
    3) 四边不留大片空白（裁齐了）
    """
    print("=== 自检 ===")
    ok = True

    def show(name, cond, extra=""):
        nonlocal ok
        ok = ok and cond
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))

    import tempfile
    tmp = tempfile.mkdtemp(prefix="final_selftest_")

    # 一个 2:1 的图案，放在 400×400 的黑底里
    a = np.full((400, 400), BG, np.uint8)
    a[120:280, 60:340] = 255         # 280 宽 × 160 高，比例 1.75
    p = os.path.join(tmp, "wide.png")
    Image.fromarray(a, "L").save(p)

    out, info = crop_complete(p, max_side=1024)
    w, h = out.size
    show("长宽比保持（不强制正方形）", abs(w / float(h) - 1.75) < 0.12,
         "%.2f" % (w / float(h)))
    show("裁掉了多余黑边", w < 360 and h < 220, "%dx%d" % (w, h))

    # 白像素总量：源是 280*160，输出只允许因缩放变化，不允许翻倍（翻倍=重复了）
    src_white = int((a > 200).sum())
    out_white = int((np.asarray(out) > 200).sum())
    show("没有复制重复（白像素未翻倍）", out_white < src_white * 1.3,
         "源 %d → 出 %d" % (src_white, out_white))

    # 图案必须完整：外框四条边上不能有白像素贴着（否则说明被裁断）
    o = np.asarray(out) > 200
    show("四边没有被裁断的图案",
         not (o[0, :].any() or o[-1, :].any() or o[:, 0].any() or o[:, -1].any()),
         "上%d 下%d 左%d 右%d" % (o[0, :].sum(), o[-1, :].sum(),
                                  o[:, 0].sum(), o[:, -1].sum()))

    # 全空图不崩
    blank = np.full((80, 80), BG, np.uint8)
    p2 = os.path.join(tmp, "blank.png")
    Image.fromarray(blank, "L").save(p2)
    out2, info2 = crop_complete(p2, max_side=128)
    show("全空图不报错", info2["empty"] is True, str(out2.size))

    print()
    print("自检：%s" % ("通过" if ok else "有失败项"))
    return 0 if ok else 1


def make_sheet(items, path, cell=200, cols=5):
    rows = (len(items) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell * 2, rows * (cell + 16)), "white")
    dr = ImageDraw.Draw(sheet)
    for i, (src, dst, label) in enumerate(items):
        r, c = i // cols, i % cols
        x, y = c * cell * 2, r * (cell + 16) + 14
        a = Image.open(src).convert("RGB")
        a.thumbnail((cell - 8, cell - 8))
        sheet.paste(a, (x + 3, y))
        b = Image.open(dst).convert("RGB")
        b.thumbnail((cell - 8, cell - 8))
        sheet.paste(b, (x + cell + 3, y))
        dr.text((x + 3, y - 12), label[:28], fill="black")
    sheet.save(path)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(ROOT, "data", "motifs_bw"))
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "motifs_final"))
    ap.add_argument("--max-side", type=int, default=1024)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sheet", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not os.path.isdir(args.src):
        print("[失败] 找不到 %s" % args.src)
        return 2

    pairs = []
    n = 0
    aspects = []
    for cat in CATS:
        d = os.path.join(args.src, cat)
        if not os.path.isdir(d):
            continue
        o = os.path.join(args.out, cat)
        os.makedirs(o, exist_ok=True)
        files = sorted(f for f in os.listdir(d) if f.endswith(".png"))
        if args.limit:
            files = files[:args.limit]
        for f in files:
            src = os.path.join(d, f)
            dst = os.path.join(o, f)
            im, info = crop_complete(src, max_side=args.max_side)
            im.save(dst, optimize=True)
            n += 1
            if not info["empty"]:
                aspects.append(info["aspect"])
            if args.sheet and len(pairs) < 8:
                pairs.append((src, dst, "%s/%s" % (cat, os.path.splitext(f)[0])))
        print("  %-6s %d 张" % (cat, len(files)))
        if args.limit:
            break

    aspects.sort()
    print()
    print("共 %d 张" % n)
    if aspects:
        print("长宽比：中位 %.2f，10%%分位 %.2f，90%%分位 %.2f"
              % (aspects[len(aspects) // 2], aspects[int(len(aspects) * 0.1)],
                 aspects[int(len(aspects) * 0.9)]))
    if pairs:
        p = os.path.join(args.out, "_对照拼图.png")
        make_sheet(pairs, p)
        print("对照拼图：%s" % p)
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    raise SystemExit(main())
