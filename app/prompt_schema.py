# -*- coding: utf-8 -*-
"""把三维分类结果拼成生成用的提示词（五段式）。

为什么是这个结构
----------------
`docs/苗绣潮玩_数据集与案例调研.md` 里查到，公开的中国纹样 LoRA
（IoC Lab / latentcat）用的是固定五段式标注：

    ①分类词 ②构图词 ③配色词 ④题材词 ⑤中心元素

它训练出来的模型对「构图词」响应特别明显（圆形/方形/异形构图可控），
而我们的分类表里**恰好就有构图这一维**（sym_label + rot_fold）。
所以这里不是另起一套，而是把已有的分类结果直接喂进这个已验证的槽位。

数据来源：metadata/纹样三维分类清单.csv 的一行（dict）。
本模块只做「读一行 → 出提示词」，不读文件、不依赖 Streamlit，纯函数，
方便单测，也方便以后接进 ② 页。
"""

from __future__ import annotations

# 构图槽：按对称类型 + 瓣数拼出可读描述。
# 中文给人看（学生写设计说明用），英文给模型用（SD/ComfyUI 认）。
_COMPOSE_CN = {
    "放射对称": "圆形放射构图，{fold} 瓣对称，视觉焦点集中在中心",
    "连续重复": "连续重复构图，图案单元可向四周延展，适合平铺",
    "左右对称": "左右轴对称构图，主纹居中，两侧呼应",
    "上下对称": "上下轴对称构图，重心稳定",
    "中心对称": "中心对称构图，旋转 180 度后自重合",
    "自由式": "自由式构图，疏密有致，不以几何对称为主",
}

_COMPOSE_EN = {
    "放射对称": "radial symmetrical composition with {fold}-fold rotation, "
              "centered focal motif",
    "连续重复": "seamless repeating composition, tileable unit pattern",
    "左右对称": "left-right symmetrical composition",
    "上下对称": "top-bottom symmetrical composition",
    "中心对称": "centered symmetrical composition",
    "自由式": "free-form composition, organic arrangement",
}

# 题材槽：艺术分类 → 题材描述
_SUBJECT_CN = {
    "花卉植物纹": "以花草枝叶为主体，花瓣与藤蔓穿插",
    "几何对称纹": "以菱形、回纹、八角星等几何骨架为主体",
    "动物神兽纹": "以蝴蝶、鸟、鱼、龙等图腾形象为主体",
    "卷草涡纹": "以卷草、涡旋、缠枝连绵不断为主体",
    "其他": "以绣片本身的构图与针脚肌理为主体",
}

_SUBJECT_EN = {
    "花卉植物纹": "flora and vines, flowers and leaves",
    "几何对称纹": "geometric lattice, diamonds and fretwork",
    "动物神兽纹": "totemic animals, butterfly, bird, fish, dragon",
    "卷草涡纹": "scrolling vines and spirals, continuous tendrils",
    "其他": "textile surface and stitch texture",
}

# 载体槽：产品倒推结果 → 工艺约束
_CARRIER_CN = {
    "钥匙扣徽章": "做成 3.5cm 徽章，轮廓需清晰、缩小后仍可辨",
    "包袋面料": "做成面料图案单元，需可重复拼接、接缝处能对上",
    "潮玩娃娃摆件": "做成 12cm 立体摆件贴图，需有正面的完整形象",
}

_CARRIER_EN = {
    "钥匙扣徽章": "enamel pin design, bold silhouette, readable at 3.5cm",
    "包袋面料": "textile print unit, seamless repeat",
    "潮玩娃娃摆件": "character decal for a 12cm vinyl figure, front-facing",
}

# 版式基调：来自流线与黄金比，决定画面"看着舒服不舒服"
_MOOD_CN = {
    ("高", "高"): "线条流动、构图考究，接近传统绣样的成熟章法",
    ("高", "中"): "线条流动，构图疏密自然",
    ("高", "低"): "线条流动为主，构图松弛",
    ("中", "高"): "线条平实，构图讲究比例",
}

DEFAULT_FOLD = 4


def _fold(row):
    try:
        f = int(float(row.get("rot_fold") or 0))
    except (TypeError, ValueError):
        f = 0
    return f if 2 <= f <= 8 else DEFAULT_FOLD


