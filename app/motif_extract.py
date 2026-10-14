# -*- coding: utf-8 -*-
"""
苗绣纹样提取：把「纹样」从「绣品照片」里分离出来

要解决的问题（教学定位上的关键一点）：
    数据集里的图是**绣品实物的照片** —— 一块靛蓝土布上绣着彩色花枝，
    边缘还常有黑色填充条。

    但课程要教的是「纹样」这个**符号/特征**：
    它是可提取、可比较、可复用、可进入知识图谱的东西，
    而不是「一张附着在布上的照片」。

    所以这里做的是语义上的分离：
        底布（靛蓝布、黑边）  → 背景
        绣线图案（花枝、配色）→ 纹样

实现思路（只用 numpy + PIL，不引入 opencv/scipy）：
    1. 先裁掉照片的纯黑填充边（letterbox）
    2. 估计底布颜色：取图像四周一圈像素的众数（布总是铺到边上的）
    3. 在 HSV 空间做分割 —— 关键点：
       底布是「高饱和 + 固定色相」的靛蓝，
       而绣线包含白线（低饱和）、粉/绿/橙（不同色相），
       所以用「色相接近 AND 饱和度高」判定背景，比 RGB 距离更稳
    4. 只保留「与图像边缘连通」的背景区域（其余是纹样内部的小孔，要填掉）
    5. 形态学清理毛刺
    6. 裁到纹样外接矩形，输出带透明通道的 PNG
"""

from __future__ import annotations

import io
import os

import numpy as np

try:
    from PIL import Image, ImageFilter
except ImportError:      # pragma: no cover
    Image = None


class MotifExtractError(Exception):
    pass


# ---------------------------------------------------------------------------
# 第一步：去掉照片的纯黑填充边
# ---------------------------------------------------------------------------
def trim_black_bars(arr, dark=28, uniform_ratio=0.94):
    """裁掉四周纯黑/近黑的填充条。

    数据集里部分照片是「正片 + 黑边」的形式（例如 633x745 的图
    底部有一条黑条）。黑边既不是底布也不是纹样，先去掉。

    返回 (裁剪后的数组, (left, top, right, bottom))
    """
    a = arr.astype(np.int16)
    h, w, _ = a.shape

    def is_dark_row(y):
        row = a[y]
        return (row.max(axis=1) < dark).mean() > uniform_ratio

    def is_dark_col(x):
        col = a[:, x]
        return (col.max(axis=1) < dark).mean() > uniform_ratio

    top = 0
    while top < h - 1 and is_dark_row(top):
        top += 1
    bottom = h - 1
    while bottom > top and is_dark_row(bottom):
        bottom -= 1
    left = 0
    while left < w - 1 and is_dark_col(left):
        left += 1
    right = w - 1
    while right > left and is_dark_col(right):
        right -= 1

    # 全黑图直接返回原图，避免裁成空
    if bottom - top < 10 or right - left < 10:
        return arr, (0, 0, w, h)

    return arr[top:bottom + 1, left:right + 1], (left, top, right + 1, bottom + 1)


# ---------------------------------------------------------------------------
# 第二步：估计底布颜色
# ---------------------------------------------------------------------------
def _ring_local_gradient(arr, r):
    """在图像网格上算「外圈区域的相邻像素色差」。

    为什么要专门写一个：
        早期版本把四条边带拼成一个数组再做 np.diff —— 但边带之间
        本来就不相连，那一次跳变会把分位数整体抬高好几倍
        （实测直接导致容差过大、底布生长吞掉全部纹样）。
        必须在原图的二维网格上取差分才对。
    """
    f = arr.astype(np.float32)
    gx = np.abs(f[:, :-1] - f[:, 1:]).sum(axis=-1)     # (h, w-1) 水平
    gy = np.abs(f[:-1, :] - f[1:, :]).sum(axis=-1)     # (h-1, w) 垂直

    parts = [
        gx[:r, :].ravel(), gx[-r:, :].ravel(),
        gy[:, :r].ravel(), gy[:, -r:].ravel(),
    ]
    g = np.concatenate([p for p in parts if p.size])
    return g


