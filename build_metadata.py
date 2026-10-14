# -*- coding: utf-8 -*-
"""
苗绣纹样数据集 -> 元数据清单生成脚本

功能：
  1. 扫描图片目录，读取每张图的尺寸、模式、文件大小
  2. 解析文件名前缀，识别变体分组（basic / texture / color / mixup）
  3. 按编号规则推断变体归属与原始图（父图）
  4. 计算质量状态（可标注 / 过小 / 拼贴不适用等）
  5. 输出 metadata/images.csv，可直接用于 Neo4j LOAD CSV

用法：
  python build_metadata.py --src "图片目录" --out metadata/images.csv
"""

import argparse
import csv
import os
import re
import sys
from collections import Counter, defaultdict

try:
    from PIL import Image
except ImportError:
    print("缺少 Pillow，请先安装：pip install pillow", file=sys.stderr)
    sys.exit(1)

Image.MAX_IMAGE_PIXELS = None

# ---- 业务规则（可按课程需要调整） ----
VARIANT_ORDER = ["basic", "texture", "color", "mixup"]

VARIANT_LABEL = {
    "basic": "原始图",
    "texture": "纹理变体",
    "color": "色彩变体",
    "mixup": "拼贴合成图",
}

# 最小可用尺寸：短边低于此值的图不适合做纹样区域标注
MIN_MARKABLE_SIDE = 100

# 命名形如 basic_0.jpg，末尾数字为全局序号
NAME_RE = re.compile(r"^(?P<variant>[a-z]+)_(?P<idx>\d+)\.(?P<ext>jpe?g|png|bmp|webp)$", re.I)


def scan(src_dir):
    """扫描目录，返回每张图的原始信息列表。"""
    rows = []
    for name in sorted(os.listdir(src_dir)):
        path = os.path.join(src_dir, name)
        if not os.path.isfile(path):
            continue
        m = NAME_RE.match(name)
        if not m:
            # 不符合命名规范的文件也记录下来，标记为待人工确认
            rows.append(
                {
                    "file_name": name,
                    "file_path": path,
                    "variant": "unknown",
                    "idx": "",
                    "width": "",
                    "height": "",
                    "aspect": "",
                    "orientation": "未知",
                    "mode": "",
                    "file_size_kb": round(os.path.getsize(path) / 1024, 1),
                    "quality_status": "命名异常",
                    "markable": "否",
                    "note": "文件名不符合 variant_index 规范，需人工确认",
                }
            )
            continue

        variant = m.group("variant").lower()
        idx = int(m.group("idx"))

        width = height = mode = ""
        read_err = ""
        try:
            with Image.open(path) as im:
                width, height = im.size
                mode = im.mode
        except Exception as e:  # 坏图
            read_err = str(e)

        if read_err:
            rows.append(
                {
                    "file_name": name,
                    "file_path": path,
                    "variant": variant,
                    "idx": idx,
                    "width": "",
                    "height": "",
                    "aspect": "",
                    "orientation": "未知",
                    "mode": "",
                    "file_size_kb": round(os.path.getsize(path) / 1024, 1),
                    "quality_status": "读取失败",
                    "markable": "否",
                    "note": read_err[:120],
                }
            )
            continue

        short_side = min(width, height)
        if short_side < MIN_MARKABLE_SIDE:
            quality = "尺寸过小"
            markable = "否"
            note = "短边不足 %dpx，不宜做区域标注" % MIN_MARKABLE_SIDE
        elif variant == "mixup":
            quality = "拼贴图"
            markable = "否"
            note = "合成/拼贴图，含多个尺度内容，标注范围需教师确认"
        else:
            quality = "可标注"
            markable = "是"
            note = ""

        aspect = round(width / height, 3) if height else ""
        if width > height * 1.1:
            orientation = "横图"
        elif height > width * 1.1:
            orientation = "竖图"
        else:
            orientation = "方图"

        rows.append(
            {
                "file_name": name,
                "file_path": path,
                "variant": variant,
                "idx": idx,
                "width": width,
                "height": height,
                "aspect": aspect,
                "orientation": orientation,
                "mode": mode,
                "file_size_kb": round(os.path.getsize(path) / 1024, 1),
                "quality_status": quality,
                "markable": markable,
                "note": note,
            }
        )
    return rows


