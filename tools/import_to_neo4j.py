# -*- coding: utf-8 -*-
"""
把现有 CSV 数据导入本地 Neo4j。

用法：
    python tools/import_to_neo4j.py [--uri bolt://localhost:7687]
                                    [--user neo4j] [--password xxxx]

它会：
    1. 连接 Neo4j（连不上就报错退出，不做任何破坏性操作）
    2. 建立约束/索引（可重复执行）
    3. 用 MERGE 导入节点与关系（幂等，重复跑不会产生重复数据）

安全原则：
    - **只做 MERGE，不做 DETACH DELETE**。默认不清库，
      避免误删已有数据。要清库请显式加 --wipe。
    - 所有写入都带参数化查询，不拼字符串。
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, ROOT)

try:
    from neo4j import GraphDatabase
except ImportError:
    print("缺少 neo4j 驱动：pip install neo4j")
    raise SystemExit(1)


def read_csv(path):
    if not os.path.exists(path):
        return []
    with io.open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def g_(row, *keys, default=""):
    for k in keys:
        v = (row.get(k) or "").strip()
        if v:
            return v
    return default


CONSTRAINTS = [
    "CREATE CONSTRAINT image_id_unique IF NOT EXISTS "
    "FOR (n:Image) REQUIRE n.image_id IS UNIQUE",
    "CREATE CONSTRAINT group_id_unique IF NOT EXISTS "
    "FOR (n:MotifGroup) REQUIRE n.group_id IS UNIQUE",
    "CREATE CONSTRAINT color_name_unique IF NOT EXISTS "
    "FOR (n:Color) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT form_name_unique IF NOT EXISTS "
    "FOR (n:Form) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT technique_name_unique IF NOT EXISTS "
    "FOR (n:Technique) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT region_name_unique IF NOT EXISTS "
    "FOR (n:Region) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT student_name_unique IF NOT EXISTS "
    "FOR (n:Student) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT work_id_unique IF NOT EXISTS "
    "FOR (n:GeneratedWork) REQUIRE n.work_id IS UNIQUE",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", default=os.environ.get(
        "NEO4J_URI", "bolt://localhost:7687"))
    ap.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    ap.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD", ""))
    ap.add_argument("--database", default=os.environ.get("NEO4J_DATABASE"))
    ap.add_argument("--wipe", action="store_true",
                    help="导入前清空数据库（危险，默认关闭）")
    args = ap.parse_args()

    print("连接 %s (user=%s) …" % (args.uri, args.user))
    try:
        drv = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
        drv.verify_connectivity()
    except Exception as e:
        print("连接失败：%s" % e)
        print("\n请确认 Neo4j 已启动，且用户名/密码正确。")
        return 1
    print("连接成功。\n")

    d = ROOT

    def run(cypher, **p):
        # 注意：第一个参数名不能叫 q —— Cypher 语句里存在名为 $q 的参数
        # （quality_status），位置传参 + 关键字传参会撞车。
        with drv.session(database=args.database) as s:
            return list(s.run(cypher, **p))

    if args.wipe:
        print("⚠ --wipe：正在清空数据库 …")
        run("MATCH (n) DETACH DELETE n")
        print("已清空。\n")

    # ---- 约束 ----
    print("[1/5] 建立约束 / 索引 …")
    for c in CONSTRAINTS:
        try:
            run(c)
        except Exception as e:
            print("  跳过：%s (%s)" % (c.split()[2], e))
    print("  完成 %d 条" % len(CONSTRAINTS))

    # ---- 纹样组 ----
    groups = read_csv(os.path.join(d, "graph", "csv", "nodes_groups.csv"))
    print("[2/5] 导入纹样组 %d …" % len(groups))
    for r in groups:
        gid = g_(r, "group_id")
        if not gid:
            continue
        run("""
            MERGE (g:MotifGroup {group_id: $gid})
            SET g.region = $region, g.technique = $tech,
                g.dominant_color = $color, g.pattern_type = $ptype,
                g.image_count = $cnt
        """, gid=gid, region=g_(r, "region"), tech=g_(r, "technique"),
            color=g_(r, "dominant_color"), ptype=g_(r, "pattern_type"),
            cnt=g_(r, "image_count"))
        if g_(r, "region"):
            run("""
                MERGE (reg:Region {name: $n})
                WITH reg MATCH (g:MotifGroup {group_id: $gid})
                MERGE (g)-[:FROM_REGION]->(reg)
            """, n=g_(r, "region"), gid=gid)
        if g_(r, "technique"):
            run("""
                MERGE (t:Technique {name: $n})
                WITH t MATCH (g:MotifGroup {group_id: $gid})
                MERGE (g)-[:USES_TECHNIQUE]->(t)
            """, n=g_(r, "technique"), gid=gid)
    print("  完成")

    # ---- 图片 + 颜色 + 形态 ----
    meta = read_csv(os.path.join(d, "metadata", "images.csv"))
    print("[3/5] 导入图片 %d …" % len(meta))
    for r in meta:
        iid = g_(r, "image_id")
        if not iid:
            continue
        run("""
            MERGE (i:Image {image_id: $iid})
            SET i.file_name = $fn, i.variant = $v, i.variant_label = $vl,
                i.orientation = $o, i.width = $w, i.height = $h,
                i.aspect = $asp, i.mode = $mode, i.file_size_kb = $kb,
                i.quality_status = $q, i.markable = $mk, i.is_source = $src,
                i.group_id = $gid
        """, iid=iid, fn=g_(r, "file_name"),
            v=g_(r, "variant"), vl=g_(r, "variant_label"),
            o=g_(r, "orientation"), w=g_(r, "width"), h=g_(r, "height"),
            asp=g_(r, "aspect"), mode=g_(r, "mode"), kb=g_(r, "file_size_kb"),
            q=g_(r, "quality_status"), mk=g_(r, "markable"),
            src=g_(r, "is_source"), gid=g_(r, "group_id"))
        gid = g_(r, "group_id")
        if gid:
            # HAS_VARIANT：教学页（graph_teach.py）用，语义是「组 → 该组的变体图」
            run("""
                MATCH (g:MotifGroup {group_id: $gid}),
                      (i:Image {image_id: $iid})
                MERGE (g)-[:HAS_VARIANT]->(i)
            """, gid=gid, iid=iid)
            # BELONGS_TO：检索页（neo4j_client.py）用，语义是「图 → 所属组」
            # 两条关系方向相反但表达同一事实，保留是为了兼容两处既有代码
            run("""
                MATCH (g:MotifGroup {group_id: $gid}),
                      (i:Image {image_id: $iid})
                MERGE (i)-[:BELONGS_TO]->(g)
            """, gid=gid, iid=iid)
        color = g_(r, "dominant_color")
        if color:
            run("""
                MERGE (c:Color {name: $c})
                WITH c MATCH (i:Image {image_id: $iid})
                MERGE (i)-[:USES_COLOR]->(c)
            """, c=color, iid=iid)
        orient = g_(r, "orientation")
        if orient:
            run("""
                MERGE (f:Form {name: $o})
                WITH f MATCH (i:Image {image_id: $iid})
                MERGE (i)-[:HAS_FORM]->(f)
            """, o=orient, iid=iid)
    print("  完成")

    # ---- 派生关系 ----
    rels = read_csv(os.path.join(d, "graph", "csv", "rels_derived.csv"))
    print("[4/5] 导入派生关系 %d …" % len(rels))
    n_ok = 0
    for r in rels:
        a, b = g_(r, "image_id"), g_(r, "source_image_id")
        if not (a and b):
            continue
        run("""
            MATCH (x:Image {image_id: $a}), (y:Image {image_id: $b})
            MERGE (x)-[:DERIVED_FROM]->(y)
        """, a=a, b=b)
        n_ok += 1
    print("  完成 %d" % n_ok)

    # ---- 学生作品 ----
    works = read_csv(os.path.join(d, "metadata", "works.csv"))
    print("[5/5] 导入学生作品 %d …" % len(works))
    for w in works:
        wid = g_(w, "work_id")
        if not wid:
            continue
        run("""
            MERGE (k:GeneratedWork {work_id: $wid})
            SET k.prompt = $p, k.created_at = $c, k.output_path = $o
        """, wid=wid, p=g_(w, "prompt"), c=g_(w, "created_at"),
            o=g_(w, "output_path"))
        stu = g_(w, "student_name", "student_id")
        if stu:
            run("""
                MERGE (s:Student {name: $s})
                WITH s MATCH (k:GeneratedWork {work_id: $wid})
                MERGE (k)-[:CREATED_BY]->(s)
            """, s=stu, wid=wid)
        import re as _re
        for src in _re.split(r"[,\s]+", g_(w, "source_images")):
            if not src:
                continue
            run("""
                MATCH (k:GeneratedWork {work_id: $wid}),
                      (i:Image {image_id: $src})
                MERGE (k)-[:DERIVED_FROM]->(i)
            """, wid=wid, src=src)
    print("  完成")

    # ---- 汇总 ----
    print()
    n1 = run("MATCH (n) RETURN count(n) AS c")[0]["c"]
    n2 = run("MATCH ()-[r]->() RETURN count(r) AS c")[0]["c"]
    print("=" * 50)
    print("导入完成：节点 %d，关系 %d" % (n1, n2))
    print("=" * 50)
    for rec in run("MATCH (n) RETURN labels(n)[0] AS l, count(*) AS c "
                   "ORDER BY c DESC"):
        print("  %-16s %d" % (rec["l"], rec["c"]))
    print()
    for rec in run("MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c "
                   "ORDER BY c DESC"):
        print("  %-20s %d" % (rec["t"], rec["c"]))

    drv.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
