# -*- coding: utf-8 -*-
"""
标注参考特征提取

为标注任务表提供机器预判值，减轻人工填表负担。

提取三类特征：
  1. 主色调 —— 用 HSV 分桶归纳为中文色名（靛蓝 / 藏青 / 朱红 …）
  2. 对称性 —— 水平/垂直镜像的像素相似度，推断是否轴对称
  3. 画面特征 —— 明暗、饱和度、纹理密度，辅助判断繁简

注意：这些是**参考值**，不是标注结果。苗绣的技法（平绣、锁绣、破线绣等）
无法由像素推断，必须人工填写。
"""

from __future__ import annotations

import os

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

# ---------------------------------------------------------------------------
# 色名映射：把 RGB 归纳为苗绣常用色系的中文名
# ---------------------------------------------------------------------------
def _rgb_to_hsv_name(r, g, b):
    """把 RGB 转成中文色名。基于 HSV 分桶，针对苗绣常见配色。

    重要修正：低明度但**有色相**的像素不应判为"黑色"。
    苗绣大量使用深绿、深蓝、深褐底色（如 RGB 0,64,64 的墨绿、
    0,32,96 的靛蓝），早期版本把这些统一叫"黑色"，
    导致主色预判严重失真。现在的规则是：
      - 只有饱和度极低 且 明度极低 才叫黑色
      - 有色相的深色按其色相命名（墨绿 / 藏青 / 暗红 …）
    """
    r_, g_, b_ = r / 255.0, g / 255.0, b / 255.0
    mx, mn = max(r_, g_, b_), min(r_, g_, b_)
    v = mx
    s = 0.0 if mx == 0 else (mx - mn) / mx
    d = mx - mn

    if d == 0:
        h = 0.0
    elif mx == r_:
        h = (60 * ((g_ - b_) / d) + 360) % 360
    elif mx == g_:
        h = 60 * ((b_ - r_) / d) + 120
    else:
        h = 60 * ((r_ - g_) / d) + 240

    # ---- 无彩色系：饱和度低才判灰阶 ----
    if s < 0.12:
        if v < 0.22:
            return "黑色"
        if v > 0.85:
            return "白色"
        if v > 0.55:
            return "浅灰"
        return "深灰"

    # ---- 有色相：按色相 + 明度命名，深色不叫黑 ----
    deep = v < 0.42          # 深色标志
    pale = v > 0.78 and s < 0.45   # 浅淡色标志

    if h < 12 or h >= 345:
        if deep:
            return "暗红"
        return "朱红" if v > 0.55 else "赭红"
    if h < 40:
        if deep:
            return "深褐"
        return "橙黄" if v > 0.6 else "赭石"
    if h < 70:
        if deep:
            return "深棕"
        return "明黄" if v > 0.65 else "土黄"
    if h < 100:
        if deep:
            return "墨绿"
        return "黄绿" if v < 0.7 else "嫩绿"
    if h < 160:
        if deep:
            return "墨绿"
        if pale:
            return "浅绿"
        return "翠绿"
    if h < 200:
        if deep:
            return "深青"
        return "青色" if v < 0.75 else "浅青"
    if h < 250:
        if deep:
            return "藏青" if s > 0.5 else "深蓝"
        if pale:
            return "蓝灰"
        return "靛蓝"
    if h < 290:
        if deep:
            return "深紫"
        return "紫色" if v < 0.8 else "浅紫"
    # 品红/桃红系
    if deep:
        return "玫红"
    return "桃红" if v > 0.65 else "品红"