def _freeform(row, lang="cn"):
    """自由式构图的描述。

    为什么需要这一层：分类表里 97% 都是「自由式」，如果构图槽照实填
    「自由式构图」，那五段式里**最能控图的一段等于没填** ——
    生成结果会全糊成一类。改用两个连续量来区分：疏密（fill）与
    结构强度（sym_grade，批内三分位），这样同一句模板也能拉开层级。
    """
    try:
        fill = float(row.get("fill") or 0)
    except (TypeError, ValueError):
        fill = 0.0
    g = (row.get("sym_grade") or "").strip()

    if lang == "en":
        density = ("dense all-over pattern filling the frame" if fill >= 0.65
                   else ("balanced negative space" if fill >= 0.45
                         else "large negative space, small centered motif"))
        rigor = {"高": "with disciplined local echoes",
                 "中": "naturally arranged",
                 "低": "loosely arranged"}.get(g, "naturally arranged")
        return "%s, %s" % (density, rigor)

    density = ("纹样铺满画面，满构图" if fill >= 0.65
               else ("主体与留白均衡" if fill >= 0.45
                     else "大量留白，主体小而集中"))
    rigor = {"高": "局部有严谨的呼应结构",
             "中": "布局自然",
             "低": "布局松散随意"}.get(g, "布局自然")
    return "%s，%s" % (density, rigor)


def _color_cn(row):
    """配色槽：表里的 color_class 形如「墨黑地·素彩」。"""
    c = (row.get("color_class") or "").strip()
    if not c:
        return "以靛蓝与素白为主的传统配色"
    parts = c.split("·")
    if len(parts) == 2:
        return "%s底布，%s的花色" % (parts[0], parts[1])
    return c


def _color_en(row):
    c = (row.get("color_class") or "")
    # 只做粗粒度映射，够模型理解大方向；细分交给权重
    table = [
        ("墨黑", "deep indigo black ground"), ("蓝青", "blue-green ground"),
        ("素白", "off-white ground"), ("浅彩", "light pastel ground"),
        ("灰褐", "grey-brown ground"), ("玫红", "rose-red ground"),
    ]
    ground = "traditional indigo ground"
    for k, v in table:
        if k in c:
            ground = v
            break
    figure = "multi-colour" if "多彩" in c else (
        "monochrome elegant" if "素彩" in c else
        ("red-dominant" if "红彩" in c else "balanced"))
    return "%s with %s embroidery threads" % (ground, figure)


def build_prompt(row, lang="cn"):
    """把一行分类记录拼成五段式提示词。

    row 的键与 `metadata/纹样三维分类清单.csv` 一致：
      art_class, sym_label, rot_fold, color_class,
      stream_label, golden_label, product

    缺字段不报错 —— 分类表是逐步补的，缺哪段就跳过哪段，
    拼出来的提示词仍然可用（这比抛异常实用）。
    """
    sym = (row.get("sym_label") or "自由式").strip()
    fold = _fold(row)
    art = (row.get("art_class") or "其他").strip()
    prod = (row.get("product") or "").strip()

    if lang == "en":
        seg = []
        seg.append("traditional Miao embroidery pattern")
        if sym == "自由式":
            seg.append(_freeform(row, "en"))
        else:
            seg.append(_COMPOSE_EN.get(sym, _COMPOSE_EN["自由式"])
                       .format(fold=fold))
        seg.append(_color_en(row))
        seg.append(_SUBJECT_EN.get(art, _SUBJECT_EN["其他"]))
        if prod in _CARRIER_EN:
            seg.append(_CARRIER_EN[prod])
        return ", ".join(seg)

    seg = ["苗绣纹样"]
    if sym == "自由式":
        seg.append(_freeform(row, "cn"))
    else:
        seg.append(_COMPOSE_CN.get(sym, _COMPOSE_CN["自由式"]).format(fold=fold))
    seg.append(_color_cn(row))
    seg.append(_SUBJECT_CN.get(art, _SUBJECT_CN["其他"]))
    mood = _MOOD_CN.get(((row.get("stream_label") or "").strip(),
                         (row.get("golden_label") or "").strip()))
    if mood:
        seg.append(mood)
    if prod in _CARRIER_CN:
        seg.append(_CARRIER_CN[prod])
    return "；".join(seg) + "。"


