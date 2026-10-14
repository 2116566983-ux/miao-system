# -*- coding: utf-8 -*-
"""
图数据库导入文件生成脚本

把 metadata/images.csv 转换成 Neo4j LOAD CSV 需要的三份文件：

  nodes_groups.csv     MotifGroup 节点（250 组）
  nodes_images.csv     Image 节点 + BELONGS_TO 关系端点（1000 张）
  rels_derived.csv     DERIVED_FROM 关系（750 条）

同时输出 import.cypher（实际导入语句）。

用法：
  python build_graph_csv.py --src metadata/images.csv --out graph/csv
"""

import argparse
import csv
import os
from collections import defaultdict

# Image 节点保留的属性
IMAGE_FIELDS = [
    "image_id", "file_name", "variant", "variant_label",
    "width", "height", "aspect", "orientation", "mode",
    "file_size_kb", "quality_status", "markable", "is_source",
    "pattern_type", "symmetry", "source", "permission_note",
]

# MotifGroup 节点属性（含组级标注字段，回填后此处生效）
GROUP_FIELDS = [
    "group_id", "source_image_id", "image_count",
    "region", "technique", "dominant_color", "pattern_type",
]


def read_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def write_csv(path, fields, rows):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


IMPORT_TEMPLATE = """// ============================================================================
// 苗绣纹样图数据库 · 数据导入
// ============================================================================
// 前置条件：
//   1. 已执行 schema.cypher 建好约束
//   2. 三个 CSV 已放入 Neo4j 的 import 目录
//      （Neo4j Desktop: 右键数据库 -> Open Folder -> Import）
//   3. Neo4j 配置已允许从 import 目录读取
//
// 执行方式：Neo4j Browser 中逐段粘贴，或 cypher-shell -f import.cypher
// 特性：全部使用 MERGE，可重复执行而不产生重复数据
// ============================================================================


// ---------------------------------------------------------------------------
// 第 1 步：创建 MotifGroup 节点（纹样来源分组）
// ---------------------------------------------------------------------------
LOAD CSV WITH HEADERS FROM 'file:///nodes_groups.csv' AS row
MERGE (g:MotifGroup {group_id: row.group_id})
SET g.source_image_id = row.source_image_id,
    g.image_count      = toInteger(row.image_count),
    g.region           = nullIf(row.region, ''),
    g.technique        = nullIf(row.technique, ''),
    g.dominant_color   = nullIf(row.dominant_color, ''),
    g.pattern_type     = nullIf(row.pattern_type, '');


// ---------------------------------------------------------------------------
// 第 2 步：创建 Image 节点，并挂接到所属分组
// ---------------------------------------------------------------------------
LOAD CSV WITH HEADERS FROM 'file:///nodes_images.csv' AS row
MERGE (i:Image {image_id: row.image_id})
SET i.file_name      = row.file_name,
    i.variant        = row.variant,
    i.variant_label  = row.variant_label,
    i.width          = toInteger(row.width),
    i.height         = toInteger(row.height),
    i.aspect         = toFloat(row.aspect),
    i.orientation    = row.orientation,
    i.mode           = row.mode,
    i.file_size_kb   = toFloat(row.file_size_kb),
    i.quality_status = row.quality_status,
    i.markable       = row.markable,
    i.is_source      = row.is_source,
    i.pattern_type   = nullIf(row.pattern_type, ''),
    i.symmetry       = nullIf(row.symmetry, ''),
    i.source         = nullIf(row.source, ''),
    i.permission_note = nullIf(row.permission_note, '')
WITH i, row
MATCH (g:MotifGroup {group_id: row.group_id})
MERGE (i)-[:BELONGS_TO]->(g);


// ---------------------------------------------------------------------------
// 第 3 步：建立变体派生关系 DERIVED_FROM
// ---------------------------------------------------------------------------
LOAD CSV WITH HEADERS FROM 'file:///rels_derived.csv' AS row
MATCH (v:Image {image_id: row.image_id})
MATCH (s:Image {image_id: row.source_image_id})
MERGE (v)-[:DERIVED_FROM]->(s);


// ---------------------------------------------------------------------------
// 第 4 步：导入校验
// ---------------------------------------------------------------------------
// 节点数量（期望：Image = %(n_images)d，MotifGroup = %(n_groups)d）
MATCH (i:Image) RETURN count(i) AS image_count;
MATCH (g:MotifGroup) RETURN count(g) AS group_count;

// 关系数量（期望：BELONGS_TO = %(n_images)d，DERIVED_FROM = %(n_derived)d）
MATCH ()-[r:BELONGS_TO]->() RETURN count(r) AS belongs_to_count;
MATCH ()-[r:DERIVED_FROM]->() RETURN count(r) AS derived_count;

// 分组完整性：应全部为 4，无例外
MATCH (g:MotifGroup)<-[:BELONGS_TO]-(i:Image)
RETURN g.group_id AS group_id, count(i) AS member_count
ORDER BY member_count DESC LIMIT 5;


// ============================================================================
// 教学检索常用查询（供 Streamlit 界面调用参考）
// ============================================================================

// 【查询 1】按变体类型检索，每组只取代表图（消除重复）—— 学生端默认
// :param variant => 'basic';
MATCH (g:MotifGroup)<-[:BELONGS_TO]-(i:Image)
WHERE i.variant = $variant AND i.markable = '是'
RETURN g.group_id AS group_id,
       i.image_id AS image_id,
       i.file_name AS file_name,
       i.variant_label AS variant_label,
       i.orientation AS orientation,
       i.width AS width, i.height AS height
ORDER BY i.image_id
LIMIT $limit;

// 【查询 2】按组聚合，显示每组的全部变体 —— 技术拓展层
MATCH (g:MotifGroup)<-[:BELONGS_TO]-(i:Image)
WITH g, collect(i.file_name) AS variants, count(i) AS n
WHERE n = 4
RETURN g.group_id, variants
LIMIT 5;

// 【查询 3】追溯某个变体的完整派生链路 —— 版权溯源用
// :param image_id => 'color_2';
MATCH path = (i:Image {image_id: $image_id})-[:DERIVED_FROM*0..3]->(root:Image)
RETURN [n IN nodes(path) | n.image_id] AS chain;

// 【查询 4】按方向 + 可标注筛选，供界面下拉使用
MATCH (i:Image)
WHERE i.orientation = $orientation AND i.markable = '是'
RETURN i.image_id, i.file_name, i.variant_label
ORDER BY i.image_id
LIMIT $limit;

// 【查询 5】标注进度看板 —— 教师端
MATCH (g:MotifGroup)
RETURN count(g) AS total_groups,
       sum(CASE WHEN g.region IS NOT NULL THEN 1 ELSE 0 END) AS annotated_region,
       sum(CASE WHEN g.technique IS NOT NULL THEN 1 ELSE 0 END) AS annotated_technique,
       sum(CASE WHEN g.dominant_color IS NOT NULL THEN 1 ELSE 0 END) AS annotated_color;

// 【查询 6】后续回填标注属性的写法（示例，勿直接执行）
// MATCH (g:MotifGroup {group_id: 'G0000'})
// SET g.region = '贵州黔东南', g.technique = '平绣', g.dominant_color = '靛蓝';
// —— 注意：属性挂在 MotifGroup 上，组内 4 个变体自动共享，无需重复标注
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="metadata/images.csv", help="images.csv 路径")
    ap.add_argument("--out", default="graph/csv", help="输出目录")
    args = ap.parse_args()

    rows = read_rows(args.src)
    print("读取 %d 行" % len(rows))

    # ---- 1. 按 group_id 汇总，生成 MotifGroup ----
    groups = {}
    for r in rows:
        gid = r.get("group_id", "")
        if not gid:
            continue
        g = groups.setdefault(gid, {"group_id": gid, "members": []})
        g["members"].append(r)

    group_rows = []
    for gid, g in sorted(groups.items()):
        members = g["members"]
        src = next((m for m in members if m.get("is_source") == "是"), members[0])
        group_rows.append({
            "group_id": gid,
            "source_image_id": src["image_id"],
            "image_count": len(members),
            # 组级标注字段：统一从源图取，留空表示未标注
            "region": src.get("region", ""),
            "technique": src.get("technique", ""),
            "dominant_color": src.get("dominant_color", ""),
            "pattern_type": src.get("pattern_type", ""),
        })

    write_csv(os.path.join(args.out, "nodes_groups.csv"), GROUP_FIELDS, group_rows)

    # ---- 2. Image 节点 ----
    image_rows = []
    for r in rows:
        image_rows.append({k: r.get(k, "") for k in IMAGE_FIELDS})
    write_csv(os.path.join(args.out, "nodes_images.csv"), IMAGE_FIELDS, image_rows)

    # ---- 3. DERIVED_FROM 关系 ----
    rel_rows = []
    for r in rows:
        if r.get("relation") == "DERIVED_FROM" and r.get("source_file"):
            rel_rows.append({
                "image_id": r["image_id"],
                "source_image_id": os.path.splitext(r["source_file"])[0],
            })
    write_csv(os.path.join(args.out, "rels_derived.csv"),
              ["image_id", "source_image_id"], rel_rows)

    # ---- 4. 生成 import.cypher ----
    cypher = IMPORT_TEMPLATE % {
        "n_images": len(image_rows),
        "n_groups": len(group_rows),
        "n_derived": len(rel_rows),
    }
    cypher_path = os.path.join(os.path.dirname(os.path.abspath(args.out)), "import.cypher")
    os.makedirs(os.path.dirname(cypher_path), exist_ok=True)
    with open(cypher_path, "w", encoding="utf-8") as f:
        f.write(cypher)

    # ---- 控制台报告 ----
    print("\n" + "=" * 56)
    print("生成完成")
    print("=" * 56)
    print("  MotifGroup 节点：%d" % len(group_rows))
    print("  Image 节点：%d" % len(image_rows))
    print("  DERIVED_FROM 关系：%d" % len(rel_rows))

    sizes = defaultdict(int)
    for g in group_rows:
        sizes[g["image_count"]] += 1
    print("\n  分组规模分布：")
    for s, c in sorted(sizes.items()):
        print("    每组 %d 张 -> %d 组" % (s, c))

    annotated = sum(1 for g in group_rows if g["region"] or g["technique"])
    print("\n  已标注的分组：%d / %d" % (annotated, len(group_rows)))
    if annotated == 0:
        print("    (组级标注字段为空，回填后重跑本脚本即可)")

    print("\n输出目录：%s" % os.path.abspath(args.out))
    print("导入脚本：%s" % cypher_path)


if __name__ == "__main__":
    main()