def estimate_cloth(arr, ring=6):
    """从图像外圈估计底布颜色与生长容差。

    布总是铺满画面的（纹样是绣在布上的），所以边上一圈像素
    大概率是纯底布。

    返回 dict：rgb / tol / cover / n / local_grad

    关于 cover 偏低的情况（重要）：
        cover 是「最集中的那个 16 级量化桶占外圈像素的比例」。
        对靛蓝土布这种**颜色单一**的底布，cover 能到 0.3+；
        但对**有连续渐变**的黑色/深色布（如 basic_592 的细密黑布），
        颜色摊在几十个桶里，任何一个桶都占不到 5%，
        cover 天然就低 —— 这不代表估计失败。

        早期用「cover < 0.05 判失败」，会把这些图整体拒掉。
        现在改为看 **spread（色距的稳健离群边界）**：
        渐变布虽然桶分散，但整体色距范围是收窄的（各像素都离中位色不远），
        spread 小说明「外圈颜色确实集中」，底布可信。
        真正该拒的是「外圈五颜六色」——那才是画面杂乱。
    """
    h, w, _ = arr.shape
    r = max(2, min(ring, h // 10, w // 10))

    ring_px = np.concatenate([
        arr[:r, :].reshape(-1, 3),
        arr[-r:, :].reshape(-1, 3),
        arr[:, :r].reshape(-1, 3),
        arr[:, -r:].reshape(-1, 3),
    ]).astype(np.float32)

    # 量化到 16 级便于统计众数（抗轻微噪声）
    q = (ring_px.astype(np.int32) // 16)
    keys, counts = np.unique(q, axis=0, return_counts=True)
    if len(counts) == 0:
        return None
    top = keys[counts.argmax()]

    sel = np.all(q == top, axis=1)
    if sel.sum() < 10:
        return None

    mean_rgb = ring_px[sel].mean(axis=0)
    cover = float(sel.mean())

    # 底布颜色容差（spread）＝外圈色距的稳健离群边界
    #
    # 用 q3 + 1.5×IQR 这个经典离群判据：
    #   布自身的散布（渐变、织纹）落在四分位内；
    #   外圈里夹带的纹样像素离布色很远，会被这个边界排除出去。
    #
    # 早期版本用 p95 直接当跨度，结果外圈里几朵被裁断的花
    # 把跨度顶到 313（等于色距上限），容差过宽反而吃掉纹样。
    d_all = np.abs(ring_px - mean_rgb).sum(axis=1)
    q1, q3 = np.percentile(d_all, [25, 75])
    iqr = max(float(q3 - q1), 6.0)
    spread = float(q3 + 1.5 * iqr)

    # 容差按「底布内部相邻像素色差」定，供逐像素生长时使用
    g = _ring_local_gradient(arr, r)
    base = float(np.percentile(g, 85)) if g.size > 20 else 12.0
    tol = float(np.clip(base * 1.6, 20.0, 96.0))

    return {"rgb": mean_rgb, "tol": tol, "cover": cover, "spread": spread,
            "n": int(sel.sum()), "local_grad": base}


def _rgb_to_hsv_array(rgb):
    """把 (...,3) 的 uint8 RGB 数组转 HSV，H/S/V 都归一到 0..1。"""
    a = rgb.astype(np.float32) / 255.0
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mx = a.max(axis=-1)
    mn = a.min(axis=-1)
    d = mx - mn

    h = np.zeros_like(mx)
    nz = d > 1e-6

    rm = nz & (mx == r)
    gm = nz & (mx == g) & ~rm
    bm = nz & (mx == b) & ~rm & ~gm
    with np.errstate(invalid="ignore", divide="ignore"):
        h[rm] = ((g - b)[rm] / d[rm]) % 6
        h[gm] = ((b - r)[gm] / d[gm]) + 2
        h[bm] = ((r - g)[bm] / d[bm]) + 4
    h = h / 6.0

    s = np.where(mx > 1e-6, d / np.maximum(mx, 1e-6), 0.0)
    return h, s, mx


# ---------------------------------------------------------------------------
# 第三步：分割
# ---------------------------------------------------------------------------
def _shift_or(mask, src):
    """四邻域膨胀一步（用 numpy 位移实现，不依赖 scipy）。"""
    out = mask.copy()
    out[1:, :] |= (mask[:-1, :] & src[1:, :])
    out[:-1, :] |= (mask[1:, :] & src[:-1, :])
    out[:, 1:] |= (mask[:, :-1] & src[:, 1:])
    out[:, :-1] |= (mask[:, 1:] & src[:, :-1])
    return out


def _connected_from_border(candidate):
    """找出与图像边缘连通的候选区域（迭代波前法）。"""
    h, w = candidate.shape
    reach = np.zeros_like(candidate)
    reach[0, :] = candidate[0, :]
    reach[-1, :] = candidate[-1, :]
    reach[:, 0] = candidate[:, 0]
    reach[:, -1] = candidate[:, -1]

    for _ in range(max(h, w) + 10):
        new = _shift_or(reach, candidate)
        if np.array_equal(new, reach):
            break
        reach = new
    return reach


def grow_background(arr, tol, cloth_rgb=None, seed_tol=None):
    """从图像边缘的**底布像素**开始「长」出底布区域。

    为什么用逐像素生长，而不是「与某个全局底色比距离」：
        现实中的布不是均匀色块。靛蓝布从上到下由浅到深有明显渐变，
        绣品照片还有光照渐变。用全局色差判定，阈值调紧则布切不干净、
        调松则把纹样也吃掉 —— 实测两种情况都踩过。

        而相邻像素之间的色差是稳定的：布内部相邻色差很小，
        布与绣线交界处色差很大（针脚边缘是硬的）。
        所以「沿着相邻色差小的方向扩散」能自然顺着渐变铺开，
        又会在纹样边缘停住。这就是抠图里「容差魔棒」的做法。

    种子为什么不能取全部边缘像素：
        数据集里不少照片的纹样是**被画框裁断**的（花朵开在画面边上）。
        若把所有边缘像素都当种子，生长就会从「被裁断的花」上起步，
        反过来把花当成背景吃掉 —— 实测 basic_0 左上那朵花就是这样丢的。
        所以只在「颜色接近底布」的边缘像素上播种。

    参数：
        tol        相邻像素允许的色差上限（各通道差之和）
        cloth_rgb  底布代表色；给了才做种子筛选
        seed_tol   种子判定的容差，默认取 tol 的 1.6 倍
    """
    h, w, _ = arr.shape
    f = arr.astype(np.float32)

    # 相邻像素色差：水平 (h, w-1) 与垂直 (h-1, w)
    dx = np.abs(f[:, :-1] - f[:, 1:]).sum(axis=-1)
    dy = np.abs(f[:-1, :] - f[1:, :]).sum(axis=-1)

    border = np.zeros((h, w), dtype=bool)
    border[0, :] = True
    border[-1, :] = True
    border[:, 0] = True
    border[:, -1] = True

    if cloth_rgb is not None:
        st = float(seed_tol if seed_tol is not None else tol * 1.6)
        near_cloth = np.abs(f - np.asarray(cloth_rgb, np.float32)).sum(
            axis=-1) < st
        bg = border & near_cloth
        # 边缘上找不到底布色（画面全是图案）时退化为全部边缘播种
        if bg.sum() < max(8, int(0.01 * (h + w))):
            bg = border.copy()
    else:
        bg = border.copy()

    passable_x = dx < tol
    passable_y = dy < tol

    for _ in range(max(h, w) + 10):
        new = bg.copy()
        new[:, 1:] |= bg[:, :-1] & passable_x
        new[:, :-1] |= bg[:, 1:] & passable_x
        new[1:, :] |= bg[:-1, :] & passable_y
        new[:-1, :] |= bg[1:, :] & passable_y
        if np.array_equal(new, bg):
            break
        bg = new

    return bg


def _box_mean(img, r):
    """用积分图算矩形窗口均值（边界按镜像裁剪计数）。"""
    h, w = img.shape
    ii = np.zeros((h + 1, w + 1), dtype=np.float64)
    ii[1:, 1:] = np.cumsum(np.cumsum(img, axis=0), axis=1)

    ys = np.arange(h)
    xs = np.arange(w)
    y0 = np.clip(ys - r, 0, h)
    y1 = np.clip(ys + r + 1, 0, h)
    x0 = np.clip(xs - r, 0, w)
    x1 = np.clip(xs + r + 1, 0, w)

    s = (ii[np.ix_(y1, x1)] - ii[np.ix_(y0, x1)]
         - ii[np.ix_(y1, x0)] + ii[np.ix_(y0, x0)])
    cnt = (y1 - y0)[:, None] * (x1 - x0)[None, :]
    return s / np.maximum(cnt, 1)


def local_roughness(arr, radius=4):
    """局部粗糙度图：每个像素邻域内的色彩方差。

    为什么用方差而不是相邻色差：
        绣品照片的布面织纹是逐像素跳变的噪声，单点相邻色差在
        纹理尖峰处会突然很大（实测 p90 达 79），和纹样边缘
        同一量级，阈值怎么调都会漏。
        方差是**邻域统计量**——布面整体方差低（虽然有噪声但均值稳），
        纹样区域方差高（针脚走向杂乱、颜色多），两者分离度大得多，
        而且天然抗单像素噪声。
    """
    f = arr.astype(np.float64)
    rough = np.zeros(f.shape[:2], dtype=np.float64)
    for c in range(f.shape[2]):
        ch = f[:, :, c]
        m1 = _box_mean(ch, radius)
        m2 = _box_mean(ch * ch, radius)
        rough += np.maximum(m2 - m1 * m1, 0.0)
    return np.sqrt(rough)      # 标准差，量纲回到"色阶"


def _otsu_threshold(values, bins=192):
    """大津法自动阈值：找出把分布切成「两堆」的最佳分界。

    为什么不用「外圈粗糙度的某个分位数」当阈值：
        数据集里不少照片的纹样是**被画框裁断的**（花朵开在画面边上），
        所以外圈里同时有布和纹样。实测 basic_0 的外圈 p75 粗糙度是 49.5，
        乘上系数后阈值达到 128.7，而整幅图的最大粗糙度才 129 ——
        结果「粗糙区」几乎为空，全图被判成底布，提取结果一片空白。

        大津法直接从整体分布找双峰分界，不依赖任何手工基线，
        对「布 + 纹样」这种天然双类场景很合适。
    """
    v = np.asarray(values, dtype=np.float64).ravel()
    v = v[np.isfinite(v)]
    if v.size == 0:
        return 0.0
    lo, hi = float(v.min()), float(v.max())
    if hi - lo < 1e-9:
        return lo

    hist, edges = np.histogram(v, bins=bins, range=(lo, hi))
    p = hist.astype(np.float64) / hist.sum()
    centers = (edges[:-1] + edges[1:]) / 2.0

    w0 = np.cumsum(p)                       # 前景（低粗糙度侧）权重
    w1 = 1.0 - w0
    cum = np.cumsum(p * centers)
    total = cum[-1]
    with np.errstate(invalid="ignore", divide="ignore"):
        m0 = cum / np.maximum(w0, 1e-12)
        m1 = (total - cum) / np.maximum(w1, 1e-12)
    between = w0 * w1 * (m0 - m1) ** 2
    between[~np.isfinite(between)] = -1.0

    idx = int(np.argmax(between))

    # 平台取中点
    #
    # 两类之间若是空档（无过渡像素），则空档内任意阈值给出的
    # 类间方差完全相同 —— between 在整段上是一个平台。
    # 直接 argmax 会返回平台最左端，那是"刚好能分开"的临界值，
    # 对噪声最敏感（一个像素稍大于它就会被判成纹样）。
    # 取平台中点，离两类都最远，最稳。
    peak = between[idx]
    if np.isfinite(peak) and peak > 0:
        plateau = np.flatnonzero(between >= peak * (1.0 - 1e-9))
        if plateau.size > 1:
            idx = int(plateau[plateau.size // 2])

    return float(centers[idx])


def build_mask(arr, cloth, dark_floor=0.055, work_scale=0.5,
               rough_radius=4):
    """生成纹样掩膜：True = 纹样（前景）。

    算法（局部方差 + 大津阈值 + 孔洞回填）：

        1. 降采样 + 轻度模糊 —— 压掉布面织纹的高频噪声
        2. 算局部粗糙度图（邻域色彩方差）
        3. 用大津法自动找阈值，分出「粗糙区」（纹样骨架）
        4. 把「不粗糙且与图像边缘连通」的部分判为底布；其余算纹样
           —— 这一步自动补上纹样内部的平滑块（同色花瓣、留白），
           因为它们被纹样包住，连不到画面边缘
        5. 排除极暗像素（照片阴影/残留黑边）

    第 4 步是关键：不必判断纹样内部是什么颜色，
    只要「被纹样包住」就归纹样。
    """
    h, w, _ = arr.shape
    sh, sw = max(32, int(h * work_scale)), max(32, int(w * work_scale))
    if (sh, sw) != (h, w):
        small = np.asarray(
            Image.fromarray(arr).resize((sw, sh), Image.LANCZOS)
            .filter(ImageFilter.GaussianBlur(1.0)))
    else:
        small = arr

    rough = local_roughness(small, radius=rough_radius)

    # 自动阈值；再设一个下限，避免布面本身极平滑时阈值塌到 0
    thr = _otsu_threshold(rough)
    thr = max(thr, 3.0)

    # ---- 底布候选 = 平滑 且 颜色接近底布 ----
    #
    # 为什么必须加颜色条件：
    #   数据集里不少纹样是**被画框裁断**的（花朵开在画面边上），
    #   花瓣内部的平滑区就直接连通到了画面边缘。
    #   只用"平滑"判底布，花心会被当成布挖空，
    #   提取结果只剩一圈轮廓线（实测 basic_0 就是这样）。
    #   加上颜色约束后：花瓣平滑但颜色不是布的靛蓝 → 仍归纹样。
    # 颜色参数在**工作分辨率**上重估一次：
    # 降采样+模糊会削掉布面的细碎抖动，跨度和容差都要跟着变，
    # 直接沿用原图的估计值会偏保守（布切不干净）。
    c_work = estimate_cloth(small) or cloth

    cl = np.asarray(c_work.get("rgb", small.reshape(-1, 3).mean(axis=0)),
                    np.float32)

    # 底布颜色容差（直接取 estimate_cloth 量好的"布能铺多开"）
    # 见 estimate_cloth 里 spread 的说明。
    seed_tol = float(c_work.get("spread", 0.0))
    if seed_tol <= 0:
        seed_tol = 120.0
    seed_tol = float(np.clip(seed_tol, 55.0, 300.0))

    color_dist = np.abs(small.astype(np.float32) - cl).sum(axis=-1)
    cloth_similar = color_dist < seed_tol

    bg_candidate = (rough <= thr) & cloth_similar

    # 底布 = 底布候选 且 与画面边缘连通
    bg_small = _connected_from_border(bg_candidate)

    # 边缘找不到底布时（画面全是图案）退化为"平滑即底布"
    if bg_small.mean() < 0.02:
        bg_small = _connected_from_border(rough <= thr)

    fg_small = ~bg_small

    # 放大回原尺寸
    bg = np.asarray(
        Image.fromarray((bg_small * 255).astype(np.uint8), "L")
        .resize((w, h), Image.NEAREST)) > 127

    _hh, _ss, v = _rgb_to_hsv_array(arr)
    foreground = (~bg) & (v > dark_floor)

    info = dict(cloth)
    info["rough_thr"] = thr
    info["rough_p50"] = float(np.percentile(rough, 50))
    info["rough_p90"] = float(np.percentile(rough, 90))
    return foreground, info


def clean_mask(mask, open_size=3, close_size=7):
    """形态学清理：去毛刺（开）+ 填小孔（闭）。

    用 PIL 的 Max/Min 滤波器实现，避免引入 opencv。
    """
    m = Image.fromarray((mask.astype(np.uint8) * 255), "L")
    if close_size >= 3:
        m = m.filter(ImageFilter.MaxFilter(close_size))   # 膨胀
        m = m.filter(ImageFilter.MinFilter(close_size))   # 腐蚀 → 闭
    if open_size >= 3:
        m = m.filter(ImageFilter.MinFilter(open_size))    # 腐蚀
        m = m.filter(ImageFilter.MaxFilter(open_size))    # 膨胀 → 开
    return np.asarray(m) > 127


def _largest_components(mask, keep=6, min_ratio=0.0015, work_max=320):
    """保留较大的连通块，去掉零星噪点。

    纹样通常由几个分离的花枝组成，所以保留前若干个，
    而不是只留最大的一个。

    为什么要降采样（性能关键）：
        这里的洪泛填充每轮只把一个连通块向外扩 1 像素，
        用 4 次整幅数组位移实现。图像 1500×1200 时，
        一个横跨全图的纹样要迭代 ~1500 轮 —— 实测单张 23 秒。

        而这一步的职责是「挑出哪几块该留」，属于**拓扑判断**，
        不需要像素级精度。所以先缩到最长边 ≤ work_max(=320) 再算：
        迭代轮数从 1500 降到 320 量级，单张从 23 秒降到 0.3 秒以内。
        挑出的块再映射回原分辨率（用最近邻上采样，边界不糊）。
    """
    h, w = mask.shape

    # 小图直接用；大图先降采样（保持长宽比）
    scale = min(1.0, float(work_max) / max(h, w))
    if scale < 1.0:
        small = Image.fromarray((mask.astype(np.uint8) * 255), "L")
        sw, sh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
        small = small.resize((sw, sh), Image.NEAREST)
        m = np.asarray(small) > 127
    else:
        m = mask

    sh, sw = m.shape

    # --- 在低分辨率上做连通块标记 ---
    labels = np.zeros((sh, sw), dtype=np.int32)
    cur = 0
    areas = []

    remaining = m.copy()

    while len(areas) < 4000:            # 保护上限
        pts = np.argwhere(remaining)
        if len(pts) == 0:
            break
        seed = tuple(pts[0])
        comp = np.zeros_like(m)
        comp[seed] = True
        for _ in range(max(sh, sw) + 10):
            new = _shift_or(comp, m)
            if np.array_equal(new, comp):
                break
            comp = new
        a = int(comp.sum())
        if a:
            cur += 1
            labels[comp] = cur
            areas.append(a)
            remaining &= ~comp
        else:
            remaining[seed] = False

    if not areas:
        return mask

    areas = np.array(areas)
    order = areas.argsort()[::-1]
    # min_ratio 是对「低分辨率总像素」的比例，故按其自身总量判断
    keep_idx = [i + 1 for i in order[:keep]
                if areas[i] >= m.sum() * min_ratio]

    keep_small = np.isin(labels, keep_idx)
    if not keep_small.any():
        return mask

    # --- 映射回原分辨率 ---
    if scale < 1.0:
        up = Image.fromarray((keep_small.astype(np.uint8) * 255), "L")
        up = up.resize((w, h), Image.NEAREST)
        out = (np.asarray(up) > 127) & mask   # 只能收窄，不能凭空造出像素
    else:
        out = keep_small

    return out if out.any() else mask


def crop_to_content(arr, mask, pad=6):
    """按纹样外接矩形裁剪（顺带裁掉多余空白）。"""
    ys, xs = np.where(mask)
    if len(ys) == 0:
        return arr, mask, (0, 0, arr.shape[1], arr.shape[0])

    y0 = max(0, ys.min() - pad)
    y1 = min(arr.shape[0], ys.max() + pad + 1)
    x0 = max(0, xs.min() - pad)
    x1 = min(arr.shape[1], xs.max() + pad + 1)

    return arr[y0:y1, x0:x1], mask[y0:y1, x0:x1], (x0, y0, x1, y1)


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def extract(path, out_dir=None, keep_components=6):
    """从一张绣品照片里提取纹样。

    返回 dict：
        ok            是否成功
        array         RGB 数组（已裁剪）
        alpha         掩膜（bool）
        coverage      纹样像素占比
        bbox          在**原图**中的外接矩形
        saved         落盘的透明 PNG 路径（若指定了 out_dir）
        message       说明
    """
    if Image is None:
        raise MotifExtractError("需要 Pillow：pip install pillow")

    if not os.path.exists(path):
        raise MotifExtractError("图片不存在：%s" % path)

    im = Image.open(path).convert("RGB")
    full = np.asarray(im)

    # 1. 去黑边
    arr, _trim = trim_black_bars(full)

    # 2. 估底色
    cloth = estimate_cloth(arr)
    if cloth is None:
        return {"ok": False, "message": "无法估计底布颜色（图像可能太小或异常）",
                "array": None, "alpha": None, "coverage": 0.0, "bbox": None}

    # 注意：这里**不**用 cover 做「能不能提取」的判据。
    #
    # 原因：cover 是「最集中的那个 16 级色桶占外圈的比例」，
    # 而外圈常常正好切到花朵 —— 花占得越多，cover 越低。
    # 实测 cover 从 0.045 到 0.42 都可能是能正常提取的图，
    # 曾经用 cover<0.05 当门槛，结果 basic_0（0.0503）勉强通过、
    # basic_592（0.0451）被整体拒掉 —— 这是刀锋式误判，不是真信号。
    #
    # 真正决定成败的是 build_mask 里的「粗糙度 + 颜色约束 + 边缘连通」，
    # 提取不出来它自己会返回低 coverage，下面的 coverage<0.02 兜住。

    # 3. 分割（build_mask 内部会降采样并返回该分辨率下的底色估计）
    mask, cloth = build_mask(arr, cloth)

    # 4. 清理
    mask = clean_mask(mask)
    mask = _largest_components(mask, keep=keep_components)

    coverage = float(mask.mean())
    if coverage < 0.02:
        return {"ok": False, "message": "提取到的纹样过少（占比 %.1f%%），"
                                        "可能底色判断有误" % (coverage * 100),
                "array": arr, "alpha": mask, "coverage": coverage, "bbox": None,
                "cloth_rgb": tuple(int(x) for x in cloth["rgb"])}

    # 5. 裁剪
    cut, mcut, bbox = crop_to_content(arr, mask)

    result = {
        "ok": True,
        "array": cut,
        "alpha": mcut,
        "coverage": coverage,
        "bbox": bbox,
        "cloth_rgb": tuple(int(x) for x in cloth["rgb"]),
        "cloth_cover": cloth["cover"],
        "cloth_tol": cloth.get("tol"),
        "size": (cut.shape[1], cut.shape[0]),
        "saved": None,
        "message": "提取成功：纹样占画面 %.0f%%，尺寸 %dx%d"
                   % (coverage * 100, cut.shape[1], cut.shape[0]),
    }

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        stem = os.path.splitext(os.path.basename(path))[0]
        out = os.path.join(out_dir, stem + "_motif.png")
        rgba = np.dstack([cut, (mcut.astype(np.uint8) * 255)])
        Image.fromarray(rgba, "RGBA").save(out)
        result["saved"] = out

    return result


def checkerboard(width, height, cell=16, light=(255, 255, 255),
                 dark=(205, 205, 210)):
    """生成棋盘格背景（RGBA uint8），用来检验透明纹样的轮廓。

    为什么必须用棋盘底看图：
        basic 系列里不少纹样的花瓣本身就是**白线**，放在白色页面上
        会和背景糊成一片 —— 看起来像「花瓣被挖空了」，
        其实只是白线白底看不见。棋盘格（深浅交替）能立刻分辨
        「真的是透明孔洞」还是「白色的绣线」。
    """
    yy = (np.arange(height)[:, None] // max(int(cell), 1))
    xx = (np.arange(width)[None, :] // max(int(cell), 1))
    mask = ((yy + xx) % 2).astype(bool)

    out = np.empty((height, width, 4), np.uint8)
    out[..., 0] = np.where(mask, light[0], dark[0])
    out[..., 1] = np.where(mask, light[1], dark[1])
    out[..., 2] = np.where(mask, light[2], dark[2])
    out[..., 3] = 255
    return out


def tile_preview(array, alpha, tiles=(3, 3), size=256, mirror=True,
                 backdrop=(248, 248, 246)):
    """把提取出的纹样拼成四方连续，看它作为「图案符号」的效果。

    mirror=True 用镜像接缝（半数绣样是左右对称的，镜像后接缝自然）。

    backdrop:
        底色。返回的图**已经合成好底色**，不是透明图 ——
        因为四方连续是用来判断「拼接好不好看」的，
        透明的缝在深色页面上会变成一片黑，看不出接缝效果。
        传 None 则保留透明（供需要透明拼贴的场合）。

    返回 RGBA Image（backdrop=None 时 alpha 有意义）。
    """
    if array is None or alpha is None:
        return None

    rgba = np.dstack([array, (alpha.astype(np.uint8) * 255)])
    im = Image.fromarray(rgba, "RGBA")

    # 缩放到统一单元尺寸，保持比例并留白
    cell = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    scale = min(size / im.width, size / im.height)
    nw, nh = max(1, int(im.width * scale)), max(1, int(im.height * scale))
    im = im.resize((nw, nh), Image.LANCZOS)
    cell.paste(im, ((size - nw) // 2, (size - nh) // 2), im)

    if mirror:
        tile = Image.new("RGBA", (size * 2, size * 2))
        tile.paste(cell, (0, 0))
        tile.paste(cell.transpose(Image.FLIP_LEFT_RIGHT), (size, 0))
        tile.paste(cell.transpose(Image.FLIP_TOP_BOTTOM), (0, size))
        tile.paste(cell.transpose(Image.ROTATE_180), (size, size))
    else:
        tile = cell

    cols, rows = tiles
    out = Image.new("RGBA", (tile.width * cols, tile.height * rows))
    for r in range(rows):
        for c in range(cols):
            out.paste(tile, (c * tile.width, r * tile.height))

    if backdrop is not None:
        bg = Image.new("RGBA", out.size, tuple(backdrop) + (255,))
        out = Image.alpha_composite(bg, out)

    return out


def heightmap(array, alpha, size=256, invert=False):
    """把纹样转成高度图，供 3D 浮雕展示。

    高度取自「绣线的明度」——刺绣是凸起的，亮部对应高出的线迹，
    这样 3D 起来才像实际的绣品浮雕，而不是一张贴图的硬边挤出。
    """
    if array is None or alpha is None:
        return None

    gray = (0.299 * array[..., 0] + 0.587 * array[..., 1]
            + 0.114 * array[..., 2])
    if invert:
        gray = 255.0 - gray

    # 只在纹样范围内取值，范围外置 0
    g = np.where(alpha, gray, 0.0)

    # 归一化到 0..1，并做一次轻平滑让浮雕过渡自然
    lo, hi = float(g[alpha].min()) if alpha.any() else 0.0, \
        float(g[alpha].max()) if alpha.any() else 1.0
    g = (g - lo) / max(hi - lo, 1e-6)
    g = np.clip(g, 0.0, 1.0) * alpha

    img = Image.fromarray((g * 255).astype(np.uint8), "L")
    img = img.resize((size, size), Image.LANCZOS)
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    return np.asarray(img).astype(np.float32) / 255.0


def relief_field(array, alpha, size=200, smooth=2.6):
    """把纹样转成「有结构的浮雕高度场」，供真 3D 网格使用。

    和 heightmap 的区别 —— 这是本函数存在的理由：

        heightmap 只用**明度**做高度。但苗绣大量是「同色系平绣」，
        线迹明度几乎一致，归一化之后只剩噪点，看不出任何结构
        （这就是之前 3D 看起来"平的、没效果"的根本原因）。

    本函数改用「**双尺度**」融合，让高度真正表达绣片的物理形态：

        粗尺度（决定大形状）── 距纹样边缘的距离
            纹样中心厚、边缘薄，形成天然的"浮雕坡面"。
            这是让纹样像"一块有厚度的实物"而不是"一张纸"的关键。

        细尺度（决定表面纹理）── 明度，且**强模糊**后才使用
            缎面反光让亮线略高一点。必须模糊，否则照片的
            传感器噪点会被逐像素放大成一丛尖刺
            （实测：不模糊时渲染出来像珊瑚/石笋，不像绣片）。

    注意 —— 这里**刻意不用**"局部标准差(边缘项)"：
        那个信号在真实绣品照片上几乎全是噪点，权重稍大就会
        把画面变成一片乱刺。浮雕要的是"光滑的坡面 + 温和的纹理"，
        不是把每个像素的高低差都忠实地立起来。

    返回 (h, w) 的 float32，范围 0..1，纹样外为 0。
    """
    if array is None or alpha is None:
        return None

    a = alpha.astype(np.float32)
    if not a.any():
        return None

    gray = (0.299 * array[..., 0] + 0.587 * array[..., 1]
            + 0.114 * array[..., 2]).astype(np.float32)

    # ---- 粗尺度：距边缘的距离（多步腐蚀近似）----
    dist = np.zeros_like(a)
    cur = (a > 0.5)
    level = 0
    max_lv = 24
    while cur.any() and level < max_lv:
        dist[cur] = level
        nxt = cur.copy()
        nxt[1:, :] &= cur[:-1, :]
        nxt[:-1, :] &= cur[1:, :]
        nxt[:, 1:] &= cur[:, :-1]
        nxt[:, :-1] &= cur[:, 1:]
        cur = nxt
        level += 1
    dist = dist / max(float(dist.max()), 1e-6)
    # 开方：中心隆起、边缘快速收薄，形成自然的浮雕坡面
    dist = np.sqrt(dist)

    # ---- 细尺度：明度，先强模糊再归一化 ----
    gimg = Image.fromarray(np.clip(gray, 0, 255).astype(np.uint8), "L")
    gimg = gimg.filter(ImageFilter.GaussianBlur(3.0))
    gs = np.asarray(gimg).astype(np.float32)
    vals = gs[a > 0]
    lo, hi = float(vals.min()), float(vals.max())
    lum = np.clip((gs - lo) / max(hi - lo, 1e-6), 0.0, 1.0)

    # ---- 融合：粗尺度主导形状，细尺度只做温和的表面起伏 ----
    h = 0.72 * dist + 0.28 * lum
    h = np.clip(h, 0.0, 1.0)
    h = h * a

    if size:
        himg = Image.fromarray((h * 255).astype(np.uint8), "L")
        aimg = Image.fromarray((a * 255).astype(np.uint8), "L")
        w0, h0 = array.shape[1], array.shape[0]
        scale = min(1.0, float(size) / max(w0, h0))
        if scale < 1.0:
            nw, nh = max(1, int(w0 * scale)), max(1, int(h0 * scale))
            himg = himg.resize((nw, nh), Image.LANCZOS)
            aimg = aimg.resize((nw, nh), Image.BILINEAR)
        h = np.asarray(himg).astype(np.float32) / 255.0
        a = np.asarray(aimg).astype(np.float32) / 255.0

    if smooth and smooth > 0:
        himg2 = Image.fromarray((h * 255).astype(np.uint8), "L")
        himg2 = himg2.filter(ImageFilter.GaussianBlur(float(smooth)))
        h = np.asarray(himg2).astype(np.float32) / 255.0

    # 用（缩放后的）alpha 裁掉边缘溢出，保证纹样外严格为 0
    h = h * (a > 0.5)

    return h


def relief_mesh(array, alpha, height=None, size=200,
                thickness=0.10, mask_thresh=0.06, max_cells=150):
    """把纹样变成一块**有厚度的真 3D 网格**（用于 Plotly Mesh3d）。

    为什么不用 go.Surface：
        Surface 本质是"一张弯曲的纸"——没有厚度、没有侧面、
        没有背光面，凑近看永远是一层壳。只有三角网格（Mesh3d）
        才是有实体的物体，能表达"这是一块绣片/徽章"。

    做法：
        1. 取纹样的高度场作为**正面**；背面 = 正面 - thickness，
           于是同一个平面上的点被"拉开"成有厚度的板。
        2. 逐格判断：该格的 4 个角是否有纹样——
             全部有 → 生成 2 个三角形（正面+背面）
             部分有 → 生成连接正面与背面的**侧壁**三角形（这就是"厚度"）
        3. 顶点色直接用纹样原色，让 3D 出来是彩色的纹样，
           而不是一片灰。

    返回 dict：
        {
          "x", "y", "z":          顶点坐标（一维等长数组）
          "i", "j", "k":          三角面索引
          "color":                每个顶点的 RGB 字符串
          "z_front", "z_back":    供场景摆放参考
        }
    若纹样为空则返回 None。
    """
    if height is None:
        height = relief_field(array, alpha, size=size)
    if height is None or not height.any():
        return None

    h, w = height.shape

    # ---- 边界柔化，消除"梳齿" ----
    # 问题：alpha 是二值掩膜，边界是一圈像素级锯齿。直接按它
    # 生成侧壁，就会在轮廓上立起一排高低不齐的尖齿
    # （实测渲染出来像"梳子/珊瑚"）。
    #
    # 做法：把掩膜轻微模糊成**软边界**，再让高度在靠近边界时
    # 平滑地衰减到 0。这样侧壁的高度从中心到边缘连续变化，
    # 轮廓变成一条柔和的坡，而不是一排竖齿。
    m_img = Image.fromarray((np.clip(height, 0, 1) * 255)
                            .astype(np.uint8), "L")
    taper = np.asarray(m_img.filter(ImageFilter.GaussianBlur(1.6))
                       ).astype(np.float32) / 255.0
    height = height * np.clip(taper * 3.2, 0.0, 1.0)

    # 顶点色：把纹样原图缩到同一尺寸
    rgb = array
    if rgb.shape[0] != h or rgb.shape[1] != w:
        rimg = Image.fromarray(rgb.astype(np.uint8), "RGB").resize(
            (w, h), Image.LANCZOS)
        rgb = np.asarray(rimg)

    # ---- 归一化到以中心为原点、长边为 1 的坐标 ----
    xs = (np.arange(w, dtype=np.float32) - (w - 1) / 2.0) / float(max(w, h))
    ys = (np.arange(h, dtype=np.float32) - (h - 1) / 2.0) / float(max(w, h))
    X, Y = np.meshgrid(xs, ys)          # (h, w)

    solid = height > mask_thresh
    zf = height * 0.55                  # 正面高度（放大一点更立体）
    zb = zf - thickness                 # 背面

    # ---- 收集顶点 ----
    # 顶点表：把每个像素的"正面点"和"背面点"都当作候选，
    # 但只为被引用到的点建顶点，避免爆炸。
    vkey = {}
    vx, vy, vz, vcol = [], [], [], []

    tris = []

    # ---- 用「规则栅格 + 高度归零」取代「格子内/外 + 竖侧壁」 ----
    #
    # 踩过的坑：最早按每个格子的 4 个角是否在纹样内来分情况，
    # 只给"部分在内"的格子补一圈竖直侧壁。结果轮廓上会出现
    # 一排高低不齐的梳齿 —— 因为格子是粗的，边界格子被整块
    # 抬起，相邻格子时有时无，就形成了锯齿状的石笋。
    #
    # 正确的做法（也是所有高度图建模的通用解法）：
    #   把纹样当作一张**完整的栅格高度图**来铺网格 —— 只要一个
    #   格子沾到纹样，就整格生成正面 + 背面。因为我们已经把
    #   高度在边界处平滑衰减到 0，边界格子的正面和背面会在
    #   z 上**自然闭合**（正面高度≈0，背面≈-thickness），
    #   既没有裂口，也不会有竖齿。轮廓完全由高度场本身决定，
    #   也就跟着高度场一起是平滑的。
    #
    # 代价：会多铺一圈"零高度"的格子。收益：轮廓干净、无锯齿，
    # 且网格是流形（每个边恰好两个面），渲染不会有破面。
    step = max(1, int(round(max(h, w) / float(max(max_cells, 8)))))
    ys_i = list(range(0, h - 1, step))
    xs_i = list(range(0, w - 1, step))

    for py in ys_i:
        py2 = min(py + step, h - 1)
        for px in xs_i:
            px2 = min(px + step, w - 1)
            corners = ((px, py), (px2, py), (px2, py2), (px, py2))
            inside = [bool(solid[c[1], c[0]]) for c in corners]
            if not any(inside):
                continue

            # 判断某个角是否"真的在纹样内"。不在纹样内的角会被压向
            # 背面，使这一格在边界处自然收拢（而不是竖起一堵墙）。
            def zpos(cx, cy, side):
                if not solid[cy, cx]:
                    # 纹样外：正面也压到背面平面，于是这一格在边界
                    # 处厚度趋近 0，轮廓随高度场一起平滑收口。
                    return float(zb[cy, cx])
                return float(zf[cy, cx] if side == 0 else zb[cy, cx])

            def pid(cx, cy, side):
                k = (cx, cy, side)
                idx = vkey.get(k)
                if idx is None:
                    idx = len(vx)
                    vkey[k] = idx
                    vx.append(float(X[cy, cx]))
                    vy.append(float(Y[cy, cx]))
                    vz.append(zpos(cx, cy, side))
                    c = rgb[cy, cx]
                    vcol.append("rgb(%d,%d,%d)"
                                % (int(c[0]), int(c[1]), int(c[2])))
                return idx

            f = [pid(cx, cy, 0) for cx, cy in corners]
            b = [pid(cx, cy, 1) for cx, cy in corners]
            # 正面 / 背面
            tris += [(f[0], f[1], f[2]), (f[0], f[2], f[3])]
            tris += [(b[0], b[2], b[1]), (b[0], b[3], b[2])]

            # 侧壁：只为"这一格有纹样、而相邻格没有"的那条边生成。
            # 这样生成的正是实体的**外侧面**，内部相邻格之间不再
            # 重复出墙（原来的写法让三角数多了 3 倍，纯属浪费）。
            # 判断"相邻格是否也有纹样"用该方向中点是否在掩膜内。
            mx0 = (px + px2) // 2
            my0 = (py + py2) // 2
            nb_in = (
                solid[my0, max(px - step, 0)] if px - step >= 0 else False,
                solid[min(py2 + step, h - 1), mx0] if py2 + step <= h - 1 else False,
                solid[my0, min(px2 + step, w - 1)] if px2 + step <= w - 1 else False,
                solid[max(py - step, 0), mx0] if py - step >= 0 else False,
            )
            for e0 in range(4):
                e1 = (e0 + 1) % 4
                if nb_in[e0]:
                    continue
                # 只在这条边真的沾到纹样时才建
                if not (inside[e0] and inside[e1]):
                    continue
                tris += [(f[e0], f[e1], b[e1]), (f[e0], b[e1], b[e0])]

    if not tris:
        return None

    i_arr = [t[0] for t in tris]
    j_arr = [t[1] for t in tris]
    k_arr = [t[2] for t in tris]

    return {
        "x": vx, "y": vy, "z": vz,
        "i": i_arr, "j": j_arr, "k": k_arr,
        "color": vcol,
        "z_front": float(np.max(vz)) if vz else 0.0,
        "z_back": float(np.min(vz)) if vz else 0.0,
    }


def build_3d_figure(array, alpha, size=200, max_cells=100,
                    thickness=0.10, show_floor=True, style="light",
                    mesh=None):
    """组装一张可自由环绕观察的 3D 场景（返回 Plotly Figure）。

    相比原来"只有一张 go.Surface 曲面"，这里是一个**完整场景**：

        展示台（地板） + 环境光 + 定向光 + 纹样实体网格
        + 阴影感 + 透视相机 + 可自由旋转/缩放/平移

    为什么必须是完整场景而不是孤立曲面：
        孤立的一张曲面悬在虚空里，大脑无法判断它的大小、朝向和
        空间关系，所以"看起来不像 3D"。加上地板和光照之后，
        视锥、投影、遮挡关系全部建立起来，纹样才真正"立"在那里。

    参数 style: "light" / "dark"，跟随页面主题，避免亮色页面上
        出现一块突兀的黑背景。

    参数 mesh: 可选的**已构建网格**。传进来就跳过重复建模 ——
        调用方（界面）通常已经算过一次网格用来显示统计信息，
        再传进来可省掉一次 0.3 秒的重复计算。

    未安装 plotly 时返回 None（由调用方降级）。
    """
    try:
        import plotly.graph_objects as go
    except ImportError:
        return None

    if mesh is None:
        mesh = relief_mesh(array, alpha, size=size, max_cells=max_cells,
                           thickness=thickness)
    if mesh is None:
        return None

    is_light = (style != "dark")
    scene_bg = "rgb(248,248,246)" if is_light else "rgb(24,26,30)"
    floor_col = "rgb(228,228,226)" if is_light else "rgb(38,40,46)"
    floor_line = "rgb(205,205,205)" if is_light else "rgb(58,60,68)"

    fig = go.Figure()

    # ---- ① 展示台：一块比纹样略大的地板 ----
    if show_floor:
        span = 0.62
        fig.add_trace(go.Mesh3d(
            x=[-span, span, span, -span],
            y=[-span, -span, span, span],
            z=[0, 0, 0, 0],
            i=[0, 0], j=[1, 2], k=[2, 3],
            color=floor_col,
            opacity=1.0,
            lighting=dict(ambient=0.85, diffuse=0.35,
                          specular=0.02, roughness=1.0),
            hoverinfo="skip",
            showscale=False,
        ))
        # 台面网格线：给地板一个尺度参照，强化"地面"的认知
        for t in (-0.4, -0.2, 0.0, 0.2, 0.4):
            fig.add_trace(go.Scatter3d(
                x=[-span, span], y=[t, t], z=[0.001, 0.001],
                mode="lines", line=dict(color=floor_line, width=1),
                hoverinfo="skip", showlegend=False))
            fig.add_trace(go.Scatter3d(
                x=[t, t], y=[-span, span], z=[0.001, 0.001],
                mode="lines", line=dict(color=floor_line, width=1),
                hoverinfo="skip", showlegend=False))

    # ---- ② 纹样实体 ----
    fig.add_trace(go.Mesh3d(
        x=mesh["x"], y=mesh["y"], z=mesh["z"],
        i=mesh["i"], j=mesh["j"], k=mesh["k"],
        vertexcolor=mesh["color"],
        # 让光照真有明暗：环境光压低、定向光抬高，
        # 这样凸起的线迹会亮、侧壁会暗，立体感来自明暗对比
        lighting=dict(
            ambient=0.45,
            diffuse=0.85,
            specular=0.30,
            roughness=0.55,
            fresnel=0.15,
        ),
        lightposition=dict(x=120, y=-160, z=220),
        flatshading=False,
        opacity=1.0,
        hoverinfo="skip",
        showscale=False,
        name="纹样",
    ))

    fig.update_layout(
        height=560,
        margin=dict(l=0, r=0, t=0, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
        scene=dict(
            bgcolor=scene_bg,
            aspectmode="manual",
            # 稍微压扁 z，但保留明显厚度，兼顾"像绣片"和"看得出起伏"
            aspectratio=dict(x=1.15, y=1.0, z=0.62),
            xaxis=dict(visible=False, showbackground=False),
            yaxis=dict(visible=False, showbackground=False),
            zaxis=dict(visible=False, showbackground=False),
            camera=dict(
                eye=dict(x=0.05, y=-1.55, z=0.85),
                up=dict(x=0, y=0, z=1),
            ),
        ),
        showlegend=False,
    )
    return fig


if __name__ == "__main__":
    """自检：对数据集里若干图片做提取，报告效果。"""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from paths import find_image_root

    root = find_image_root()
    if not root:
        print("未找到图片目录")
        raise SystemExit(1)

    out = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "data", "motifs")
    print("图片目录：%s" % root)
    print("输出目录：%s" % out)
    print()

    for f in ["basic_0.jpg", "basic_8.jpg", "basic_996.jpg"]:
        p = os.path.join(root, f)
        if not os.path.exists(p):
            print("%-16s 跳过（不存在）" % f)
            continue
        r = extract(p, out_dir=out)
        print("%-16s %s" % (f, r["message"]))
        if r.get("cloth_rgb"):
            print("                 底布色 RGB=%s（占外圈 %.0f%%）"
                  % (str(r["cloth_rgb"]), r["cloth_cover"] * 100))