REQUIRED_PX = {"钥匙扣徽章": 413, "包袋面料": 591, "潮玩娃娃摆件": 945}


def warnings(row):
    """生成前该提示的风险。返回中文短语列表，空列表表示没意见。

    最要紧的一条是分辨率：按 `docs/苗绣纹样美学分类标准.md` §4.3，
    现有图库做 12cm 潮玩有 87% 的原图撑不住。不提示的话，
    学生会拿到一张糊图还以为是自己提示词写错了。
    """
    out = []
    prod = (row.get("product") or "").strip()
    need = REQUIRED_PX.get(prod)
    try:
        px = int(float(row.get("motif_px") or 0))
    except (TypeError, ValueError):
        px = 0
    if need and px and px < need * 0.7:
        out.append("原图主体仅 %d 像素，而 %s 需要 %d 像素，"
                   "直接生成会糊；建议先放大或矢量重绘" % (px, prod, need))
    elif need and px and px < need:
        out.append("原图主体 %d 像素略低于 %s 所需 %d 像素，"
                   "放大后可能偏软" % (px, prod, need))
    if not (row.get("color_class") or "").strip():
        out.append("缺配色标签，配色槽用了默认值")
    return out


def _selftest():
    """最小自检：五段式不能拼出空串或重复段。

    "跑通了"不等于"拼对了" —— 一个返回空字符串的模板照样能跑完。
    """
    ok = True

    def show(name, cond, extra=""):
        nonlocal ok
        ok = ok and cond
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))

    full = {"art_class": "动物神兽纹", "sym_label": "放射对称", "rot_fold": 6,
            "color_class": "墨黑地·红彩", "stream_label": "高",
            "golden_label": "高", "product": "潮玩娃娃摆件", "motif_px": 1200,
            "fill": 0.6, "sym_grade": "高"}
    cn = build_prompt(full, "cn")
    en = build_prompt(full, "en")
    show("中文提示非空且含 6 瓣", len(cn) > 20 and "6 瓣" in cn, cn[:28])
    show("英文提示非空且含 radial", len(en) > 20 and "radial" in en, en[:32])
    show("分段数 ≥ 5（中文按分号切）", len(cn.rstrip("。").split("；")) >= 5,
         str(len(cn.rstrip("。").split("；"))))

    empty = {"art_class": "其他", "sym_label": "自由式", "fill": 0.5,
             "sym_grade": "中", "product": "钥匙扣徽章"}
    ce = build_prompt(empty, "cn")
    show("缺字段也不炸、仍出提示", len(ce) > 20, ce[:28])
    show("缺配色时给出警告", any("配色" in w for w in warnings(empty)))

    low = dict(full, motif_px=200)
    show("分辨率不足时给出警告",
         any("会糊" in w for w in warnings(low)), str(warnings(low))[:40])
    show("分辨率足够时不警告", warnings(full) == [], str(warnings(full)))

    print("自检：%s" % ("通过" if ok else "有失败项"))
    return ok


def demo(n_per_class=1):
    """从三维分类清单里挑几张，打印拼出来的提示词。"""
    import csv
    import io
    import os

    if not _selftest():
        return 1
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "metadata", "纹样三维分类清单.csv")
    if not os.path.exists(path):
        print("找不到 %s，先跑 tools/classify_aesthetics.py" % path)
        return 1
    with io.open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    seen = {}
    picked = []
    for r in rows:
        k = r.get("art_class")
        if seen.get(k, 0) < n_per_class:
            seen[k] = seen.get(k, 0) + 1
            picked.append(r)
    for r in picked:
        print("-" * 62)
        print("样本：%s（%s / %s / %s）"
              % (r.get("file"), r.get("art_class"), r.get("sym_label"),
                 r.get("product")))
        print("中文提示：")
        print("  " + build_prompt(r, "cn"))
        print("英文提示：")
        print("  " + build_prompt(r, "en"))
        for w in warnings(r):
            print("  ⚠ " + w)
    print("-" * 62)
    print("共 %d 条示例（数据集 %d 行）" % (len(picked), len(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(demo())
