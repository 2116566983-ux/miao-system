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


def _fill_holes(mask):
    """把纹样**内部**的小孔填上（不碰外部背景）。

    做法：对「非纹样」区域做一次从边界出发的连通性判断，
    能连到边界的说明是真正的背景；连不到的「非纹样」像素
    就是被纹样包住的内部孔 → 填成 True。

    复用已有的 _connected_from_border，不另写洪泛。

    为什么需要它：见 extract() 里调用处的注释 —— 2D 上小孔无害，
    3D 上每个孔都会长出一圈内壁，变成满屏竖条纹。
    """
    if mask is None or not mask.any():
        return mask

    bg = ~mask
    reached = _connected_from_border(bg)
    holes = bg & ~reached
    if not holes.any():
        return mask
    return mask | holes


def _solid_outline(mask, h, w):
    """把纹样的外轮廓内部补成实体（供 3D 网格使用）。

    和 alpha 的区别：alpha 是"绣线在哪"（花瓣之间是空的），
    本函数给的是"这块绣布的料在哪"（一整块，底布也算）。

    做法：
        1. 闭运算（先膨胀后腐蚀）把邻近的花枝连成一整块；
        2. 填掉闭运算留下的内部孔；
        3. 用"从边界洪泛"求出外轮廓（纹样外仍为 False）。

    这样得到的是一块**连成一体的绣布形状**，边界仍是纹样的自然外形，
    不会有方角或人工直边。3D 出来的就是"一件完整的绣片"。

    输入 mask 是原始尺寸；输出的形状与 mask 相同。
    """
    if mask is None or not mask.any():
        return mask

    # 闭运算的核大小随图像尺寸走：小图用 3，大图最多用 9
    k = 3 if max(h, w) < 300 else 9
    img = Image.fromarray((mask.astype(np.uint8) * 255), "L")
    img = img.filter(ImageFilter.MaxFilter(k))   # 膨胀
    img = img.filter(ImageFilter.MinFilter(k))   # 腐蚀 → 闭运算
    closed = np.asarray(img) > 127

    # 闭运算可能把不该连的地方连上（例如两个相隔很近的花枝），
    # 所以再做一次"只保留最大连通域"，保证是**一整块**。
    closed = _largest_component(closed)
    # 填内部孔
    closed = _fill_holes(closed)
    return closed


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

    # 4.5 填内部小洞 —— 这一步是**为 3D 服务的**，2D 上可有可无。
    #
    # 为什么必须做：苗绣的叶脉、花瓣之间常有几像素宽的缝隙，
    # 分割出来就是 alpha 上的小孔。2D 展示时这些孔无所谓
    # （透明底上根本看不出来），但**立体化之后是灾难**：
    # 每个孔都会被建一圈 thickness 高的内壁，
    # 网格上密布竖着的小墙，渲染出来就是"镂空碎斑"。
    #
    # 填洞的做法：从图像外框做一次背景洪泛，凡是"洪泛到不了
    # 的空洞"就是被纹样包住的内部孔 → 填成纹样。
    # 这样只填内部孔，不会把纹样外部的背景也吃掉。
    mask = _fill_holes(mask)

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


