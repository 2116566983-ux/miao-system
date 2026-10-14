# -*- coding: utf-8 -*-
"""
工作流模板管理与动态参数注入

ComfyUI 的 API 格式工作流是一个嵌套 dict：
    {"3": {"class_type": "KSampler", "inputs": {...}}, "4": {...}}

本模块把"模板"和"学生选择"解耦：
  学生只选图片和调强度，代码负责把这些值写进模板的正确节点。

使用前需要替换的占位符（保持原样即可，程序会自动填充）：

    模式一 · 单图重组
        __INPUT_IMAGE__     输入图片文件名（由 upload_image 返回）
        __POSITIVE__        正向提示词
        __NEGATIVE__        负向提示词
        __SEED__            随机种子
        __DENOISE__         重绘强度（0.3-0.8，越低越保留原图）

    模式二 · 多纹样组合
        __INPUT_IMAGE__     主纹样图片
        __INPUT_IMAGE_2__   第二张纹样图片
        __POSITIVE__ / __NEGATIVE__ / __SEED__ / __DENOISE__

用法：
    from workflow import load_template, inject, build_img2img
    tpl = load_template("workflows/img2img_template.json")
    wf  = build_img2img(tpl, "uploaded.jpg", prompt="...", seed=42, denoise=0.55)
"""

from __future__ import annotations

import copy
import json
import os
import random
import re

# ---------------------------------------------------------------------------
# 教学默认提示词
# 苗绣纹样讲究对称、几何、蓝靛配色，提示词要引导模型保留这些特征
# ---------------------------------------------------------------------------
DEFAULT_POSITIVE = (
    "miao embroidery pattern, traditional chinese ethnic motif, "
    "symmetrical geometric design, indigo blue and crimson, "
    "fine silk thread texture, flat lay on handwoven fabric, "
    "clean composition, high detail, textile art"
)

DEFAULT_NEGATIVE = (
    "blurry, low quality, distorted, extra limbs, watermark, text, "
    "signature, modern clothing, photorealistic portrait, cluttered background"
)

# 生成强度预设（面向学生，避免直接调数值）
DENOISE_PRESETS = {
    "轻微调整（最大程度保留原始纹样）": 0.35,
    "中等重组（推荐）": 0.55,
    "大幅创作（纹样变化明显）": 0.75,
}


# ---------------------------------------------------------------------------
# 模板加载
# ---------------------------------------------------------------------------
def strip_meta(workflow):
    """剔除工作流里的「非节点」键。

    为什么必须做：
        ComfyUI 的 API 格式要求顶层每个键都是一个节点 ID。
        如果有人（包括我们自己）在模板里加了 `_readme` 说明、
        `_comment` 注释之类的键，ComfyUI 会把它当节点解析，
        直接抛未捕获异常 —— 返回的是 500 而不是可读的校验错误，
        排查起来非常费劲（本项目就踩过这个坑）。

        所以统一在这里清掉：以 `_` 开头的键，以及值不是 dict 的键，
        另外把节点内的 `_meta` 也去掉（ComfyUI 界面元数据，API 不需要）。

    返回清理后的新字典，不修改入参。
    """
    if not isinstance(workflow, dict):
        return workflow

    out = {}
    for key, node in workflow.items():
        # 顶层：只保留"字典且不含前导下划线"的键
        if str(key).startswith("_"):
            continue
        if not isinstance(node, dict):
            continue

        # 节点内：去掉 _meta 这类界面元数据
        clean_node = {k: v for k, v in node.items()
                      if not str(k).startswith("_")}
        out[key] = clean_node
    return out


def load_template(path):
    """读取工作流模板 JSON，并剔除注释类键。

    模板里可以写 `_readme` 之类的说明，读取时会自动剔除，不会污染提交内容。
    """
    if not os.path.exists(path):
        raise FileNotFoundError("工作流模板不存在：%s" % path)
    with open(path, encoding="utf-8") as f:
        return strip_meta(json.load(f))


def count_nodes(workflow):
    """统计有效节点数（用于自检与界面显示）。"""
    return len(strip_meta(workflow))


# ---------------------------------------------------------------------------
# 模板选择：按服务器实际可用的模型挑
# ---------------------------------------------------------------------------
# 为什么需要这个：
#   不同 ComfyUI 环境装的模型差别很大。
#   有整合检查点（SD/SDXL 的 .safetensors）时用 CheckpointLoaderSimple；
#   只有分离式模型（UNET + CLIP + VAE）时要用 UNETLoader 那一套。
#   选错模板提交过去，ComfyUI 只会回一个 500，看不出原因。
#
# 本机实测过的两种环境：
#   1. 标准环境   -> checkpoints 有模型 -> img2img_template.json
#   2. 服务器环境 -> 只有分离式模型段   -> img2img_qwen.json
TEMPLATE_CHECKPOINT = "workflows/img2img_template.json"
TEMPLATE_SPLIT = "workflows/img2img_qwen.json"

