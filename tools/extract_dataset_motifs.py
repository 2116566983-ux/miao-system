# -*- coding: utf-8 -*-
"""把整个数据集的纹样批量提取成透明 PNG。

复用的是 ④ 提取纹样 页那套 `motif_extract.extract()`，不是另写一份算法 ——
页面上怎么抠，这里就怎么抠，保证「批量结果」和「学生手动点出来的」一致。

输出：
    <out>/<类别>/<原文件名>_motif.png     透明底纹样
    metadata/纹样提取清单.csv             每张的成功/失败、占比、外接框、尺寸

失败不是错误：底色杂乱、主体太小的图本来就不该硬抠。清单里会写清原因。

用法：
    python tools/extract_dataset_motifs.py --selftest          # 先测通路
    python tools/extract_dataset_motifs.py                     # 全量
    python tools/extract_dataset_motifs.py --sheet 30          # 另出一张拼图供人眼复核
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from motif_extract import extract, checkerboard  # noqa: E402

DEFAULT_SRC = r"C:\Users\21165\WorkBuddy\2026-10-13-17-23-42\_aesthetic_work\basic"
CATS = ["byx", "dx", "mx", "szmwx", "qt"]


def square_pad(path, max_side=1024):
    """把抠出来的图补成正方形并限尺寸。

    为什么要方：这批图抠出来的是**整块布片**（满地绣，没有"地布/花样"的
    二元结构，所以抠不出单朵花）。布片本身是斜的、长短不一，
    直接当素材用会很难排版。补成正方形后可以直接当图块拼。
    透明部分保留，方便叠在任何底色上。
    """
    from PIL import Image
    im = Image.open(path).convert("RGBA")
    w, h = im.size
    side = max(w, h)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(im, ((side - w) // 2, (side - h) // 2), im)
    if side > max_side:
        canvas = canvas.resize((max_side, max_side), Image.LANCZOS)
    canvas.save(path)
    return canvas.size


def collect(src):
    """按类别收集图片。类别目录不存在时也支持平铺目录。"""
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


def selftest():
    """用三张已知答案的合成图验证通路：纯色底上的形状必须被抠出来。"""
    import numpy as np
    from PIL import Image
    import tempfile

    print("=== 通路自检 ===")
    ok = True

    def show(name, cond, extra=""):
        nonlocal ok
        ok = ok and cond
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))

    tmp = tempfile.mkdtemp(prefix="extract_selftest_")
    n = 320
    yy, xx = np.mgrid[0:n, 0:n]

    # 深蓝底布 + 一个亮红圆盘（模拟「布 + 花」）
    base = np.zeros((n, n, 3), np.uint8)
    base[..., 0], base[..., 1], base[..., 2] = 30, 40, 90
    disc = np.hypot(yy - n // 2, xx - n // 2) < 90
    base[disc] = (200, 60, 50)
    p = os.path.join(tmp, "dish.png")
    Image.fromarray(base).save(p)
    r = extract(p, out_dir=tmp)
    show("纯色底上的圆盘能被提取", bool(r.get("ok")),
         r.get("message", "")[:36])
    if r.get("ok"):
        cov = r.get("coverage", 0)
        show("提取占比接近真实面积（0.2~0.32）", 0.2 <= cov <= 0.32,
             "%.3f" % cov)
        show("落盘了透明 PNG", os.path.exists(r.get("saved") or ""),
             os.path.basename(r.get("saved") or ""))

    # 纯色图：没有纹样可提，应当明确失败而不是硬返回一块
    flat = np.zeros((n, n, 3), np.uint8)
    flat[..., 0], flat[..., 1], flat[..., 2] = 120, 120, 120
    p2 = os.path.join(tmp, "flat.png")
    Image.fromarray(flat).save(p2)
    r2 = extract(p2, out_dir=tmp)
    show("无主体的纯色图被判失败", not r2.get("ok"), r2.get("message", "")[:36])

    print()
    print("自检：%s" % ("通过" if ok else "有失败项"))
    return 0 if ok else 1


def make_sheet(rows, path, n=24, cell=180, seed=7):
    """抽若干张已提取的透明图，铺棋盘格拼成一张，供人眼判断抠得对不对。

    必须用棋盘格：不少苗绣的花瓣本身是白线，白底上会看成"花瓣被挖空"。
    """
    from PIL import Image
    good = [r for r in rows if r["ok"] and r.get("out")]
    if not good:
        return None
    random.seed(seed)
    pick = random.sample(good, min(n, len(good)))
    cols = 6
    rowsn = (len(pick) + cols - 1) // cols
    sheet = Image.new("RGBA", (cols * cell, rowsn * (cell + 16)), "white")
    from PIL import ImageDraw
    dr = ImageDraw.Draw(sheet)
    for i, r in enumerate(pick):
        im = Image.open(r["out"]).convert("RGBA")
        im.thumbnail((cell - 10, cell - 10))
        bg = Image.fromarray(checkerboard(im.width, im.height, cell=12), "RGBA")
        bg.alpha_composite(im)
        x = (i % cols) * cell + 5
        y = (i // cols) * (cell + 16) + 14
        sheet.paste(bg, (x, y))
        dr.text((x, y - 12), r["file"][:26], fill="black")
    sheet.convert("RGB").save(path)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_SRC, help="数据集目录（含五类子目录）")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "motifs_dataset"))
    ap.add_argument("--manifest", default=os.path.join(
        ROOT, "metadata", "纹样提取清单.csv"))
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 张（调试用）")
    ap.add_argument("--sheet", type=int, default=0, help="另外生成 N 张的复核拼图")
    ap.add_argument("--no-square", action="store_true",
                    help="不补成正方形，保留原始裁剪")
    ap.add_argument("--max-side", type=int, default=1024)
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
    print("待处理 %d 张，输出到 %s" % (len(items), args.out))

    rows = []
    t0 = time.time()
    for i, (cat, path, fn) in enumerate(items, 1):
        out_dir = os.path.join(args.out, cat)
        try:
            r = extract(path, out_dir=out_dir)
        except Exception as e:
            r = {"ok": False, "message": "异常：%s" % e}
        saved = r.get("saved") or ""
        if saved and not args.no_square and os.path.exists(saved):
            try:
                square_pad(saved, args.max_side)
            except Exception as e:
                print("   [警告] 补方失败 %s：%s" % (fn, e))
        rows.append({
            "cat": cat,
            "file": "%s/%s" % (cat, fn),
            "ok": bool(r.get("ok")),
            "coverage": round(float(r.get("coverage") or 0), 4),
            "bbox": (str(r.get("bbox")) if r.get("bbox") else ""),
            "size": ("%dx%d" % tuple(r["size"])) if r.get("size") else "",
            "out": r.get("saved") or "",
            "message": (r.get("message") or "")[:80],
        })
        if i % 100 == 0:
            print("  已处理 %d/%d，用时 %.0f 秒" % (i, len(items), time.time() - t0))

    os.makedirs(os.path.dirname(args.manifest), exist_ok=True)
    with io.open(args.manifest, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["cat", "file", "ok", "coverage",
                                          "bbox", "size", "out", "message"])
        w.writeheader()
        w.writerows(rows)

    ok = [r for r in rows if r["ok"]]
    bad = [r for r in rows if not r["ok"]]
    print()
    print("=== 结果 ===")
    print("成功 %d / %d（%.0f%%），失败 %d"
          % (len(ok), len(rows), 100.0 * len(ok) / len(rows), len(bad)))
    print("用时 %.1f 分钟" % ((time.time() - t0) / 60.0))
    if ok:
        covs = sorted(r["coverage"] for r in ok)
        print("纹样占画面比例：中位 %.0f%%，最小 %.0f%%，最大 %.0f%%"
              % (covs[len(covs) // 2] * 100, covs[0] * 100, covs[-1] * 100))
    print()
    print("按类别：")
    for cat in sorted({r["cat"] for r in rows}):
        sub = [r for r in rows if r["cat"] == cat]
        s = sum(1 for r in sub if r["ok"])
        print("  %-6s %3d/%3d 成功（%.0f%%）" % (cat, s, len(sub),
                                                100.0 * s / len(sub)))
    if bad:
        print()
        print("失败原因（前 5 条）：")
        for r in bad[:5]:
            print("  %s —— %s" % (r["file"], r["message"]))

    if args.sheet:
        p = os.path.join(ROOT, "data", "motifs_dataset", "_复核拼图.png")
        if make_sheet(rows, p, n=args.sheet):
            print()
            print("复核拼图：%s" % p)
    print()
    print("清单：%s" % args.manifest)
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    raise SystemExit(main())
