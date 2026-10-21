# -*- coding: utf-8 -*-
"""把苗纹抠出来，并补齐成完整纹样。

和之前两版的区别
----------------
- 第一版（`motif_extract.extract`）只保最大几个连通块 + 形态学腐蚀 → 残缺
- 第二版（`extract_pattern_blocks`）干脆不抠，整幅保留 → 干净但没"抠"

这一版按需求来：**先抠，再补**。

抠：按颜色聚类分割。先用 k-means 把画面分成 k 类颜色，再把「贴着画面四条边
且占满边缘」的那几类判为底布，剩下的就是纹样。比"全局阈值"稳，
因为底布有渐变、纹样有多种颜色时，全局阈值一定两头不讨好。

补：三步
1. 形态学闭运算 —— 把被针脚切开的细缝连起来
2. 填内部空洞 —— 花瓣之间、叶脉之间的缝补实
3. 颜色修补 —— 补出来的区域用周围纹样颜色往里渗，不是留一块底布色

输出透明 PNG，纹样本体完整。

用法：
    python tools/extract_motif_complete.py --selftest
    python tools/extract_motif_complete.py --limit 8 --sheet     # 先看效果
    python tools/extract_motif_complete.py --max-side 512        # 全量
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

from motif_extract import trim_black_bars, checkerboard  # noqa: E402

DEFAULT_SRC = r"C:\Users\21165\WorkBuddy\2026-10-13-17-23-42\_aesthetic_work\basic"
CATS = ["byx", "dx", "mx", "szmwx", "qt"]

K = 7                 # 颜色聚类数
WORK = 320            # 分割在多大图上做
CLOSE_R = 3           # 闭运算半径（WORK 尺度下的像素）
MIN_SPEC = 0.0004     # 小于画面这个比例的碎点并进背景


# ------------------------------------------------------------------
# 分割
# ------------------------------------------------------------------
def _kmeans(pix, k=K, iters=12):
    """确定性 k-means：首点取均值，之后每次取离已有中心最远的点。

    不用随机初始化：同一张图两次跑要给出同样结果，否则没法复核。
    """
    cent = [pix.mean(axis=0)]
    for _ in range(k - 1):
        d = np.min(np.stack([((pix - c) ** 2).sum(axis=1) for c in cent]), axis=0)
        cent.append(pix[int(np.argmax(d))])
    cent = np.stack(cent)
    for _ in range(iters):
        d = ((pix[:, None, :] - cent[None, :, :]) ** 2).sum(axis=2)
        lab = np.argmin(d, axis=1)
        for j in range(k):
            m = lab == j
            if m.any():
                cent[j] = pix[m].mean(axis=0)
    d = ((pix[:, None, :] - cent[None, :, :]) ** 2).sum(axis=2)
    return np.argmin(d, axis=1), cent


def _border_ring(h, w, frac=0.06):
    r = max(2, int(round(min(h, w) * frac)))
    m = np.zeros((h, w), bool)
    m[:r, :] = m[-r:, :] = m[:, :r] = m[:, -r:] = True
    return m


def _mask_from_labels(lab, h, w):
    """判底布：**边缘上**占比够高的颜色类别，就是底布。

    注意方向：要看"画面边缘有多少像素属于这一类"，不是
    "这一类有多少像素落在边缘"。后者对大块底布永远是小的
    （底布会一直铺到画面中间），照那个判据底布永远判不出来，
    结果整幅都被当纹样（自检里 coverage=1.0 就是这么来的）。
    """
    ring = _border_ring(h, w)
    ring_lab = lab[ring]
    keep = np.ones(K, bool)
    for j in range(K):
        if not (lab == j).any():
            keep[j] = False
            continue
        share = float((ring_lab == j).mean())
        if share >= 0.12:            # 这一类占了边缘的 12% 以上 → 底布
            keep[j] = False
    # 全被判成底布时，退化为「保留面积最小的那类」——总有纹样
    if not keep.any():
        areas = [(lab == j).sum() for j in range(K)]
        keep[int(np.argmin(areas))] = True
    return keep[lab]


def _dilate(mask, r):
    m = mask.copy()
    for _ in range(r):
        n = m.copy()
        n[1:, :] |= m[:-1, :]
        n[:-1, :] |= m[1:, :]
        n[:, 1:] |= m[:, :-1]
        n[:, :-1] |= m[:, 1:]
        m = n
    return m


def _erode(mask, r):
    return ~_dilate(~mask, r)


def _fill_holes(mask):
    """从画面外框洪泛，外界到不了的空白就是内部空洞。"""
    h, w = mask.shape
    free = ~mask
    reach = np.zeros_like(free)
    reach[0, :] = free[0, :]
    reach[-1, :] = free[-1, :]
    reach[:, 0] = free[:, 0]
    reach[:, -1] = free[:, -1]
    for _ in range(max(h, w) + 4):
        n = reach.copy()
        n[1:, :] |= reach[:-1, :] & free[1:, :]
        n[:-1, :] |= reach[1:, :] & free[:-1, :]
        n[:, 1:] |= reach[:, :-1] & free[:, 1:]
        n[:, :-1] |= reach[:, 1:] & free[:, :-1]
        if np.array_equal(n, reach):
            break
        reach = n
    return mask | (free & ~reach)


def _inpaint(rgb, missing, iters=14):
    """把缺失区域用周围已知像素往里渗，避免补出来的地方是一块底布色。"""
    img = rgb.astype(np.float32).copy()
    known = ~missing
    for _ in range(iters):
        acc = np.zeros_like(img)
        cnt = np.zeros(known.shape, np.float32)
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            k = np.zeros_like(known)
            k[max(0, dy):known.shape[0] + min(0, dy),
              max(0, dx):known.shape[1] + min(0, dx)] = \
                known[max(0, -dy):known.shape[0] - max(0, dy),
                      max(0, -dx):known.shape[1] - max(0, dx)]
            a = np.zeros_like(img)
            a[max(0, dy):known.shape[0] + min(0, dy),
              max(0, dx):known.shape[1] + min(0, dx)] = \
                img[max(0, -dy):known.shape[0] - max(0, dy),
                    max(0, -dx):known.shape[1] - max(0, dx)]
            acc += a * k[..., None]
            cnt += k
        todo = (~known) & (cnt > 0)
        if not todo.any():
            break
        img[todo] = acc[todo] / cnt[todo][:, None]
        known |= todo
    return np.clip(img, 0, 255).astype(np.uint8)


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------
def extract_complete(path, max_side=512, margin=0.06):
    """返回 (RGBA Image, info dict)。"""
    arr = np.asarray(Image.open(path).convert("RGB"))
    arr, _ = trim_black_bars(arr)
    H, W = arr.shape[:2]

    # 小图上分割
    s = WORK / float(max(H, W)) if max(H, W) > WORK else 1.0
    ww, hh = max(16, int(W * s)), max(16, int(H * s))
    small = np.asarray(Image.fromarray(arr).resize((ww, hh), Image.BILINEAR))
    pix = small.reshape(-1, 3).astype(np.float32)
    lab, _cent = _kmeans(pix)
    m = _mask_from_labels(lab.reshape(hh, ww), hh, ww)

    # 抠 + 补
    m = _dilate(m, 1)
    m0 = m.copy()                      # 补之前的原始抠图结果，补色要以它为"已知"
    before = m.sum()
    m = _dilate(m, CLOSE_R)
    m = _fill_holes(m)
    m = _erode(m, CLOSE_R)
    filled = int(m.sum() - before)

    # 放大回原图
    mi = Image.fromarray((m * 255).astype(np.uint8)).resize((W, H), Image.NEAREST)
    full = np.asarray(mi) > 127

    ys, xs = np.nonzero(full)
    if len(ys) == 0:
        raise ValueError("没抠出任何纹样")
    pad_y = max(2, int((ys.max() - ys.min()) * margin))
    pad_x = max(2, int((xs.max() - xs.min()) * margin))
    y0, y1 = max(0, ys.min() - pad_y), min(H, ys.max() + pad_y + 1)
    x0, x1 = max(0, xs.min() - pad_x), min(W, xs.max() + pad_x + 1)

    cut = arr[y0:y1, x0:x1]
    alpha = full[y0:y1, x0:x1]

    # 补色：闭运算/填洞新纳入的区域，原图那里是**底布色**，
    # 直接留着会看到一块块底布补丁。做法是以"原始抠图结果"为已知像素，
    # 让周围纹样颜色往里渗。
    #
    # 注意已知像素只能是纹样本身：若把透明区也算已知，底布色会被渗进来，
    # 补出来的地方就是一块蓝布，等于没补。
    orig = np.asarray(
        Image.fromarray((m0 * 255).astype(np.uint8)).resize((W, H),
                                                            Image.NEAREST)
    )[y0:y1, x0:x1] > 127
    rgb_cut = cut.copy()
    if orig.any():
        med = np.median(cut[orig].reshape(-1, 3), axis=0)
        rgb_cut[~orig] = med.astype(np.uint8)
        rgb_cut = _inpaint(rgb_cut, ~orig, iters=12)

    rgba = np.dstack([rgb_cut, (alpha * 255).astype(np.uint8)])
    out = Image.fromarray(rgba, "RGBA")
    ch, cw = out.size[1], out.size[0]
    if max_side and max(cw, ch) > max_side:
        k = max_side / float(max(cw, ch))
        out = out.resize((max(1, int(cw * k)), max(1, int(ch * k))),
                         Image.LANCZOS)

    info = {"coverage": float(full.mean()), "filled_px": int(filled),
            "size": out.size}
    return out, info


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


def selftest():
    """已知答案：一圈花瓣 + 中间有洞 + 一道裂缝，抠出来必须补成实心。"""
    print("=== 自检 ===")
    ok = True

    def show(name, cond, extra=""):
        nonlocal ok
        ok = ok and cond
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))

    import tempfile
    tmp = tempfile.mkdtemp(prefix="complete_selftest_")
    n = 320
    yy, xx = np.mgrid[0:n, 0:n]
    rr = np.hypot(yy - n // 2, xx - n // 2)
    # 底布：深蓝；纹样：红环（中间空心）+ 一道竖直裂口
    img = np.zeros((n, n, 3), np.uint8)
    img[...] = (25, 35, 95)
    ring = (rr > 60) & (rr < 110)
    img[ring] = (205, 55, 45)
    img[120:200, 155:165] = (25, 35, 95)      # 裂缝
    p = os.path.join(tmp, "ring.png")
    Image.fromarray(img).save(p)

    out, info = extract_complete(p, max_side=320)
    a = np.asarray(out)[..., 3] > 127
    ys, xs = np.nonzero(a)
    cy, cx = (ys.min() + ys.max()) / 2, (xs.min() + xs.max()) / 2
    hole = a[int(cy) - 15:int(cy) + 15, int(cx) - 15:int(cx) + 15]
    show("中间的空洞被补实", hole.mean() > 0.95, "空洞填充率 %.2f" % hole.mean())
    show("竖直裂缝被补上",
         a[int(cy) - 5:int(cy) + 5, int(cx) - 5:int(cx) + 5].mean() > 0.9,
         "%.2f" % a[int(cy) - 5:int(cy) + 5, int(cx) - 5:int(cx) + 5].mean())
    show("抠出的覆盖率不为 0 也不是整幅", 0.05 < info["coverage"] < 0.95,
         "%.2f" % info["coverage"])

    print()
    print("自检：%s" % ("通过" if ok else "有失败项"))
    return 0 if ok else 1


def make_sheet(pairs, path, cell=200, cols=4):
    rows = (len(pairs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell * 2, rows * (cell + 18)), "white")
    dr = ImageDraw.Draw(sheet)
    for i, (src, out, label) in enumerate(pairs):
        r, c = i // cols, i % cols
        x, y = c * cell * 2, r * (cell + 18) + 14
        o = Image.open(src).convert("RGB")
        o.thumbnail((cell - 6, cell - 6))
        sheet.paste(o, (x + 3, y))
        t = out.convert("RGBA")
        t.thumbnail((cell - 12, cell - 12))
        bg = Image.fromarray(checkerboard(t.width, t.height, cell=10), "RGBA")
        bg.alpha_composite(t)
        sheet.paste(bg.convert("RGB"), (x + cell + 3, y))
        dr.text((x + 3, y - 12), label[:26], fill="black")
    sheet.save(path)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_SRC)
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "motifs_complete"))
    ap.add_argument("--max-side", type=int, default=512)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sheet", action="store_true")
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
    print("待处理 %d 张" % len(items))

    pairs, ok_n, bad = [], 0, []
    t0 = time.time()
    for i, (cat, path, fn) in enumerate(items, 1):
        try:
            out, info = extract_complete(path, max_side=args.max_side)
            d = os.path.join(args.out, cat)
            os.makedirs(d, exist_ok=True)
            out.save(os.path.join(d, os.path.splitext(fn)[0] + ".png"),
                     optimize=True)
            ok_n += 1
            if args.sheet and i <= 8:
                pairs.append((path, out, "%s/%s" % (cat, fn)))
        except Exception as e:
            bad.append((fn, str(e)[:60]))
        if i % 100 == 0:
            print("  %d/%d，用时 %.0f 秒" % (i, len(items), time.time() - t0))

    print()
    print("成功 %d / %d，用时 %.1f 分钟" % (ok_n, len(items),
                                           (time.time() - t0) / 60))
    if bad:
        print("失败 %d 张，前 3：" % len(bad))
        for fn, e in bad[:3]:
            print("  %s —— %s" % (fn, e))
    if pairs:
        p = os.path.join(args.out, "_对照拼图.png")
        make_sheet(pairs, p)
        print("对照拼图：%s" % p)
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    raise SystemExit(main())