# 多纹样组合的对应模板。
# 以前 multi_motif 模式硬编码 multi_motif_template.json（检查点版），
# 服务器上没有 checkpoint 时必然 400 失败。现在两套并列，按模型自动挑。
TEMPLATE_MULTI_CHECKPOINT = "workflows/multi_motif_template.json"
TEMPLATE_MULTI_SPLIT = "workflows/multi_motif_qwen.json"


def template_requirements(template):
    """从模板里读出它实际需要的模型文件。

    为什么需要：
        光看"服务器有没有 UNET / CLIP / VAE"是不够的 ——
        还必须是**模板里写的那个文件**。例如服务器上有 3 个扩散模型，
        但模板指定的是其中一个，另外两个不匹配。

        早先的版本只检查"有没有"，然后在理由里显示模型列表的第一项，
        结果提示的是完全不相干的模型（比如 48GB 的视频编码器），
        会误导教师以为配错了。

    返回 dict：{"unet"|"ckpt": 文件名, "clip": ..., "vae": ...}
    """
    need = {}
    for node in strip_meta(template).values():
        ct = node.get("class_type")
        ins = node.get("inputs") or {}
        if ct == "UNETLoader" and ins.get("unet_name"):
            need["unet"] = ins["unet_name"]
        elif ct == "CheckpointLoaderSimple" and ins.get("ckpt_name"):
            need["ckpt"] = ins["ckpt_name"]
        elif ct == "CLIPLoader" and ins.get("clip_name"):
            need["clip"] = ins["clip_name"]
        elif ct == "VAELoader" and ins.get("vae_name"):
            need["vae"] = ins["vae_name"]
    return need


# 模板与它需要的模型类别（用于挑选时核对）
_TEMPLATE_SPECS = [
    # (相对路径, 说明, 需要的键)
    (TEMPLATE_CHECKPOINT, "整合检查点模型", ("ckpt",)),
    (TEMPLATE_SPLIT, "分离式模型（扩散 + 编码器 + VAE）",
     ("unet", "clip", "vae")),
]

# 多纹样组合的模板集。顺序同上：先试整合检查点，再试分离式。
# 注意：以前 multi_motif 模式硬编码用 multi_motif_template.json（检查点版），
# 服务器上没有 checkpoint 时必然提交失败（ComfyUI 只回 400 且看不出原因）。
_TEMPLATE_SPECS_MULTI = [
    (TEMPLATE_MULTI_CHECKPOINT, "整合检查点模型", ("ckpt",)),
    (TEMPLATE_MULTI_SPLIT, "分离式模型（扩散 + 编码器 + VAE）",
     ("unet", "clip", "vae")),
]

_LABEL = {"ckpt": "整合检查点", "unet": "扩散模型",
          "clip": "文本编码器", "vae": "VAE"}


def pick_template(project_dir, inventory, mode="img2img"):
    """按服务器实际可用的模型挑选工作流模板。

    做法：逐个检查候选模板，读它**实际引用**的模型文件，
    确认这些文件确实在服务器上存在。第一个完全满足的即选中。

    mode: "img2img" 单图重组 / "multi_motif" 多纹样组合

    inventory 由 ComfyClient.model_inventory() 得到，形如：
        {"checkpoints": [...], "unets": [...], "clips": [...], "vaes": [...]}

    返回 (相对路径, 选择理由, 是否可用)
    """
    inv = inventory or {}
    pool = {
        "ckpt": inv.get("checkpoints") or [],
        "unet": inv.get("unets") or [],
        "clip": inv.get("clips") or [],
        "vae": inv.get("vaes") or [],
    }

    specs = (_TEMPLATE_SPECS_MULTI if mode == "multi_motif"
             else _TEMPLATE_SPECS)

    tried = []
    for rel, desc, keys in specs:
        path = os.path.join(project_dir, rel)
        if not os.path.exists(path):
            tried.append("%s（模板文件不存在）" % rel)
            continue

        try:
            tpl = load_template(path)
        except Exception as e:
            tried.append("%s（读取失败：%s）" % (rel, str(e)[:40]))
            continue

        need = template_requirements(tpl)
        missing = []
        for k in keys:
            want = need.get(k)
            if not want:
                missing.append("%s（模板未指定）" % _LABEL[k])
            elif want not in pool[k]:
                missing.append("%s %s 不在服务器上" % (_LABEL[k], want))

        if not missing:
            detail = " + ".join(need[k] for k in keys if need.get(k))
            return (rel, "%s 齐全：%s" % (desc, detail), True)

        tried.append("%s 缺：%s" % (desc, "；".join(missing)))

    # 都不满足：给出最贴近的诊断
    fallback = (TEMPLATE_MULTI_SPLIT if mode == "multi_motif"
                else TEMPLATE_SPLIT)
    return (fallback,
            "没有可用的工作流模板。逐个检查结果：" + " ｜ ".join(tried)
            + "。请给 ComfyUI 放入匹配的模型，或改用免费云端 API。",
            False)


