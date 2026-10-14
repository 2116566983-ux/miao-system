# -*- coding: utf-8 -*-
"""
标注任务表生成与回填合并

两个功能：
  1. build   —— 从 images.csv 生成标注工作表（含机器参考值）
  2. merge   —— 把填好的标注表合并回 images.csv 和 graph/csv

工作流：
    python build_annotation_sheet.py build            # 生成待填表
    （人工填写 annotation/ 目录下两个 CSV）
    python build_annotation_sheet.py merge            # 合并回主数据

设计说明：
  - 标注分为**组级**和**图片级**两层。
    组级字段（地域、技法、主色、纹样类型）标在源图上，同组 4 个变体共享，
    这样人工只需标 250 次而不是 1000 次。
  - 机器已预判的字段放在 ref_ 前缀列，人工列留空。
    人工列一旦填写，合并时优先采用人工值。
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import OrderedDict

# ---------------------------------------------------------------------------
# 字段定义
# ---------------------------------------------------------------------------

# 组级标注字段（人工填写）——挂在 MotifGroup 上
GROUP_ANNOTATION_FIELDS = OrderedDict([
    ("pattern_type", "纹样类型"),
    ("region", "地域来源"),
    ("technique", "刺绣技法"),
    ("dominant_color", "主色调"),
    ("symmetry_type", "对称形式"),
    ("composition", "构图特征"),
    ("cultural_meaning", "文化寓意"),
    ("permission_note", "来源与授权"),
])

# 图片级标注字段（人工填写）——针对源图本身
IMAGE_ANNOTATION_FIELDS = OrderedDict([
    ("quality_note", "质量备注"),
    ("pattern_type_detail", "纹样细类"),
    ("usage_suggestion", "适用说明"),
])

# 机器参考字段（自动生成，只读）
#
# 定位说明：这些值是**色彩分布提示**，不是标注结论。
# 实测发现这批图多为实物拍摄，普遍带黑色边框或暗背景，
# 机器统计出的首位色常是背景色而非纹样主色（例如一张桃红花卉刺绣
# 会因边框占比最高而被判为"黑色"）。
# 因此使用建议是：把 ref_palette 当作"这张图里有哪些颜色"的线索，
# 主色最终由人工判断后填入 dominant_color 列。
REF_FIELDS = OrderedDict([
    ("ref_dominant_color", "机器统计首位色（常为背景，勿直接采信）"),
    ("ref_palette", "机器统计配色线索"),
    ("ref_symmetry", "机器预判对称"),
    ("ref_symmetry_lr", "左右对称度"),
    ("ref_symmetry_ud", "上下对称度"),
    ("ref_brightness", "明度"),
    ("ref_saturation", "饱和度"),
    ("ref_complexity", "疏密程度"),
])


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def write_csv(path, fields, rows, headers=None):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(headers if headers else fields)
        for r in rows:
            w.writerow([r.get(k, "") for k in fields])


# ---------------------------------------------------------------------------
# build：生成待填表
# ---------------------------------------------------------------------------
def cmd_build(args):
    rows_all = read_csv(args.images)
    print("读取元数据 %d 行" % len(rows_all))

    # 先按全部图片建立分组，得到真实组规模
    full_groups = {}
    for r in rows_all:
        gid = r.get("group_id")
        if gid:
            full_groups.setdefault(gid, []).append(r)

    # 只处理可标注的图（markable = 是）
    rows = rows_all
    if args.only_markable:
        rows = [r for r in rows_all if r.get("markable") == "是"]
        print("筛选可标注图：%d 张（来自 %d 组）" % (len(rows), len(full_groups)))

    # ---- 挑选每组代表（优先 basic 源图）----
    groups = {}
    for r in rows:
        gid = r.get("group_id")
        if not gid:
            continue
        groups.setdefault(gid, []).append(r)

    rep_rows = []
    for gid, members in sorted(groups.items()):
        src = next((m for m in members if m.get("is_source") == "是"), None)
        if src is None:
            # 源图不可标注（尺寸过小等），退而取组内质量最好的
            cands = sorted(members,
                           key=lambda m: 0 if m.get("quality_status") == "可标注" else 1)
            src = cands[0]
        src = dict(src)
        # 真实组规模（含不可标注的变体），而非筛选后的数量
        src["_member_count"] = len(full_groups.get(gid, members))
        src["_markable_count"] = sum(
            1 for m in full_groups.get(gid, members) if m.get("markable") == "是")
        rep_rows.append(src)

    print("生成代表图 %d 张（每组一张）" % len(rep_rows))

    # ---- 特征提取 ----
    import analyze_features as af

    # 图片目录：命令行没给就自动探测
    img_root = args.image_root
    if not img_root:
        try:
            import sys as _sys
            _sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
            from paths import find_image_root
            img_root = find_image_root() or ""
        except Exception:
            img_root = ""
    if not img_root:
        print("[提示] 未找到图片目录，机器参考值将留空（不影响待填字段）")
        print("       如需特征值，用 --image-root 指定图片目录")

    out_rows = []
    for i, r in enumerate(rep_rows, 1):
        path = r.get("file_path") or (os.path.join(img_root, r["file_name"]) if img_root else "")
        feats = {}
        if os.path.exists(path):
            try:
                feats = af.analyze(path)
            except Exception as e:
                feats = {"ref_dominant_color": "提取失败：%s" % str(e)[:40]}
        else:
            feats = {"ref_dominant_color": "图片未找到"}

        row = {
            "group_id": r.get("group_id", ""),
            "image_id": r.get("image_id", ""),
            "file_name": r.get("file_name", ""),
            "width": r.get("width", ""),
            "height": r.get("height", ""),
            "orientation": r.get("orientation", ""),
            "member_count": r.get("_member_count", ""),
            "markable_count": r.get("_markable_count", ""),
        }
        # 待填字段全部留空
        for k in GROUP_ANNOTATION_FIELDS:
            row[k] = ""
        for k in IMAGE_ANNOTATION_FIELDS:
            row[k] = ""
        # 机器参考值
        row.update(feats)
        out_rows.append(row)

        if i % 50 == 0:
            print("  已分析 %d / %d" % (i, len(rep_rows)))

    fields = (["group_id", "image_id", "file_name", "width", "height",
               "orientation", "member_count", "markable_count"]
              + list(GROUP_ANNOTATION_FIELDS)
              + list(IMAGE_ANNOTATION_FIELDS)
              + list(REF_FIELDS))

    headers = (["分组编号", "图片编号", "文件名", "宽", "高", "方向",
                "组内图片总数", "其中可标注数"]
               + ["【待填】" + v for v in GROUP_ANNOTATION_FIELDS.values()]
               + ["【待填】" + v for v in IMAGE_ANNOTATION_FIELDS.values()]
               + ["【参考】" + v for v in REF_FIELDS.values()])

    os.makedirs(args.out_dir, exist_ok=True)

    # 主表（含参考值，供人工参考）
    main_path = os.path.join(args.out_dir, "annotation_sheet.csv")
    write_csv(main_path, fields, out_rows, headers)

    # 精简表（只有待填字段，适合导出给助教批量填）
    slim_fields = (["group_id", "image_id", "file_name", "orientation"]
                   + list(GROUP_ANNOTATION_FIELDS)
                   + list(IMAGE_ANNOTATION_FIELDS))
    slim_headers = (["分组编号", "图片编号", "文件名", "方向"]
                    + list(GROUP_ANNOTATION_FIELDS.values())
                    + list(IMAGE_ANNOTATION_FIELDS.values()))
    slim_path = os.path.join(args.out_dir, "annotation_sheet_slim.csv")
    write_csv(slim_path, slim_fields, out_rows, slim_headers)

    print()
    print("=" * 58)
    print("生成完成")
    print("=" * 58)
    print("  待标注分组：%d 组" % len(out_rows))
    print()
    print("  完整表（含机器参考值）：")
    print("    %s" % os.path.abspath(main_path))
    print("  精简表（只含待填字段，适合导出）：")
    print("    %s" % os.path.abspath(slim_path))
    print()
    print("  待填字段（%d 个）：" % (len(GROUP_ANNOTATION_FIELDS)
                                   + len(IMAGE_ANNOTATION_FIELDS)))
    for k, v in list(GROUP_ANNOTATION_FIELDS.items()) + list(IMAGE_ANNOTATION_FIELDS.items()):
        print("    %-22s %s" % (v, k))
    print()
    print("  机器已预判（%d 个，仅供参考）：" % len(REF_FIELDS))
    for k, v in REF_FIELDS.items():
        print("    %-22s %s" % (v, k))


# ---------------------------------------------------------------------------
# merge：合并回主数据
# ---------------------------------------------------------------------------
def cmd_merge(args):
    sheet_path = os.path.join(args.out_dir, "annotation_sheet.csv")
    if not os.path.exists(sheet_path):
        print("找不到标注表：%s" % sheet_path, file=sys.stderr)
        sys.exit(1)

    sheet = read_csv(sheet_path)
    print("读取标注表 %d 行" % len(sheet))

    # 标注表用的是中文表头，这里建立 中文表头 -> 英文字段 的反查
    # 否则按英文 key 取值会全部取到空字符串（曾因此导致合并静默失败）
    zh_to_en = {}
    for k, v in GROUP_ANNOTATION_FIELDS.items():
        zh_to_en["【待填】" + v] = k
    for k, v in IMAGE_ANNOTATION_FIELDS.items():
        zh_to_en["【待填】" + v] = k
    ref_to_en = {}
    for k, v in REF_FIELDS.items():
        ref_to_en["【参考】" + v] = k

    # 校验标注表列名是否符合预期
    actual_cols = set(sheet[0].keys()) if sheet else set()
    missing = [c for c in zh_to_en if c not in actual_cols]
    if missing:
        print("警告：标注表缺少以下列，请勿修改列名：", file=sys.stderr)
        for m in missing:
            print("  %s" % m, file=sys.stderr)

    # 建立 group_id -> 标注 的映射
    ann = {}
    filled_stat = {k: 0 for k in list(GROUP_ANNOTATION_FIELDS)
                   + list(IMAGE_ANNOTATION_FIELDS)}

    for r in sheet:
        gid = (r.get("分组编号") or "").strip()
        if not gid:
            continue

        vals = {}
        for zh, en in zh_to_en.items():
            vals[en] = (r.get(zh) or "").strip()

        for k, v in vals.items():
            if v:
                filled_stat[k] += 1

        # 主色调若人工未填，用机器参考值兜底
        if not vals.get("dominant_color"):
            ref_col = "【参考】" + REF_FIELDS["ref_dominant_color"]
            vals["dominant_color"] = (r.get(ref_col) or "").strip()
        if not vals.get("symmetry_type"):
            ref_col = "【参考】" + REF_FIELDS["ref_symmetry"]
            vals["symmetry_type"] = (r.get(ref_col) or "").strip()

        ann[gid] = vals

    print()
    print("填表进度：")
    for k, v in filled_stat.items():
        label = GROUP_ANNOTATION_FIELDS.get(k) or IMAGE_ANNOTATION_FIELDS.get(k, k)
        pct = v / max(len(sheet), 1) * 100
        print("  %-14s %3d / %d  (%.0f%%)" % (label, v, len(sheet), pct))

    # ---- 回写 images.csv ----
    images = read_csv(args.images)
    n_updated = 0
    for r in images:
        gid = r.get("group_id", "")
        a = ann.get(gid)
        if not a:
            continue
        changed = False
        for k, v in a.items():
            if k in r and v:
                if r.get(k) != v:
                    r[k] = v
                    changed = True
        if changed:
            n_updated += 1

    fields = list(images[0].keys()) if images else []
    with open(args.images, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(images)

    print()
    print("已回写 %s" % args.images)
    print("  更新行数：%d / %d" % (n_updated, len(images)))

    print()
    print("下一步：重新生成图数据库导入文件")
    print("  python build_graph_csv.py --src metadata/images.csv --out graph/csv")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="标注任务表生成与合并")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="生成待填标注表")
    b.add_argument("--images", default="metadata/images.csv")
    b.add_argument("--image-root", default=None,
                   help="图片目录；不指定则自动探测")
    b.add_argument("--out-dir", default="annotation")
    b.add_argument("--only-markable", action="store_true", default=True)
    b.set_defaults(func=cmd_build)

    m = sub.add_parser("merge", help="把标注合并回主数据")
    m.add_argument("--images", default="metadata/images.csv")
    m.add_argument("--out-dir", default="annotation")
    m.set_defaults(func=cmd_merge)

    args = ap.parse_args()
    args.func(args)