def relief_field(array, alpha, size=200, smooth=2.6, cloth_rgb=None):
    """把纹样转成「有结构的浮雕高度场」，供真 3D 网格使用。

    这里是整个 3D 效果的核心，值得把思路讲清楚 —— 因为**前两版
    的建模思路都是错的**，浪费了很多时间：

    ❌ 第一版：只用**明度**做高度（见 heightmap）
        苗绣大量是「同色系平绣」，线迹明度几乎一致，
        归一化之后只剩传感器噪点，看不出任何结构。

    ❌ 第二版：用「距纹样边缘的距离」当主形状
        理论上"中心厚、边缘薄"没错，但**尺度用错了** ——
        算出来的是「整块绣布的形状」，一块鼓起来的馒头，
        而真实绣片是「平的布 + 微微凸起的线迹」。
        实测这个思路渲染出来像山丘，不像绣片。

    ✅ 第三版（本函数现用）：**分两层，各管各的尺度**
        这是关键的认识转变 —— 「绣片的立体感」不是一种信号，
        而是**两种信号叠加**：

        底形（大尺度）── 整块布的厚度，用 alpha 的距离场，
            但幅度**压到很小**（0.18），只表达"这是一块有厚度的布"，
            绝不能让它变成馒头。

        纹路（中尺度）── **这一层才是灵魂**。
            用「非底布」来判断绣花：底布是近黑的靛蓝，
            绣花是彩色的，两者颜色差很大。
            绣花处抬高、底布处压低，于是高度场里
            出现的是**花朵、叶脉、回纹边框**——
            和肉眼看到的图案一致。
            这一步让"3D 的纹样"和"照片里的纹样"是同一个东西。

        肌理（小尺度）── 明度的强模糊，只做微弱起伏（0.18）。

    这个三层结构同时解决了两个老问题：
        · 不再是馒头（底形被压到 0.18）
        · 不再平滑得失真（纹路层带来清晰的图案边缘）

    参数 cloth_rgb: 底布颜色 (r,g,b)，用于区分绣花与底布。
        传 None 时退化为"只用 alpha"，效果会弱一些但不会出错。

    返回 (h, w) 的 float32，范围 0..1，纹样外为 0。
    """
    if array is None or alpha is None:
        return None

    a = alpha.astype(np.float32)
    if not a.any():
        return None

    gray = (0.299 * array[..., 0] + 0.587 * array[..., 1]
            + 0.114 * array[..., 2]).astype(np.float32)

    # ---- ① 底形：距边缘的距离，但幅度刻意压小 ----
    #
    # max_lv 原来是 24。这**不够** ——
    # 实测 basic_432 有 32.3% 的像素在第 23 层还"没收敛"，
    # 被上限一刀切断，于是形成一块平顶大平台，
    # 平台边缘就是一道硬台阶，渲染出来正是那几片"鳍"。
    #
    # 上限按图像尺寸走：纹样的最宽处最多不超过短边的一半，
    # 所以腐蚀到 min(h,w)//2 一定收敛。成本可以接受
    # （纯位运算）。
    dist = np.zeros_like(a)
    cur = (a > 0.5)
    level = 0
    max_lv = max(24, min(a.shape[0], a.shape[1]) // 2)
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
    # 开方：中心隆起、边缘收薄，形成浮雕坡面。
    #
    # 但**只开一次方是不够的** —— 实测发现 sqrt 在边界附近升得太急：
    # 离开轮廓 1 个像素高度就冲到 0.6，等于沿轮廓围了一圈陡墙，
    # 渲染出来是"鳍状"的立边（就是之前看到的那些竖片子）。
    #
    # 改成**两次开方**（等价于 0.25 次幂）：边界附近更平缓，
    # 中段仍保持上升，整片读起来才像"一块中央略厚、四周渐薄的绣片"，
    # 而不是"一块台地上围了一圈栅栏"。
    dist = np.sqrt(np.sqrt(dist))
    # 不再"抬底"。上一版写了 `0.30 + 0.70 * dist`，本意是表达
    # "绣片是实心的、边缘不是刀刃"。但那会让**整块布**都抬起 0.30，
    # 底布区也跟着鼓起来 —— 而底布本该是平的。
    # 实心感由 thickness（背面的偏移）来提供，不需要高度场来演。

    # ---- ② 纹路层：绣花凸起、底布凹陷 ----
    #
    # 这一层是"3D 像不像绣片"的决定性一步。
    #
    # 判据：苗绣的底布是整块靛蓝/藏青（近黑），绣花是彩色丝线。
    # 两者在 RGB 空间里差得很远。于是"到 cloth_rgb 的距离"
    # 天然就是一张**绣花掩膜** —— 不用做边缘检测、不用调阈值，
    # 而且和肉眼看到的图案完全一致。
    #
    # 为什么这是对的、而"距离场当主形状"是错的：
    #   距离场描述的是"一块布被剪成的形状"（宏观），
    #   绣花掩膜描述的是"布上绣了什么"（图案本身）。
    #   学生要看的是**纹样**：花、叶、回纹。这些必须立起来。
    cloth = np.asarray(cloth_rgb, dtype=np.float32) if cloth_rgb is not None \
        else np.array([53.0, 54.0, 58.0], dtype=np.float32)
    d_cloth = np.sqrt(((array.astype(np.float32) - cloth) ** 2).sum(-1))
    # 归一化：d_cloth 在底布上≈0，在绣花上很大
    dmax = float(np.percentile(d_cloth[a > 0], 92)) if a.any() else 1.0
    thread = np.clip(d_cloth / max(dmax, 1e-6), 0.0, 1.0)
    # 用 pow(0.65) 而不是 sqrt(0.5)：sqrt 会把中低对比度提得太狠，
    # 绣花边缘变成"刀片"（实测花朵轮廓像立起来的纸片）。
    # 0.65 是折中 —— 较暗的花瓣仍能立起来，但过渡是有坡的。
    thread = np.power(thread, 0.65)
    # 平滑要够：丝线的织纹是像素级噪点，不压掉就会变成一片毛刺。
    # 1.2 太轻，实测出现细密颗粒；2.0 才能得到"缎面"那种连续表面。
    thread = _gaussian_blur_float(thread, 2.0)

    # ---- ③ 肌理层：明度，先强模糊再归一化 ----
    lum = _gaussian_blur_float(gray, 3.0)
    vals = lum[a > 0]
    lo, hi = float(vals.min()), float(vals.max())
    lum = np.clip((lum - lo) / max(hi - lo, 1e-6), 0.0, 1.0)

    # ---- 融合：三层各司其职 ----
    #
    #   0.14  底形 —— 只表达"这是一块有厚度的布"，绝不做主形状
    #   0.54  纹路 —— 花朵/叶脉/回纹立起来，这是主体
    #   0.32  肌理 —— 丝线的细微起伏
    #
    # 底形只给 0.14 是**实测调出来的**：给到 0.22 时，
    # 在 basic_0 那张（宝蓝底布占大半、花朵面积也大）上，
    # 底形会把整朵花抬成一个鼓鼓的"枕头"，
    # 而 basic_432 上看着还行 —— 说明 0.22 已经过界了，
    # 只是那张图的底布占比刚好把问题掩盖了。
    # 判据不是"某一张看着好"，而是**所有图都不出现鼓包**。
    h = 0.14 * dist + 0.54 * thread + 0.32 * lum
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
        # 注意：**不能**走 `* 255 -> uint8 -> 模糊 -> /255` 这条路。
        #
        # 踩过的坑：量化到 8 位再模糊，等于把高度场砍成 256 档。
        # 实测本图相邻格的高度差中位数只有 0.0118（≈3 个灰阶），
        # 量化误差和真实信号同一个量级 —— 模糊之后又转回 float，
        # 高度场上就出现一层"台阶状"的毛刺。
        # 这些毛刺在网格里变成 1.71% 的、坡度 > 72° 的竖条，
        # 渲染出来就是左侧那排细密的"梳齿"。
        #
        # 正确做法：在 float 域直接用可分离卷积做高斯模糊，
        # 全程不落回整型，精度不掉。
        h = _gaussian_blur_float(h, float(smooth))

    # 用 alpha 把纹样外压到 0。
    #
    # 这里**不能**写 `h * (a > 0.5)`（硬截断）——
    # 那是"鳍状立边"的真正来源：高度场在轮廓内侧本来还有 0.5 左右，
    # 硬截断一步把它切到 0，轮廓上立刻出现一道接近 0.5 的垂直落差。
    # 上一版以为是融合权重的问题，改了权重也没用，因为病根在这一行。
    #
    # 正确做法：先把 alpha 边缘模糊成**软边**，再用它做乘法，
    # 并在外侧多留一点余量（>0.15 而不是 >0.5），
    # 让高度随 alpha 一起平滑地降到 0。轮廓于是变成一条缓坡，
    # 侧壁的高度也跟着连续收口。
    a_img = Image.fromarray((np.clip(a, 0, 1) * 255).astype(np.uint8), "L")
    a_soft = np.asarray(a_img.filter(ImageFilter.GaussianBlur(2.2))
                        ).astype(np.float32) / 255.0
    keep = np.clip(a_soft * 2.4 - 0.4, 0.0, 1.0)
    h = h * keep

    # ---- 去掉细碎孤岛 ----
    # alpha 里常带着原图边角上被误判进来的小块碎片（右下角的水印、
    # 一小段脱线的包边、背景里的反光斑……）。它们本身面积很小，
    # 但 dist 会把它们**当成一个完整的纹样**给到很高的高度，
    # 于是渲染出一片孤立竖起的"鳍"，看着像算法坏了
    # （实测 basic_432 右上就是个典型）。
    #
    # 教学场景下这种误读代价很高：学生会以为是程序出错，
    # 而不是"原图里本来就有一块杂物"。所以这里按面积清理掉：
    # 只保留最大的连通域，其余小岛归零。
    keep = _largest_component(keep > 0.12)
    h = h * keep.astype(np.float32)

    return h


def _gaussian_blur_float(arr, sigma):
    """在 float 域做高斯模糊（可分离卷积），不经过 8 位量化。

    为什么不用 PIL 的 GaussianBlur：
        PIL 只吃 8 位图。要模糊就得先 `* 255 -> uint8`，
        这个量化对高度场是致命的 —— 相邻格的真实高度差
        常常只有 0.01（≈3 个灰阶），量化误差和信号同量级，
        结果是在平滑的高度场上凭空造出一层台阶毛刺。

    实现：两个一维高斯核（水平 + 垂直）各卷一次。
    边界用「边缘复制」，避免纹样外围出现暗边。

    参数 arr 是二维 float，sigma 是标准差（像素）。
    """
    if arr is None or sigma <= 0:
        return arr

    radius = max(1, int(round(sigma * 3.0)))
    xs = np.arange(-radius, radius + 1, dtype=np.float32)
    k = np.exp(-(xs ** 2) / (2.0 * sigma * sigma))
    k = k / k.sum()

    # 水平方向
    pad = np.pad(arr, ((0, 0), (radius, radius)), mode="edge")
    out = np.zeros_like(arr)
    for off, wgt in enumerate(k):
        out += wgt * pad[:, off:off + arr.shape[1]]
    # 垂直方向
    pad = np.pad(out, ((radius, radius), (0, 0)), mode="edge")
    res = np.zeros_like(arr)
    for off, wgt in enumerate(k):
        res += wgt * pad[off:off + arr.shape[0], :]
    return res


def _largest_component(mask):
    """返回 mask 中面积最大的连通域（4 邻接），其余置 False。

    不用 scipy.ndimage.label —— 项目要求依赖尽量少
    （云端托管时每多一个依赖都要多编译一遍）。

    实现用**迭代式区域生长**：一遍扫过所有前景像素，
    每个还没被访问过的点作为种子长出一整块连通域，
    长完标记 visited，后续像素直接跳过。
    复杂度是 O(面积)，不是 O(面积²)。
    """
    if mask is None or not mask.any():
        return mask

    h, w = mask.shape
    visited = np.zeros_like(mask)
    best = np.zeros_like(mask)
    best_size = 0

    ys, xs = np.nonzero(mask)
    for y0, x0 in zip(ys.tolist(), xs.tolist()):
        if visited[y0, x0]:
            continue
        # 从 (y0, x0) 起做区域生长
        comp = np.zeros_like(mask)
        comp[y0, x0] = True
        frontier = np.zeros_like(mask)
        frontier[y0, x0] = True
        while frontier.any():
            nxt = np.zeros_like(mask)
            nxt[1:, :] |= frontier[:-1, :]
            nxt[:-1, :] |= frontier[1:, :]
            nxt[:, 1:] |= frontier[:, :-1]
            nxt[:, :-1] |= frontier[:, 1:]
            nxt &= mask
            nxt &= ~comp
            if not nxt.any():
                break
            comp |= nxt
            frontier = nxt
        visited |= comp
        size = int(comp.sum())
        if size > best_size:
            best_size = size
            best = comp
    return best


# 正面高度的 z 放大系数。定义为模块级常量，因为「浮雕厚度」滑杆
# 要按它换算可视厚度 —— 两处必须同步，写死两份迟早会改歪。
#
# 取值 0.22：纹样外接盒的 xy 跨度约 0.9，于是正面的高度跨度约
# 0.22 × 0.9 ≈ 0.20，占 xy 的 22% —— 这个量级本身已经"看得出厚薄"。
# 再配合 build_3d_figure 里的 aspect_z=0.35（显示端再压一道），
# 最终视觉斜率约 1.4，是"一块厚实的绣片"，不是"一片刀山"。
RELIEF_Z_SCALE = 0.22


def relief_mesh(array, alpha, height=None, size=200,
                thickness=0.10, mask_thresh=0.06, max_cells=150,
                cloth_rgb=None):
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

    参数 mask_thresh: **已废弃**。实体范围曾由 `height > mask_thresh`
        决定，但那会把底布切成碎岛、长出满屏竖条纹（详见下方
        solid 那段的注释）。现在改为由 alpha 决定。
        参数保留只为不破坏既有调用方的位置参数顺序。
    """
    if height is None:
        height = relief_field(array, alpha, size=size, cloth_rgb=cloth_rgb)
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
    #
    # 注意：这里同样**不能**走 8 位量化（PIL 的 GaussianBlur）。
    # relief_field 已经用软边 alpha 收过一次口，这一步是二次收口，
    # 量化误差会在这个"已经很平滑"的高度场上再造出一层台阶，
    # 渲染出来就是轮廓内侧那排细密的梳齿。用 float 版卷积。
    taper = _gaussian_blur_float(height, 1.6)
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

    # ---- 实体区域：由 **alpha** 决定，不由高度阈值决定 ----
    #
    # 踩过的坑（这是"竖条纹"的最后一个病根，找了好几轮）：
    # 原来写的是 `solid = height > mask_thresh`，本意是
    # "高度太低的地方不算实体"。但高度是**多层融合**出来的，
    # 在底布上高度接近 0 —— 于是底布区被判成"非实体"，
    # 而丝线的细微纹理又把底布切成一堆**小碎岛**，
    # 每个碎岛都被围上一圈 thickness 高的侧壁。
    # 结果整张底布上密布着竖直的小墙，渲染出来就是那排条纹。
    #
    # 正确做法：**实体范围只由 alpha 决定**。alpha 是"这张照片里
    # 有绣品"的区域，是一个干净的整体；高度只负责在实体内部
    # 表达起伏。两件事分开，网格就不会被高度的小波动切碎。
    #
    # ---- 但 alpha 里还有"大块的底布空洞"，3D 上要补上 ----
    #
    # alpha 表达的是"绣线在哪里"，所以花瓣之间的底布是空的
    # （实测 basic_0 有一大片宝蓝底布是空的）。2D 展示时这是对的
    # （透明底），但 3D 上会变成"一块镂空的花"——
    # 而真实的绣片是**一整块布**，底布就在那里，只是没绣花。
    #
    # 教学上这会误导：学生会以为绣品是剪纸。所以 3D 阶段
    # 把纹样的**外轮廓内部**全部补成实体（凸包式的收口），
    # 只在边缘保留自然的形状。
    # 做法：把 alpha 的小缺口用形态学闭运算连起来，再取
    # "最大连通域的外轮廓"。
    #
    # 注意：alpha 是**原图尺寸**，而 h/w 是 height 的尺寸
    # （relief_field 里按 size 缩过）。必须先缩到同一尺寸，
    # 否则形状对不上（这是一个真的踩过的坑：忘了缩放，
    # 结果 solid 是原图大小，与 (h, w) 的索引完全错位）。
    alpha_small = alpha
    if alpha.shape[0] != h or alpha.shape[1] != w:
        aimg = Image.fromarray((alpha.astype(np.uint8) * 255), "L")
        aimg = aimg.resize((w, h), Image.BILINEAR)
        alpha_small = np.asarray(aimg) > 127
    solid = _solid_outline(alpha_small, h, w)
    # 再剔除「高度为 0」的零散点。
    #
    # 为什么还要这一步：_solid_outline 补的是**掩膜的外轮廓**，
    # 但高度场在轮廓边缘已经衰减到 0 —— 于是轮廓内侧会残留
    # 一圈"实体但高度为 0"的像素。它们与旁边 0.5 高的邻居
    # 之间会生出一排 thickness 高的短墙，
    # 渲染出来就是轮廓内侧那条梳齿。
    #
    # 实测这类像素占 solid 的 2%，量很小但视觉很扎眼。
    solid &= (height > 1e-3)
    # 正面高度：RELIEF_Z_SCALE 的取值理由见文件顶部常量处的说明。
    # 一句话：0.55 会把坡度拉到 57°（实测），绣片变刀山；
    # 0.22 相当于把真实起伏（坡度量级 0.01~0.03）放大 10 倍，
    # 教学上刚好看得清层次，又不失真。
    zf = height * RELIEF_Z_SCALE
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
            # 四角高度全接近 0 的格子直接跳过。
            #
            # 为什么：_solid_outline 给的是"绣布的外轮廓"，
            # 但高度场在轮廓内侧的窄带里已经衰减到 0 —— 那里
            # 有 solid 却几乎没有高度，铺出来只是一层贴着台面的
            # 薄片，还会跟邻格的 0.5 高度形成短墙（就是轮廓内侧
            # 那排细齿）。不铺这些格子，轮廓由"高度场的零点"
            # 决定，反而更干净 —— 高度场本身是平滑衰减的，
            # 轮廓也就跟着平滑。
            if max(height[c[1], c[0]] for c in corners) < 1e-3:
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
                    mesh=None, cloth_rgb=None):
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
                           thickness=thickness, cloth_rgb=cloth_rgb)
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
            # ---- z 方向的显示比例（这一行非常容易搞错，写清楚）----
            #
            # Plotly 的 aspectmode="manual" 不是"按数据等比"，
            # 而是**把每个轴各自归一化到 [0,1]，再按 aspectratio 缩放**。
            # 于是视觉陡峭度 ≈ aspect_z / (z 的数据跨度)，
            # 而 aspect_x 要除以 x 的数据跨度 —— 两个跨度不等时，
            # aspect 的数值不再等于"看起来的比例"。
            #
            # 实测这份数据的跨度：x 0.920 / y 0.735 / z 0.200。
            # 也就是说 z 本来就只占 x 的 22%，屏幕上已经够立体了。
            # 这里必须**压低** z，而不是抬高：
            #
            #     aspect z=0.62 -> 视觉斜率 2.5   （还是偏陡）
            #     aspect z=0.35 -> 视觉斜率 1.4   ← 当前值
            #     aspect z=1.45 -> 视觉斜率 5.8   （等于假山，别用）
            #
            # 踩过的坑：第一版把 z 从 0.62 提到 1.45，本意是
            # "网格压平了、显示端拉回来"，结果两边同时放大，
            # 页面上仍是一片刀山 —— 因为 aspect 的语义被想当然了。
            # 记住：**数据端的 z 缩放和显示端的 aspect_z 是乘法关系**，
            # 要压就一起压。
            aspectratio=dict(x=1.15, y=1.0, z=0.35),
            xaxis=dict(visible=False, showbackground=False),
            yaxis=dict(visible=False, showbackground=False),
            zaxis=dict(visible=False, showbackground=False),
            camera=dict(
                # 俯角抬高一点：原来是 (0.05, -1.55, 0.85)，
                # 视角太低、几乎平视，绣片在屏幕上被压成一条，
                # 看不出"厚度"和"表面起伏"这两个关键信息。
                # 抬到 z=1.25、收紧 y，形成约 35° 的俯视 ——
                # 既能看见台面（判断大小与方位），
                # 又能斜着看到侧壁（判断厚度）。
                eye=dict(x=0.15, y=-1.75, z=1.25),
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