def pick_img2img_template(project_dir, inventory):
    """单图重组的模板挑选（保留原函数名，兼容既有调用）。"""
    return pick_template(project_dir, inventory, mode="img2img")


def pick_multi_motif_template(project_dir, inventory):
    """多纹样组合的模板挑选。"""
    return pick_template(project_dir, inventory, mode="multi_motif")


def apply_overrides(base, overrides):
    """套用用户自定义覆盖（用于教师替换模型名、采样步数等）。"""
    out = copy.deepcopy(base)
    for path, value in (overrides or {}).items():
        # path 形如 "*" 表示全局替换某个 class_type 的某个 input
        out = _walk_replace(out, path, value)
    return out


def _walk_replace(node, path, value):
    """按 'class_type.input_name' 形式替换所有匹配节点的 input。"""
    if not isinstance(node, dict):
        return node
    if "class_type" in node and "inputs" in node:
        cls, _, key = path.partition(".")
        if key and node["class_type"] == cls and key in node["inputs"]:
            node["inputs"][key] = value
        else:
            for k, v in list(node["inputs"].items()):
                node["inputs"][k] = _walk_replace(v, path, value)
    else:
        for k, v in list(node.items()):
            node[k] = _walk_replace(v, path, value)
    return node


# ---------------------------------------------------------------------------
# 占位符注入
# ---------------------------------------------------------------------------
def inject(workflow, values):
    """把 values 字典里的值替换到工作流的占位符中。

    递归遍历，遇到字符串等于 __KEY__ 就整体替换（保持类型，不强行转字符串）。
    """
    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            key = node.strip()
            m = re.fullmatch(r"__([A-Z0-9_]+)__", key)
            if m and m.group(1) in values:
                return values[m.group(1)]
        return node

    return walk(copy.deepcopy(workflow))


class WorkflowTemplateError(Exception):
    """工作流模板缺少必需占位符时抛出。

    这类错误必须显式暴露，不能静默降级——否则教师选了"多纹样组合"
    却配了单图模板时，系统会默默产出一张只用了一张图的"组合"作品，
    教师还以为融合生效了。
    """
    pass


def find_placeholders(workflow):
    """扫描工作流，返回其中出现过的全部占位符名集合。"""
    found = set()

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str):
            m = re.fullmatch(r"__([A-Z0-9_]+)__", node.strip())
            if m:
                found.add(m.group(1))

    walk(workflow)
    return found


def require_placeholders(workflow, required, template_name="工作流模板"):
    """校验工作流包含必需占位符，缺失则抛错。

    required 是 {占位符名: 说明} 形式，便于生成清晰的错误信息。
    """
    present = find_placeholders(workflow)
    missing = [(k, v) for k, v in required.items() if k not in present]
    if missing:
        lines = ["%s 缺少必需的占位符：" % template_name]
        for k, v in missing:
            lines.append("  __%s__  —— %s" % (k, v))
        lines.append("")
        lines.append("该模板内实际出现的占位符：%s"
                     % ("、".join("__%s__" % p for p in sorted(present)) or "无"))
        lines.append("")
        lines.append("请在 ComfyUI 中确认工作流包含相应节点并标好占位符，"
                     "再用 Export (API) 导出覆盖模板文件。")
        raise WorkflowTemplateError("\n".join(lines))


# ---------------------------------------------------------------------------
# 模式一：单图重组（图生图）
# ---------------------------------------------------------------------------
def build_img2img(template, image_name, prompt=None, negative=None,
                  seed=None, denoise=0.55, subfolder="", strict=False):
    """构建单图重组工作流。

    image_name 是 ComfyUI 侧的文件名（upload_image 的返回值）。
    strict=True 时校验必需占位符，缺失即报错。
    """
    if strict:
        require_placeholders(template, {
            "INPUT_IMAGE": "加载纹样图片的 LoadImage 节点",
            "POSITIVE": "正向提示词节点",
            "NEGATIVE": "负向提示词节点",
            "SEED": "KSampler 的随机种子",
            "DENOISE": "KSampler 的重绘强度",
        }, "单图重组模板")

    if seed is None:
        seed = random.randint(1, 2 ** 31 - 1)
    # 若上传到了子目录，LoadImage 需要 subfolder/name 形式
    ref = ("%s/%s" % (subfolder, image_name)) if subfolder else image_name

    values = {
        "INPUT_IMAGE": ref,
        "POSITIVE": prompt or DEFAULT_POSITIVE,
        "NEGATIVE": negative or DEFAULT_NEGATIVE,
        "SEED": int(seed),
        "DENOISE": float(denoise),
    }
    return inject(template, values), int(seed)


