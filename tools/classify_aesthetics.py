# -*- coding: utf-8 -*-
"""苗绣纹样「数学美学」分类：对称美 / 流线型 / 黄金分割 + 产品形态倒推。

为什么会有这个脚本
------------------
2026-10-19 腾讯会议确定的分类要求：

    「从传统文化和美学角度，结合**对称美、流线型、黄金分割**等数学逻辑，
      对苗绣纹样进行拆解、提取与分类。」
    「先明确最终要呈现的商业作品形态（包包、钥匙扣、潮玩娃娃），
      再**倒推**需要提取和组合的纹样主题与大小。」

数据包里 10-17 已经做好的两级分类（艺术五类 + 两级配色）回答了
"它是什么纹样、什么配色"；本脚本补的是**几何与构图**那一维。

第一版踩过的坑（别再犯）
------------------------
1. 对称度直接量整幅画面 → 全体接近 0。因为这些图是绣品**照片裁片**：
   主体不居中、四周带黑边、还有斜切条带。对称的是**纹样主体**，
   不是相框。所以必须先按前景密度「拆解」出主体区域，再在主体上量。
   这正是会议原文「对苗绣纹样进行**拆解**、提取与分类」的含义。
2. 周期性用一维均值剖面自相关 → 84% 被误判成连续重复。
   光滑信号的自相关在中长滞后上天然虚高，测出来的是"平滑度"
   而不是"重复"。改成二维归一化互相关（去均值），并要求滞后
   至少 8 像素，光滑图会因标准差过小被直接判零。

环境里没有 scipy / cv2 / skimage，大津阈值、自相关、连通域全是
numpy + PIL 手写。改阈值前先读 a_metrics() 的注释。

用法：
    python tools/classify_aesthetics.py --selftest   # 先验证算法没写反
    python tools/classify_aesthetics.py --root <basic图目录>
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import os
import sys

import numpy as np
from PIL import Image, ImageFilter

ROOT_DEFAULT = r"C:\Users\21165\WorkBuddy\2026-10-13-17-23-42\_aesthetic_work\basic"

CAT_CN = {
    "byx": "花卉植物纹",
    "dx": "几何对称纹",
    "mx": "动物神兽纹",
    "szmwx": "卷草涡纹",
    "qt": "其他",
}

# 参与计算的最大边长。原图分辨率不齐，颗粒度差好几倍，直接算出来的
# 曲率、周期这些"以像素为单位"的指标没法横向比较。统一缩放后才行。
MAX_SIDE = 512
# 主体区域上做对称计算时的模糊半径。绣线织纹是高频噪声，会彻底
# 淹没构图级的对称；这个半径保留"图案布局"而抹掉"每一针"。
MOTIF_BLUR = 6


# ------------------------------------------------------------------
# 基础工具
# ------------------------------------------------------------------
def load_arrays(path, max_side=MAX_SIDE):
    """读图并降采样，返回 (灰度 0-1, RGB 0-1, 原始尺寸(w,h))。

    原始尺寸要一起带出来：后面算「主体在原图里有多少像素」时，
    降采样后的像素数是没有意义的，必须换回原图尺度。
    """
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        s = max_side / float(max(w, h))
        if s < 1.0:
            im = im.resize((max(1, int(w * s)), max(1, int(h * s))),
                           Image.BILINEAR)
        rgb = np.asarray(im, dtype=np.float32) / 255.0
    gray = (0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1]
            + 0.114 * rgb[:, :, 2])
    return gray, rgb, (w, h)


def blur(a, radius):
    """灰度图盒模糊（PIL 自带，比手写快）。a 为 0-1 浮点。"""
    if radius <= 0:
        return a
    return np.asarray(
        Image.fromarray((np.clip(a, 0, 1) * 255).astype(np.uint8)).filter(
            ImageFilter.BoxBlur(radius)), dtype=np.float32) / 255.0


def otsu_thr(vals):
    """大津阈值（自己写 —— 环境里没有 cv2）。vals 是 0-1 的浮点数组。"""
    hist, edges = np.histogram(vals, bins=256, range=(0.0, 1.0))
    hist = hist.astype(np.float64)
    total = hist.sum()
    if total == 0:
        return 0.5
    w0 = np.cumsum(hist)
    w1 = total - w0
    mid = (edges[:-1] + edges[1:]) / 2.0
    cum = np.cumsum(hist * mid)
    m0 = np.where(w0 > 0, cum / np.maximum(w0, 1e-9), 0.0)
    m1 = np.where(w1 > 0, (cum[-1] - cum) / np.maximum(w1, 1e-9), 0.0)
    valid = (w0 > 0) & (w1 > 0)
    if not valid.any():
        return 0.5
    var = w0 * w1 * (m0 - m1) ** 2
    var[~valid] = -1.0
    return float(edges[int(np.argmax(var)) + 1])


def sym_score(g, flipped):
    """归一化残差对称度：完全对称→1，无序→0 附近。

    分母取「相对自身均值的平均偏差」并压下限 0.02：
    几乎纯色的一块布算不出对称，不能让除式把它夸大成 1.0。
    """
    num = float(np.abs(g - flipped).mean())
    den = max(float(np.abs(g - g.mean()).mean()), 0.02)
    return float(np.clip(1.0 - num / den, 0.0, 1.0))


def densest_band(density, min_frac=0.35, smooth=9):
    """一维密度剖面里最"实"的那段连续区间，返回 (lo, hi)。

    这是"拆解主体"的关键：主体通常在某个方向上聚成一带，
    两边是留白或辅料。找不到足够窄的带（密度铺满全轴）就返回全轴，
    表示"这张图本身就是纹样单元"。
    """
    n = density.size
    if n < 16:
        return 0, n
    k = smooth | 1
    pad = np.pad(density, k // 2, mode="edge")
    prof = np.convolve(pad, np.ones(k) / k, mode="valid")[:n]
    hi_thr = prof.max() * min_frac
    on = prof >= hi_thr
    if not on.any():
        return 0, n
    # 找最长连续段
    best_lo = best_len = 0
    lo = None
    for i, v in enumerate(on):
        if v and lo is None:
            lo = i
        elif not v and lo is not None:
            if i - lo > best_len:
                best_lo, best_len = lo, i - lo
            lo = None
    if lo is not None and n - lo > best_len:
        best_lo, best_len = lo, n - lo
    # 带宽占全轴 85% 以上 = 没有明显主体边界，不裁
    if best_len >= 0.85 * n or best_len < 8:
        return 0, n
    return best_lo, best_lo + best_len


def motif_crop(gray, mask):
    """按前景密度拆出主体区域，返回 (裁剪灰度, 裁剪mask, (x0,x1,y0,y1))。

    横竖各找一个最实带，交集即主体所在。这是整张流程里
    最能影响对称指标的一步 —— 主体不居中的照片，不裁就永远测不出对称。
    """
    if mask.sum() < 16:
        return gray, mask, (0, gray.shape[1], 0, gray.shape[0])
    x0, x1 = densest_band(mask.mean(axis=0))
    y0, y1 = densest_band(mask.mean(axis=1))
    if (x1 - x0) < 16 or (y1 - y0) < 16:
        return gray, mask, (0, gray.shape[1], 0, gray.shape[0])
    return (gray[y0:y1, x0:x1], mask[y0:y1, x0:x1], (x0, x1, y0, y1))


def ncc_repeat(gray, min_lag=8, hp_radius=16, size=128):
    """找重复单元，返回 (强度, 滞后像素, 轴向说明)。

    三步预处理，每一步都是被真实数据教训出来的：

    1. **先高通**（减去大半径盒模糊）。光照渐变、整体明暗这类低频成分
       会让任何两张图"看起来都相关"，不去掉就会把纯渐变判成周期图。
    2. **再裁边**。盒模糊在边界用镜像填充，线性渐变在边界会留下残差，
       这层残差自己和自己完全相关 —— 原型测试里就是它把纯渐变图
       顶到了 1.00。裁掉 hp_radius 宽的一圈，边界残差就没了。
    3. **判方向有没有信息**。竖条图案沿纵向完全恒定，纵向平移任意距离
       都完全重合，相关系数 1.0 —— 那是"这一维没信息"，不是"重复"。
       用相邻行/列的差分均值来判断，而不是用均值剖面的方差：
       棋盘格的列均值恒为 0.5（明暗相间自己抵消了），用均值判会
       把真正的四方连续误杀。
    """
    a = blur(gray, hp_radius)
    hp = gray - a
    r = hp_radius
    if hp.shape[0] > 3 * r and hp.shape[1] > 3 * r:
        hp = hp[r:-r, r:-r]

    im = Image.fromarray((np.clip(hp, 0, 1) * 255).astype(np.uint8))
    im = im.resize((size, size), Image.BILINEAR)
    a = np.asarray(im, dtype=np.float32) / 255.0
    a = a - a.mean()
    if float((a * a).sum()) < 1e-6:
        return 0.0, 0, ""
    h, w = a.shape

    row_change = float(np.abs(np.diff(a, axis=0)).mean())
    col_change = float(np.abs(np.diff(a, axis=1)).mean())
    allow_y = row_change > 0.002
    allow_x = col_change > 0.002

    best, blag, baxis = 0.0, 0, ""

    def corr(dy, dx):
        b = a[dy:, dx:]
        t = a[:h - dy, :w - dx] if (dy or dx) else a
        if b.size < 0.2 * a.size:            # 重叠太小，相关系数不可信
            return -1.0
        tb = b - b.mean()
        tt = t - t.mean()
        d = math.sqrt(float((tb * tb).sum()) * float((tt * tt).sum()))
        return float((tb * tt).sum() / d) if d > 1e-9 else -1.0

    lo = min_lag
    if allow_x:
        for dx in range(lo, w // 2, 2):                 # 横向二方连续
            c = corr(0, dx)
            if c > best:
                best, blag, baxis = c, dx, "横向%dpx" % dx
    if allow_y:
        for dy in range(lo, h // 2, 2):                 # 纵向
            c = corr(dy, 0)
            if c > best:
                best, blag, baxis = c, dy, "纵向%dpx" % dy
    if allow_x and allow_y:
        for dy in range(lo, h // 2, 4):                 # 四方连续
            for dx in range(lo, w // 2, 4):
                c = corr(dy, dx)
                if c > best:
                    best, blag, baxis = c, dx, "四方%dx%d" % (dx, dy)
    return max(0.0, best), blag, baxis


def radial_sym(gray, na=180, nr=48):
    """放射（旋转）对称：团花有几瓣。返回 (强度, 瓣数)。

    为什么必须补这一维
    ------------------
    苗绣团花是**放射**构图，不是镜像。把实物照片做镜像差异（见
    _aesthetic_work/mirror_diag.png）能直接看到：差异图本身呈对称的
    蝴蝶纹，说明布局是绕中心转出来的，不是左右对折出来的。
    只量镜像会把整批团花判成"不对称"，那是尺子错了，不是纹样错了。

    做法：取边缘幅值当结构图（消掉明暗与配色差异），以结构能量重心
    为极点做极坐标采样，得到"每个角度的径向剖面"，再看它旋转
    2π/k 后与自己的相关性。相关性在某个 k 上取得峰值，那个 k 就是瓣数。
    """
    h, w = gray.shape
    if h < 32 or w < 32:
        return 0.0, 0
    mag = blur(direction_field(gray)[0], 3)
    total = float(mag.sum())
    if total <= 1e-9:
        return 0.0, 0
    ys, xs = np.mgrid[0:h, 0:w]
    cy = float((ys * mag).sum() / total)
    cx = float((xs * mag).sum() / total)
    R = 0.45 * min(h, w)
    if R < 8:
        return 0.0, 0

    th = np.linspace(0.0, 2 * np.pi, na, endpoint=False)
    rr = np.linspace(0.15, 1.0, nr)
    X = np.rint(cx + np.outer(np.cos(th), rr) * R).astype(int)
    Y = np.rint(cy + np.outer(np.sin(th), rr) * R).astype(int)
    ok = (X >= 0) & (X < w) & (Y >= 0) & (Y < h)
    P = np.where(ok, mag[np.clip(Y, 0, h - 1), np.clip(X, 0, w - 1)], np.nan)
    prof = np.nan_to_num(np.nanmean(P, axis=1))
    if float(prof.std()) < 1e-9:
        return 0.0, 0

    cs = {}
    for k in range(2, 9):                     # 2~8 瓣，苗绣团花基本在这个范围
        sh = na // k
        a, b = prof[:na - sh], prof[sh:]
        if a.std() < 1e-9 or b.std() < 1e-9:
            continue
        cs[k] = float(np.corrcoef(a, b)[0, 1])
    if not cs:
        return 0.0, 0

    # 取"达标的**最大** k"，不是取相关性最高的那个。
    #
    # 6 重对称必然同时是 2 重和 3 重对称（6 是它们的倍数），
    # 直接取 argmax 会稳定地退到 k=2，把六瓣团花说成两瓣。
    # 正确做法是：先把接近最高值的 k 都选出来，再取其中最大的。
    top = max(cs.values())
    if top < 0.60:          # 连最高的一档都不到 0.6，就是没有放射对称，别硬报瓣数
        return max(0.0, top), 0
    cut = max(0.60, top * 0.90)
    fold = max(k for k, c in cs.items() if c >= cut)
    return max(0.0, cs[fold]), fold


# 各产品「纹样区成品尺寸 + 印刷精度」假设（把数字写死是**假设**，不是测量结果）：
#   钥匙扣：纹样区 3.5cm，按 300dpi 印刷 → 需要 3.5/2.54*300 ≈ 413 像素
#   潮玩：贴图区 12cm，按 200dpi       → 需要 12/2.54*200  ≈ 945 像素
#   包袋：图案单元 10cm，按 150dpi      → 需要 10/2.54*150  ≈ 591 像素
# 换产品规格时只改这张表，判定逻辑不动。
PRODUCT_SPEC = {
    "钥匙扣徽章": {"cm": 3.5, "dpi": 300},
    "潮玩娃娃摆件": {"cm": 12.0, "dpi": 200},
    "包袋面料": {"cm": 10.0, "dpi": 150},
}


def required_px(product):
    spec = PRODUCT_SPEC.get(product)
    if not spec:
        return 0
    return int(round(spec["cm"] / 2.54 * spec["dpi"]))


def size_verdict(motif_px, product):
    """主体像素够不够撑起这件产品的成品尺寸。

    为什么不报"厘米"：这批图是实物照片，画面里**没有尺度标尺**，
    一张 600px 的图可能对应 5cm 也可能对应 50cm，逐图给厘米是编数。
    试过两条路都不成立：来回缩放的相关系数太宽容（缩到两成还有 0.86，
    因为它测的是整体结构不是细节）；零交叉密度被绣线织纹的高频噪声
    顶住（真实照片缩到两成比值仍在 1.1 左右）。两处都记在文档里。

    能算准的是**主体在原图里有多少像素** —— 它直接决定做小件会不会糊，
    而且完全来自文件本身，不需要任何尺度假设。
    """
    need = required_px(product)
    if not need or not motif_px:
        return ""
    ratio = motif_px / float(need)
    if ratio >= 1.0:
        return "够"
    if ratio >= 0.7:
        return "勉强"
    return "不够"


def direction_field(gray):
    """返回 (梯度幅值, 单模方向场, 双角余弦, 双角正弦)。

    单模方向：角度取模 π，因为一条线的 0° 和 180° 是同一条线。
    双角 (2θ)：cos2θ/sin2θ 在 θ 跨过 π 边界时连续，
    用它求方向场的空间变化率就不会被 ±π 跳变污染。
    """
    g = blur(gray, 1.5)
    gy, gx = np.gradient(g)
    mag = np.hypot(gx, gy)
    ang = np.arctan2(gy, gx) % np.pi
    return mag, ang, np.cos(2 * ang), np.sin(2 * ang)


# ------------------------------------------------------------------
# 单张图全指标
# ------------------------------------------------------------------
def a_metrics(path):
    gray, rgb, (orig_w, orig_h) = load_arrays(path)

    # ---- 前景掩膜：背景色取四边框像素中位数（这些图多带黑边/衬布）----
    border = np.concatenate([
        rgb[0, :, :].reshape(-1, 3), rgb[-1, :, :].reshape(-1, 3),
        rgb[:, 0, :].reshape(-1, 3), rgb[:, -1, :].reshape(-1, 3),
    ])
    bg = np.median(border, axis=0)
    dist = np.abs(rgb - bg).max(axis=2)
    mask = dist > otsu_thr(dist.ravel())
    fill = float(mask.mean())

    # ---- 拆解主体，再在主体上量对称 ----
    mg, mmask, (x0, x1, y0, y1) = motif_crop(gray, mask)
    mb = blur(mg, MOTIF_BLUR)
    lr = sym_score(mb, np.fliplr(mb))
    ud = sym_score(mb, np.flipud(mb))
    r180 = sym_score(mb, np.rot90(mb, 2))
    sym_mirror = max(lr, ud, r180)

    # 镜像对称用裁剪后的主体；放射对称必须用全图。
    # 带宽裁剪是按横竖密度切的，会把花瓣切掉一块，转起来当然对不上 ——
    # 而 radial_sym 自己会按能量重心找极点，不需要预先裁剪。
    radial, fold = radial_sym(gray)

    # 尺寸倒推：这张纹样缩到几成还看得清
    # 主体在原图里有多少像素 —— 注意要把降采样倍数乘回去，
    # 否则算出来的是 512 缩放图上的像素数，跟原图分辨率无关，判"够不够"毫无意义。
    sx = orig_w / float(gray.shape[1]) if gray.shape[1] else 1.0
    sy = orig_h / float(gray.shape[0]) if gray.shape[0] else 1.0
    motif_px = int(round(min((x1 - x0) * sx, (y1 - y0) * sy)))
    sym_best = max(sym_mirror, radial)

    # ---- 平移重复：整幅图上找（重复是画面级的性质）----
    periodic, period_px, period_axis = ncc_repeat(gray)

    # ---- 流线型：方向熵 + 方向角场变化率 ----
    mag, ang, c2, s2 = direction_field(gray)
    thr = float(np.percentile(mag, 85)) if mag.size else 0.0
    edge = mag > max(thr, 1e-6)
    nbins = 24
    if edge.any():
        hist, _ = np.histogram(ang[edge], bins=nbins, range=(0.0, np.pi),
                               weights=mag[edge])
    else:
        hist = np.zeros(nbins)
    tot = hist.sum()
    if tot > 1e-9:
        p = hist / tot
        nz = p[p > 0]
        entropy = float(-(nz * np.log(nz)).sum() / math.log(nbins))
        top_share = float(hist.max() / tot)
    else:
        entropy, top_share = 0.0, 1.0

    if edge.sum() > 2:
        dc = np.abs(np.diff(c2, axis=1))[:-1, :] \
            + np.abs(np.diff(c2, axis=0))[:, :-1]
        ds = np.abs(np.diff(s2, axis=1))[:-1, :] \
            + np.abs(np.diff(s2, axis=0))[:, :-1]
        m = edge[:-1, :-1]
        angular_var = float((dc + ds)[m].mean()) if m.any() else 0.0
    else:
        angular_var = 0.0

    # ---- 主体几何 ----
    #
    # 注意分工：对称要在**主体**上量（上一段已裁），
    # 而黄金分割量的是**整幅构图**里主体的占比与位置，必须在全幅上量。
    # 第一版把黄金比也放到裁剪后的主体上算，裁完占比恒为 1.0，
    # golden_fill 直接被算成 0 —— 自检里那条断言就是这么抓出来的。
    if mask.any():
        ys, xs = np.nonzero(mask)
        cx = float(xs.mean()) / max(mask.shape[1] - 1, 1)
        cy = float(ys.mean()) / max(mask.shape[0] - 1, 1)
    else:
        cx = cy = 0.5
    mw = float(x1 - x0) / max(gray.shape[1], 1)
    mh = float(y1 - y0) / max(gray.shape[0], 1)
    mf = float(mmask.mean()) if mmask.size else fill

    # ---- 黄金分割 ----
    # 主体占整幅画面的比例贴近 0.382 / 0.618 得高分
    # （0.382 是 0.618 的余量，同一个分割关系的两面）
    d_fill = min(abs(fill - 0.382), abs(fill - 0.618))
    golden_fill = float(np.clip(1.0 - d_fill / 0.382, 0.0, 1.0))
    d_c = min(abs(cx - 0.382), abs(cx - 0.618),
              abs(cy - 0.382), abs(cy - 0.618))
    golden_center = float(np.clip(1.0 - d_c / 0.382, 0.0, 1.0))
    golden = 0.6 * golden_fill + 0.4 * golden_center

    return {
        "sym_lr": lr, "sym_ud": ud, "sym_rot180": r180,
        "sym_mirror": sym_mirror, "sym_radial": radial,
        "rot_fold": int(fold) if radial >= 0.35 else 0,
        "sym_best": sym_best,
        "motif_w": round(mw, 3), "motif_h": round(mh, 3),
        "motif_fill": round(mf, 3),
        "motif_px": motif_px, "orig_w": orig_w, "orig_h": orig_h,
        "periodic": periodic, "period_px": int(period_px),
        "period_axis": period_axis,
        "dir_entropy": entropy, "top_share": top_share,
        "angular_var": angular_var,
        "fill": fill, "cx": cx, "cy": cy,
        "golden_fill": golden_fill, "golden_center": golden_center,
        "golden": golden,
    }


# ------------------------------------------------------------------
# 标签规则
# ------------------------------------------------------------------
def tercile(v, arr_sorted):
    """v 在这批数值里的百分位（0-1）。"""
    a = np.asarray(arr_sorted, dtype=np.float64)
    return float((a <= v).mean())


def band(t):
    return "高" if t >= 2.0 / 3.0 else ("中" if t >= 1.0 / 3.0 else "低")


def sym_label(r, sym_hi=0.60, rep_hi=0.55, rep_strong=0.75,
              rad_hi=0.50, rad_fold=3):
    """对称类型。

    判定顺序就是落地优先级：
      1. 强重复        —— 能直接铺面料，最值钱
      2. 放射对称      —— 团花，做成圆形徽章/挂饰天然合适
      3. 镜像对称      —— 左右/上下/中心
      4. 自由式        —— 三者都不成立

    第 2 条必须同时满足「强度够」和「有明确瓣数≥3」。
    只卡强度会漏进大量假阳性：2 重旋转几乎任何布局都凑得上，
    而"某一档相关性没到 0.6"时 rot_fold=0、强度却可能因为
    曲线整体起伏而偏高 —— 抽检 12 张里有一半是普通绣片，就是这么来的。
    """
    if r["periodic"] >= rep_strong or (
            r["periodic"] >= rep_hi and r["periodic"] >= r["sym_best"]):
        return "连续重复"
    if (r["sym_radial"] >= rad_hi and r["rot_fold"] >= rad_fold
            and r["sym_radial"] >= r["sym_mirror"]):
        return "放射对称"
    if r["sym_mirror"] >= sym_hi:
        if r["sym_lr"] == r["sym_mirror"]:
            return "左右对称"
        if r["sym_ud"] == r["sym_mirror"]:
            return "上下对称"
        return "中心对称"
    return "自由式"


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT_DEFAULT,
                    help="basic 原图目录（下含 byx/dx/mx/szmwx/qt 五个子目录）")
    ap.add_argument("--out", default=None,
                    help="输出 CSV，默认 metadata/美学数学分类.csv")
    ap.add_argument("--selftest", action="store_true",
                    help="用已知答案的合成图验证算法没写反")
    args = ap.parse_args()

    PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_csv = args.out or os.path.join(PROJ, "metadata", "美学数学分类.csv")

    if args.selftest:
        return selftest()

    if not os.path.isdir(args.root):
        print("[失败] 找不到图目录：%s" % args.root)
        print("       用 --root 指定，或先把包里的 basic 原图抽出来")
        return 2

    cats = [d for d in sorted(os.listdir(args.root))
            if os.path.isdir(os.path.join(args.root, d)) and d in CAT_CN]
    if not cats:
        print("[失败] %s 下没有 byx/dx/mx/szmwx/qt 任一目录" % args.root)
        return 2

    rows = []
    for cat in cats:
        d = os.path.join(args.root, cat)
        files = sorted(f for f in os.listdir(d)
                       if f.lower().endswith((".jpg", ".jpeg", ".png")))
        print("%-6s %-6s %d 张" % (cat, CAT_CN[cat], len(files)))
        for i, fn in enumerate(files, 1):
            try:
                m = a_metrics(os.path.join(d, fn))
            except Exception as e:            # 坏图不该让整批崩掉
                print("   [跳过] %s/%s —— %s" % (cat, fn, e))
                continue
            m["cat"] = cat
            m["art_class"] = CAT_CN[cat]
            m["file"] = "%s/%s" % (cat, fn)
            rows.append(m)
            if i % 250 == 0:
                print("   已算 %d/%d" % (i, len(files)))
    print("合计 %d 张" % len(rows))
    if not rows:
        print("[失败] 一张都没算出来")
        return 2

    # ---- 反向自检：指标挤成一条线就说明算坏了 ----
    print()
    print("=== 分布自检（标准差接近 0 就是算坏了）===")
    for k in ["sym_mirror", "sym_radial", "sym_best", "periodic",
              "dir_entropy", "angular_var", "fill", "golden"]:
        arr = np.asarray([r[k] for r in rows], dtype=np.float64)
        print("  %-12s 均值 %.3f  标准差 %.3f  最小 %.3f  最大 %.3f"
              % (k, arr.mean(), arr.std(), arr.min(), arr.max()))
        if arr.std() < 0.01:
            print("  [FAIL] %s 几乎没有离散度，先跑 --selftest" % k)
            return 1

    ents = sorted(r["dir_entropy"] for r in rows)
    vars_ = sorted(r["angular_var"] for r in rows)
    golds = sorted(r["golden"] for r in rows)
    syms = sorted(r["sym_best"] for r in rows)

    for r in rows:
        r["sym_label"] = sym_label(r)
        # 分类标签之外再给一个强度分级：本批实物照片里严格对称的是少数，
        # 只用类别会让 82% 都叫"自由式"，这一维就排不出高低了。
        # 分级按批内三分位，保证每一档都真的有图。
        r["sym_grade"] = band(tercile(r["sym_best"], syms))
        # 流线强度 = 方向分散度 0.6 + 曲率 0.4
        r["curl_tercile"] = tercile(r["dir_entropy"], ents) * 0.6 \
            + tercile(r["angular_var"], vars_) * 0.4
        r["stream_label"] = band(r["curl_tercile"])
        r["golden_label"] = band(tercile(r["golden"], golds))
        r["sym_tercile"] = tercile(r["sym_best"], syms)

        # ---- 产品倒推 ----
        # 规则显式可改；每一个分数对应一句人话，别当黑箱。
        # 对称取镜像与放射两者的较大值 —— 团花靠的是放射，不是照镜子。
        sym_ok = np.clip(r["sym_best"] / 0.60, 0, 1)
        rep_ok = np.clip(r["periodic"] / 0.70, 0, 1)          # 能平铺
        even_fill = np.clip(1 - abs(r["fill"] - 0.45) / 0.45, 0, 1)
        focus = np.clip(1 - abs(r["motif_fill"] - 0.60) / 0.60, 0, 1)
        crisp = np.clip(1 - r["dir_entropy"], 0, 1)
        narrative = 1.0 if r["cat"] == "mx" else (
            0.6 if r["cat"] in ("byx", "qt") else 0.35)

        s_bag = 0.55 * rep_ok + 0.45 * even_fill
        s_key = 0.45 * sym_ok + 0.35 * focus + 0.20 * crisp
        s_toy = 0.45 * narrative + 0.35 * r["curl_tercile"] + 0.20 * sym_ok

        rank = sorted([("包袋面料", s_bag), ("钥匙扣徽章", s_key),
                       ("潮玩娃娃摆件", s_toy)], key=lambda kv: -kv[1])
        r["score_bag"], r["score_key"] = round(s_bag, 3), round(s_key, 3)
        r["score_toy"] = round(s_toy, 3)
        r["product"], r["product_runner"] = rank[0][0], rank[1][0]
        # 尺寸倒推：这件纹样按它推荐的产品看，缩小后还认不认得出
        r["size_verdict"] = size_verdict(r["motif_px"], r["product"])

    fields = ["cat", "art_class", "file",
              "sym_mirror", "sym_lr", "sym_ud", "sym_rot180",
              "sym_radial", "rot_fold", "sym_best", "sym_grade",
              "sym_label",
              "periodic", "period_px", "period_axis",
              "motif_w", "motif_h", "motif_fill", "motif_px",
              "dir_entropy", "top_share", "angular_var", "curl_tercile",
              "stream_label", "fill", "cx", "cy",
              "golden_fill", "golden_center", "golden", "golden_label",
              "score_bag", "score_key", "score_toy",
              "product", "product_runner", "size_verdict"]
    os.makedirs(os.path.dirname(out_csv), exist_ok=True)
    with io.open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (round(r[k], 4) if isinstance(r.get(k), float)
                            else r.get(k, "")) for k in fields})
    print()
    print("已写出：%s" % os.path.relpath(out_csv, PROJ))
    return summarize(rows)


def summarize(rows):
    from collections import Counter

    print()
    print("=== 分类结果摘要（n=%d）===" % len(rows))
    for key, name in [("sym_label", "对称型"),
                      ("stream_label", "流线强度"),
                      ("golden_label", "黄金比贴合度"),
                      ("product", "推荐落地产品"),
                      ("size_verdict", "尺寸适配")]:
        c = Counter(r[key] for r in rows)
        print("  %-8s：" % name, "，".join(
            "%s %d(%.0f%%)" % (k, v, 100.0 * v / len(rows))
            for k, v in c.most_common()))

    print()
    print("  按艺术类别看：")
    print("    %-12s %-5s %-9s %-8s %-7s %-7s"
          % ("类目", "张数", "连续重复", "对称均值", "流线均值", "黄金均值"))
    for cat in sorted(CAT_CN):
        sub = [r for r in rows if r["cat"] == cat]
        if not sub:
            continue
        rep = sum(1 for r in sub if r["sym_label"] == "连续重复") / len(sub)
        print("    %-12s %-5d %-9s %-8.2f %-7.2f %-7.2f"
              % (CAT_CN[cat], len(sub), "%.0f%%" % (rep * 100),
                 np.mean([r["sym_best"] for r in sub]),
                 np.mean([r["curl_tercile"] for r in sub]),
                 np.mean([r["golden"] for r in sub])))
    return 0


def selftest():
    """用已知答案的合成图验证算法方向没写反。

    "跑完没报错"什么也证明不了。除了基本方向，还必须覆盖第一版
    翻车的两个场景：主体不居中的对称图（框级测不出、主体级必须测出）、
    光滑无重复的图（自相关不许虚报周期）。
    """
    print("=== 合成图自检 ===")
    import tempfile

    def show(name, cond, extra=""):
        nonlocal ok
        ok = ok and cond
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))

    ok = True
    tmp = tempfile.mkdtemp(prefix="aes_selftest_")
    n = 256
    yy, xx = np.mgrid[0:n, 0:n]

    def save_rgb(name, arr):
        p = os.path.join(tmp, name)
        Image.fromarray((np.clip(arr, 0, 1) * 255).astype(np.uint8),
                        "L").convert("RGB").save(p)
        return p

    # -- 对称（主体居中，最理想情形）--
    half = np.zeros((n, n), np.float32)
    half[:, :n // 2] = (yy[:, :n // 2] * 7 + xx[:, :n // 2] * 3) % 255 / 255.
    m = a_metrics(save_rgb("lr.png",
                           np.hstack([half[:, :n // 2],
                                      np.fliplr(half[:, :n // 2])])))
    show("居中左右对称 sym_lr ≈ 1", m["sym_lr"] > 0.97, "%.3f" % m["sym_lr"])

    top = np.zeros((n, n), np.float32)
    top[:n // 2, :] = (yy[:n // 2, :] * 5 + xx[:n // 2, :] * 11) % 255 / 255.
    m = a_metrics(save_rgb("ud.png",
                           np.vstack([top[:n // 2, :],
                                      np.flipud(top[:n // 2, :])])))
    show("居中上下对称 sym_ud ≈ 1", m["sym_ud"] > 0.97, "%.3f" % m["sym_ud"])

    # -- 关键回归：对称主体偏在左上角，相框级测不出，主体级必须测出 --
    off = np.zeros((n, n), np.float32)
    s = 90
    blk = (yy[:s, :s] * 9 + xx[:s, :s] * 4) % 255 / 255.
    blk = np.hstack([blk, np.fliplr(blk)])            # 90x180 左右对称块
    off[20:20 + s, 20:20 + 2 * s] = blk
    m = a_metrics(save_rgb("off.png", off))
    show("偏置对称图主体级 sym_lr ≈ 1", m["sym_lr"] > 0.97, "%.3f" % m["sym_lr"])

    # -- 周期 --
    bars = np.zeros((n, n), np.float32)
    bars[:, ::16] = 1.0
    m = a_metrics(save_rgb("bars.png", bars))
    show("16px 竖条周期被识别",
         m["periodic"] > 0.6 and m["period_axis"].startswith("横向"),
         "强度 %.2f %s" % (m["periodic"], m["period_axis"]))

    checker = ((xx // 16 + yy // 16) % 2).astype(np.float32)
    m = a_metrics(save_rgb("checker.png", checker))
    show("棋盘格判为重复", sym_label(m) == "连续重复",
         "%s 强度 %.2f" % (m["period_axis"], m["periodic"]))

    # -- 光滑无重复图不许虚报周期 --
    grad = np.tile(np.linspace(0, 1, n), (n, 1)).astype(np.float32)
    m = a_metrics(save_rgb("grad.png", grad))
    show("光滑渐变图不误报周期", m["periodic"] < 0.5, "%.2f" % m["periodic"])

    # -- 噪声 --
    rng = np.random.default_rng(42)
    m = a_metrics(save_rgb("noise.png", rng.random((n, n)).astype(np.float32)))
    show("随机噪声对称度 < 0.3", m["sym_best"] < 0.3, "%.3f" % m["sym_best"])
    show("随机噪声不误报周期", m["periodic"] < 0.5, "%.2f" % m["periodic"])
    noise_entropy = m["dir_entropy"]

    # -- 流线 --
    vert = np.tile((np.arange(n) % 12 < 1).astype(np.float32), (n, 1))
    mv = a_metrics(save_rgb("vert.png", vert))
    show("纯直线图方向熵低于随机图", mv["dir_entropy"] < noise_entropy,
         "直线 %.3f < 随机 %.3f" % (mv["dir_entropy"], noise_entropy))

    ring = ((np.hypot(yy - n / 2, xx - n / 2) % 18) < 1.5).astype(np.float32)
    mr = a_metrics(save_rgb("ring.png", ring))
    show("同心圆环角变化率高于直线图", mr["angular_var"] > mv["angular_var"],
         "环 %.4f > 直线 %.4f" % (mr["angular_var"], mv["angular_var"]))

    # -- 黄金分割 --
    side = int(n * math.sqrt(0.382))
    fg = np.zeros((n, n), np.float32)
    fg[:side, :side] = 1.0
    mg = a_metrics(save_rgb("golden.png", fg))
    show("前景占 38.2% 时 golden_fill ≈ 1",
         mg["golden_fill"] > 0.9 and abs(mg["fill"] - 0.382) < 0.03,
         "fill %.3f golden_fill %.3f" % (mg["fill"], mg["golden_fill"]))

    # -- 放射对称：六瓣团花必须被认出 6 瓣 --
    ang = np.arctan2(yy - n / 2, xx - n / 2)
    rad = np.hypot(yy - n / 2, xx - n / 2)
    petals = ((np.sin(6 * ang) > 0.35) & (rad < 0.42 * n)
              & (rad > 0.08 * n)).astype(np.float32)
    mp = a_metrics(save_rgb("petals6.png", petals))
    show("六瓣团花判为 6 瓣放射对称",
         mp["rot_fold"] == 6 and sym_label(mp) == "放射对称",
         "瓣数 %d 强度 %.2f 标签 %s"
         % (mp["rot_fold"], mp["sym_radial"], sym_label(mp)))

    # -- 尺寸倒推：主体像素够不够做小件 --
    #
    # 满幅主体（占满 256px）做钥匙扣（需 413px）应当判"不够"，
    # 但把同一张图放大到 1024px 后重新算就够 —— 这证明判定跟着原图分辨率走，
    # 而不是被 512 的降采样尺度锁死。
    palette = (((xx // 64) + (yy // 64)) % 2).astype(np.float32)
    mc = a_metrics(save_rgb("coarse.png", palette))
    show("256px 满幅主体做钥匙扣判不够",
         size_verdict(mc["motif_px"], "钥匙扣徽章").startswith("不够"),
         "主体 %d px，需 %d px"
         % (mc["motif_px"], required_px("钥匙扣徽章")))

    big = np.kron(palette, np.ones((4, 4), np.float32))   # 256 → 1024
    mb = a_metrics(save_rgb("coarse_big.png", big))
    show("放大到 1024px 后同一纹样做钥匙扣判够",
         size_verdict(mb["motif_px"], "钥匙扣徽章").startswith("够"),
         "主体 %d px，需 %d px"
         % (mb["motif_px"], required_px("钥匙扣徽章")))

    print()
    print("自检结果：%s" % ("全部通过" if ok else "有失败项，先修算法再跑全量"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    raise SystemExit(main())