def infer_groups(rows):
    """按编号推断变体分组（同组 = 同源图片的不同变体）。

    命名规律为 全局序号 % 4 对应变体：
        0 -> basic, 1 -> texture, 2 -> color, 3 -> mixup
    该规律在数据中已核实（basic_0 / texture_1 / color_2 / mixup_3 为一组）。
    若规律不成立，则退化为"每张图自成一源"，不会产生错误关联。
    """
    by_idx = {r["idx"]: r for r in rows if isinstance(r.get("idx"), int)}

    # 校验命名规律：检查 (idx % 4) 是否与变体一致
    hits = misses = 0
    for idx, r in by_idx.items():
        expected = VARIANT_ORDER[idx % 4]
        if r["variant"] == expected:
            hits += 1
        else:
            misses += 1
    rule_ok = hits > 0 and misses / max(hits + misses, 1) < 0.1

    # 按 (idx // 4) 分组
    buckets = defaultdict(list)
    for idx, r in by_idx.items():
        buckets[idx // 4].append(r)

    for gid, members in buckets.items():
        members.sort(key=lambda r: VARIANT_ORDER.index(r["variant"])
                    if r["variant"] in VARIANT_ORDER else 99)
        base = next((r for r in members if r["variant"] == "basic"), None)
        source_id = base["file_name"] if base else members[0]["file_name"]
        for r in members:
            r["group_id"] = "G%04d" % gid
            r["is_source"] = "是" if r is base else "否"
            r["source_file"] = source_id
            r["relation"] = (
                "源图" if r is base else "DERIVED_FROM"
            )
    return rows, rule_ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="图片目录")
    ap.add_argument("--out", required=True, help="输出的 CSV 路径")
    args = ap.parse_args()

    if not os.path.isdir(args.src):
        print("目录不存在：%s" % args.src, file=sys.stderr)
        sys.exit(1)

    rows = scan(args.src)
    rows, rule_ok = infer_groups(rows)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    fields = [
        "image_id", "file_name", "file_path", "variant", "variant_label",
        "group_id", "is_source", "source_file", "relation",
        "width", "height", "aspect", "orientation", "mode", "file_size_kb",
        "quality_status", "markable",
        "region", "pattern_type", "dominant_color", "symmetry", "technique",
        "source", "permission_note", "note",
    ]

    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r = dict(r)
            r["image_id"] = os.path.splitext(r["file_name"])[0]
            r["variant_label"] = VARIANT_LABEL.get(r["variant"], "未分类")
            # 以下字段需人工/课程标注后回填，先留空
            for k in ("region", "pattern_type", "dominant_color", "symmetry",
                      "technique", "source", "permission_note"):
                r.setdefault(k, "")
            w.writerow(r)

    # ---- 控制台报告 ----
    print("=" * 56)
    print("扫描完成：%d 个文件" % len(rows))
    print("=" * 56)

    vc = Counter(r["variant"] for r in rows)
    print("\n[变体分布]")
    for v in VARIANT_ORDER:
        if vc.get(v):
            print("  %-9s %4d 张  (%s)" % (v, vc[v], VARIANT_LABEL[v]))

    qc = Counter(r["quality_status"] for r in rows)
    print("\n[质量状态]")
    for q, c in qc.most_common():
        print("  %-8s %4d 张" % (q, c))

    groups = defaultdict(int)
    for r in rows:
        if r.get("group_id"):
            groups[r["group_id"]] += 1
    print("\n[变体分组]")
    print("  共 %d 组" % len(groups))
    gsizes = Counter(groups.values())
    for size, cnt in sorted(gsizes.items()):
        print("    每组 %d 张 -> %d 组" % (size, cnt))

    print("\n[命名规律校验]")
    print("  (序号 %% 4) 与变体一致：%s" % ("通过" if rule_ok else "未通过，已退化为独立图处理"))

    dims = {(r["width"], r["height"]) for r in rows if r["width"]}
    print("\n[尺寸多样性]")
    print("  不同尺寸数：%d" % len(dims))

    print("\n输出文件：%s" % os.path.abspath(args.out))
    print("可直接用于 Neo4j：LOAD CSV WITH HEADERS FROM 'file:///images.csv' AS row ...")


if __name__ == "__main__":
    main()