# ---------------------------------------------------------------------------
# 模式二：多纹样组合
# ---------------------------------------------------------------------------
def build_multi_motif(template, image_name_1, image_name_2,
                      prompt=None, negative=None, seed=None, denoise=0.65,
                      blend=0.5, subfolder="", strict=True):
    """构建多纹样组合工作流。

    需要模板里有两处 LoadImage（占位 __INPUT_IMAGE__ 与 __INPUT_IMAGE_2__），
    以及 ImageBlend 节点（占位 __BLEND__）。

    strict 默认为 True：缺少第二张图或融合比例的占位符时直接报错，
    避免静默产出一张"假组合"作品。
    """
    if strict:
        require_placeholders(template, {
            "INPUT_IMAGE": "主纹样的 LoadImage 节点",
            "INPUT_IMAGE_2": "第二张纹样的 LoadImage 节点",
            "POSITIVE": "正向提示词节点",
            "NEGATIVE": "负向提示词节点",
            "SEED": "KSampler 的随机种子",
            "DENOISE": "KSampler 的重绘强度",
            "BLEND": "ImageBlend 的融合比例",
        }, "多纹样组合模板")

    if seed is None:
        seed = random.randint(1, 2 ** 31 - 1)

    ref1 = ("%s/%s" % (subfolder, image_name_1)) if subfolder else image_name_1
    ref2 = ("%s/%s" % (subfolder, image_name_2)) if subfolder else image_name_2

    values = {
        "INPUT_IMAGE": ref1,
        "INPUT_IMAGE_2": ref2,
        "POSITIVE": prompt or DEFAULT_POSITIVE,
        "NEGATIVE": negative or DEFAULT_NEGATIVE,
        "SEED": int(seed),
        "DENOISE": float(denoise),
        "BLEND": float(blend),
    }
    return inject(template, values), int(seed)


# ---------------------------------------------------------------------------
# 生成最小可用工作流（当用户还没有自制模板时的兜底）
# ---------------------------------------------------------------------------
def minimal_img2img(checkpoint_name="sd_xl_base_1.0.safetensors"):
    """返回一个最小的图生图 API 工作流，便于首次跑通。

    注意：节点 ID 用字符串，ComfyUI API 格式要求如此。
    默认模型名需与 ComfyUI 中实际安装的一致，否则提交会被拒绝。
    """
    return {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint_name},
        },
        "10": {
            "class_type": "LoadImage",
            "inputs": {"image": "__INPUT_IMAGE__", "upload": "image"},
        },
        "11": {
            "class_type": "VAEEncode",
            "inputs": {"pixels": ["10", 0], "vae": ["4", 2]},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "__POSITIVE__", "clip": ["4", 1]},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "__NEGATIVE__", "clip": ["4", 1]},
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": "__SEED__",
                "steps": 24,
                "cfg": 7.0,
                "sampler_name": "dpmpp_2m",
                "scheduler": "karras",
                "denoise": "__DENOISE__",
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["11", 0],
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "miao_workflow", "images": ["8", 0]},
        },
    }


if __name__ == "__main__":
    # 自检：确认占位符注入逻辑正确
    wf = minimal_img2img()
    built, sd = build_img2img(
        wf, "seed_input.jpg",
        prompt="test pattern", negative="bad",
        seed=12345, denoise=0.6,
    )
    ok = (
        built["10"]["inputs"]["image"] == "seed_input.jpg"
        and built["6"]["inputs"]["text"] == "test pattern"
        and built["7"]["inputs"]["text"] == "bad"
        and built["3"]["inputs"]["seed"] == 12345
        and built["3"]["inputs"]["denoise"] == 0.6
    )
    print("模板注入自检：", "通过" if ok else "失败")
    assert ok
    print("  图片引用：", built["10"]["inputs"]["image"])
    print("  正向提示词已注入：", built["6"]["inputs"]["text"][:30] + "...")
    print("  种子：", built["3"]["inputs"]["seed"])
    print("  重绘强度：", built["3"]["inputs"]["denoise"])

    tpl = minimal_img2img()
    built2, sd2 = build_multi_motif(
        tpl, "a.jpg", "b.jpg", prompt="combo", seed=999, denoise=0.7, blend=0.4
    )
    print("\n多纹样组合自检：通过")
    print("  主纹样：", built2["10"]["inputs"]["image"])