def dominant_colors(img, top_n=3, bins=48):
    """返回前 N 个主色的中文名及占比。

    关键处理：**先剔除背景**再统计颜色。

    实测发现，这批图以实物拍摄为主，背景（桌面、墙面、阴影）
    常常占据画面最大面积。若不剔除，会得出"72% 的图主色是黑色"
    这样明显失真的结论——那些"黑色"其实是暗背景。

    剔除方法：以画面四边框带的中位色作为背景估计，
    标记相似像素为背景，仅对前景像素做色彩统计。
    """
    small = img.convert("RGB").resize((120, 120))
    a = np.asarray(small).reshape(-1, 3).astype(np.int32)

    # ---- 背景估计：取四边各 8% 宽的框带 ----
    h = w = 120
    band = max(2, int(h * 0.08))
    border_idx = []
    for y in range(h):
        for x in range(w):
            if x < band or x >= w - band or y < band or y >= h - band:
                border_idx.append(y * w + x)
    border_idx = np.array(border_idx)
    bg = np.median(a[border_idx], axis=0)

    # ---- 前景掩膜：与背景色差超过阈值的像素 ----
    dist = np.abs(a - bg).sum(axis=1)
    fg = a[dist > 90]

    # 前景太少（<8%）说明剔除失败，退回全图统计
    if len(fg) < len(a) * 0.08:
        fg = a

    q = (fg // bins) * bins
    vals, counts = np.unique(q, axis=0, return_counts=True)
    order = np.argsort(-counts)[:top_n]
    total = counts.sum()
    out = []
    for i in order:
        c = vals[i]
        out.append({
            "name": _rgb_to_hsv_name(int(c[0]), int(c[1]), int(c[2])),
            "rgb": "%d,%d,%d" % (c[0], c[1], c[2]),
            "ratio": round(float(counts[i]) / total, 3),
        })
    return out


# ---------------------------------------------------------------------------
# 对称性
# ---------------------------------------------------------------------------
def symmetry_scores(img, max_side=256):
    """返回 (水平对称度, 垂直对称度)，取值 0–1，越大越对称。

    水平对称 = 左右镜像相似；垂直对称 = 上下镜像相似。
    先缩小再算，避免大图耗时。
    """
    g = img.convert("L")
    w, h = g.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        g = g.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    a = np.asarray(g).astype(np.float32)
    if a.size == 0:
        return 0.0, 0.0

    def sim(x, y):
        if x.shape != y.shape:
            return 0.0
        diff = np.abs(x - y).mean()
        return float(max(0.0, 1.0 - diff / 128.0))

    lr = sim(a, np.fliplr(a))
    ud = sim(a, np.flipud(a))
    return round(lr, 3), round(ud, 3)


def symmetry_label(lr, ud, threshold=0.62):
    """把对称度归为中文描述。

    阈值说明（基于对 250 张 basic 图的实测分布）：
        左右对称度 中位数 0.54，最大值 0.80
        上下对称度 中位数 0.55，最大值 0.84

    由于这批图多为**实物拍摄**而非平面纹样，拍摄角度、光照和布料褶皱
    会显著拉低镜像相似度。因此阈值不宜设高，且输出需标注"参考"字样，
    最终以人工判断为准。
    """
    hi_lr, hi_ud = lr >= threshold, ud >= threshold
    if hi_lr and hi_ud:
        return "双向对称"
    if hi_lr:
        return "左右对称"
    if hi_ud:
        return "上下对称"
    if max(lr, ud) >= threshold - 0.06:
        return "近似对称"
    return "非对称"


# ---------------------------------------------------------------------------
# 画面特征
# ---------------------------------------------------------------------------
def visual_stats(img, max_side=200):
    """返回明暗、饱和度、纹理密度等辅助特征。"""
    small = img.convert("RGB")
    w, h = small.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        small = small.resize((max(1, int(w * scale)), max(1, int(h * scale))))

    a = np.asarray(small).astype(np.float32)
    gray = a.mean(axis=2)

    mx = a.max(axis=2)
    mn = a.min(axis=2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0)

    grad_x = np.abs(np.diff(gray, axis=1)).mean() if gray.shape[1] > 1 else 0
    grad_y = np.abs(np.diff(gray, axis=0)).mean() if gray.shape[0] > 1 else 0
    detail = float((grad_x + grad_y) / 2)

    if detail > 18:
        complexity = "繁密"
    elif detail > 9:
        complexity = "中等"
    else:
        complexity = "疏朗"

    return {
        "brightness": round(float(gray.mean()) / 255, 3),
        "saturation": round(float(sat.mean()), 3),
        "detail_score": round(detail, 2),
        "complexity": complexity,
    }


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def analyze(path):
    """分析单张图，返回全部参考特征。"""
    with Image.open(path) as im:
        im = im.convert("RGB")
        colors = dominant_colors(im)
        lr, ud = symmetry_scores(im)
        stats = visual_stats(im)

    main = colors[0]["name"] if colors else ""
    palette = "、".join(c["name"] for c in colors)

    return {
        "ref_dominant_color": main,
        "ref_palette": palette,
        "ref_symmetry": symmetry_label(lr, ud),
        "ref_symmetry_lr": lr,
        "ref_symmetry_ud": ud,
        "ref_brightness": stats["brightness"],
        "ref_saturation": stats["saturation"],
        "ref_complexity": stats["complexity"],
        "ref_detail_score": stats["detail_score"],
    }


if __name__ == "__main__":
    # 自检：确认三类特征都能算出来
    # 图片目录自动探测，不再写死绝对路径
    import sys as _sys
    _sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
    try:
        from paths import find_image_root, image_root_or_hint
        src = find_image_root() or ""
    except Exception:
        src = ""

    if not src:
        print("未找到图片目录，无法自检。")
        print(image_root_or_hint() if 'image_root_or_hint' in dir() else "")
        raise SystemExit(1)

    print("图片目录：%s\n" % src)
    for f in ["basic_0.jpg", "basic_8.jpg", "basic_996.jpg"]:
        p = os.path.join(src, f)
        if os.path.exists(p):
            r = analyze(p)
            print(f)
            print("  主色：%s ｜ 配色：%s" % (r["ref_dominant_color"], r["ref_palette"]))
            print("  对称：%s（左右 %.2f / 上下 %.2f）"
                  % (r["ref_symmetry"], r["ref_symmetry_lr"], r["ref_symmetry_ud"]))
            print("  明度 %.2f ｜ 饱和 %.2f ｜ 疏密：%s"
                  % (r["ref_brightness"], r["ref_saturation"], r["ref_complexity"]))
