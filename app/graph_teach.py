# -*- coding: utf-8 -*-
"""
图数据库教学模块 · 数据层

教学定位（这是整个模块存在的理由）：
    《数据采集与清洗》这门课的核心命题之一是「数据怎么组织」。
    苗绣纹样的知识天然是**网状**的：一朵花既属于某个地域，又用了某种技法，
    又有若干变体，又被某件学生作品引用……用二维表格存，会立刻爆炸成
    一堆外键和中间表；用图存，关系本身就是一等公民。

    但「图数据库」对大多数学生是抽象的。所以这个模块要让学生**亲手**
    看到：同一个问题，表格要 JOIN 三层，图只要走一步。

设计原则：
    1. **真实 Neo4j 优先，CSV 兜底** —— 有数据库就连，连不上就用 CSV
       在内存里建一张等价的小图。两条路径返回**完全相同的数据结构**，
       界面代码不必关心数据从哪来。
    2. **教学要能"对照"** —— 每次查询都同时给出「Cypher 写法」和
       「SQL 写法」，让学生看见两种范式的差别，而不是背语法。
    3. **不依赖数据库也能上课** —— 课堂/答辩最怕服务没启动。

本模块**只做数据，不做界面**，方便单测。
"""

from __future__ import annotations

import csv
import io
import os
import re

# ---------------------------------------------------------------------------
# 图模型定义 —— 与 graph/schema.cypher、docs 里的设计保持一致
# ---------------------------------------------------------------------------

#: 节点标签 → 中文名 / 说明 / 标记
#:
#: 第三个元素是**单字标记**，原先用的 emoji 在表格里大小不一、
#: 颜色杂乱，反而干扰阅读。单字在等宽表格里能对齐，也更接近
#: 学术图表的画法（如 UML 里的类名前缀）。
NODE_LABELS = {
    "Image": ("苗绣图片", "一张采集进来的实物照片（含变体）", "图"),
    "MotifGroup": ("纹样组", "同一纹样的一组图片（源图 + 变体）", "组"),
    "Motif": ("纹样元素", "从图片中提取出的纹样符号", "纹"),
    "Color": ("颜色", "主色 / 配色", "色"),
    "Form": ("构图形态", "几何构图与对称形式", "形"),
    "Technique": ("刺绣技法", "破线绣、平绣等", "技"),
    "Region": ("地域", "纹样的来源地", "域"),
    "GeneratedWork": ("生成作品", "学生用 AI 重组出的作品", "作"),
    "Student": ("学生", "作者或小组", "生"),
    "CourseTask": ("课程任务", "实验任务定义", "课"),
}

#: 关系类型 → (起点, 终点, 中文名, 说明)
REL_TYPES = {
    "HAS_VARIANT": ("MotifGroup", "Image", "包含变体", "一个纹样组包含若干图片"),
    "DERIVED_FROM": ("Image", "Image", "派生自", "变体图由源图处理而来"),
    "CONTAINS": ("Image", "Motif", "包含纹样", "图片里提取出的纹样元素"),
    "USES_COLOR": ("Image", "Color", "使用颜色", "图片用到的主色/配色"),
    "HAS_FORM": ("Image", "Form", "具有形态", "图片的构图与对称形式"),
    "USES_TECHNIQUE": ("Image", "Technique", "使用技法", "采用的刺绣技法"),
    "FROM_REGION": ("Image", "Region", "来自地域", "纹样的地域来源"),
    "DERIVED_FROM_WORK": ("GeneratedWork", "Image", "衍生自", "作品参考的原始图片"),
    "COMBINES": ("GeneratedWork", "Motif", "组合纹样", "作品组合了哪些纹样"),
    "CREATED_BY": ("GeneratedWork", "Student", "作者", "作品由谁创作"),
    "SUBMITTED_FOR": ("GeneratedWork", "CourseTask", "提交至", "作品提交给哪个任务"),
}


# ---------------------------------------------------------------------------
# 教学用的查询预设 —— 每条都配 SQL 对照，这是教学重点
# ---------------------------------------------------------------------------

QUERY_PRESETS = [
    {
        "id": "q_variant_chain",
        "name": "① 一朵纹样的全部变体",
        "question": "找出 basic_0 这张图的所有变体（它和它的派生图）",
        "cypher": (
            "MATCH (src:Image {image_id: $iid})<-[:DERIVED_FROM]-(v:Image)\n"
            "RETURN src.image_id AS 源图, v.image_id AS 变体, v.variant AS 类型\n"
            "ORDER BY v.image_id"
        ),
        "sql": (
            "-- 需要自连接（表自己 JOIN 自己）\n"
            "SELECT s.image_id AS 源图, v.image_id AS 变体, v.variant AS 类型\n"
            "FROM images s\n"
            "JOIN images v ON v.source_image_id = s.image_id\n"
            "WHERE s.image_id = 'basic_0';"
        ),
        "why": "关系在表里是「一个外键列」，在图里是「一条边」。"
               "查一层的变体两者都能做，但图不用写 JOIN。",
        "params_hint": {"iid": "basic_0"},
    },
    {
        "id": "q_two_hop",
        "name": "② 两跳：变体的变体",
        "question": "找出 basic_0 的「变体的变体」（两跳派生关系）",
        "cypher": (
            "MATCH (src:Image {image_id: $iid})<-[:DERIVED_FROM*1..2]-(v:Image)\n"
            "RETURN DISTINCT v.image_id AS 变体, v.variant AS 类型\n"
            "ORDER BY v.image_id"
        ),
        "sql": (
            "-- 两跳就要 JOIN 两次，三跳就要 JOIN 三次\n"
            "SELECT DISTINCT v2.image_id AS 变体\n"
            "FROM images s\n"
            "JOIN images v1 ON v1.source_image_id = s.image_id\n"
            "JOIN images v2 ON v2.source_image_id = v1.image_id\n"
            "WHERE s.image_id = 'basic_0';"
        ),
        "why": "**这是图的杀手级场景。** Cypher 里写 `*1..2` 就表示"
               "「1 到 2 跳」，改数字即可换深度；SQL 里每加一跳就要多写一个 "
               "JOIN，而且**跳数固定**，没法在运行时改。",
        "params_hint": {"iid": "basic_0"},
    },
    {
        "id": "q_neighbors",
        "name": "③ 一张图的全部邻居",
        "question": "看 basic_8 这张图，围绕它的所有直接邻居（变体、颜色、形态…）",
        "cypher": (
            "MATCH (i:Image {image_id: $iid})-[r]-(other)\n"
            "RETURN type(r) AS 关系, labels(other)[0] AS 邻居类型,\n"
            "       coalesce(other.name, other.image_id, other.group_id) AS 邻居\n"
            "ORDER BY 关系"
        ),
        "sql": (
            "-- 每种关系都在不同的表里，要 UNION 起来\n"
            "SELECT 'HAS_VARIANT' AS 关系, image_id AS 邻居 FROM images\n"
            "  WHERE source_image_id = 'basic_8'\n"
            "UNION ALL\n"
            "SELECT 'USES_COLOR', name FROM image_colors\n"
            "  WHERE image_id = 'basic_8'\n"
            "UNION ALL\n"
            "-- …… 还有 N 张表要写\n"
            "SELECT 'HAS_FORM', name FROM image_forms WHERE image_id = 'basic_8';"
        ),
        "why": "图的「邻居」概念天然统一：不管什么关系，都是从一个点出发的边。"
               "在关系表里，不同关系散落在不同表，要拼起来只能 UNION，"
               "而且**表越多越难维护**。",
        "params_hint": {"iid": "basic_8"},
    },
    {
        "id": "q_shared_color",
        "name": "④ 共色的图片（推荐/关联）",
        "question": "找出与 basic_0 用同一种颜色的其他图片",
        "cypher": (
            "MATCH (a:Image {image_id: $iid})-[:USES_COLOR]->(c:Color)\n"
            "      <-[:USES_COLOR]-(b:Image)\n"
            "WHERE a <> b\n"
            "RETURN c.name AS 共同颜色, count(DISTINCT b) AS 同色图片数\n"
            "ORDER BY 同色图片数 DESC"
        ),
        "sql": (
            "-- 中间表 + 两次 JOIN + 分组\n"
            "SELECT c.name AS 共同颜色, COUNT(DISTINCT b.image_id) AS 同色图片数\n"
            "FROM image_colors a\n"
            "JOIN colors c ON c.id = a.color_id\n"
            "JOIN image_colors b ON b.color_id = a.color_id\n"
            "WHERE a.image_id = 'basic_0' AND b.image_id <> 'basic_0'\n"
            "GROUP BY c.name ORDER BY 同色图片数 DESC;"
        ),
        "why": "这是**推荐系统**的经典模式（「看了 A 的人还看了 B」）。"
               "图里就是「A→共同属性←B」这个 V 字形，直觉上非常清楚；"
               "关系表里则是中间表的自连接，读起来要绕几圈。",
        "params_hint": {"iid": "basic_0"},
    },
    {
        "id": "q_count_by_region",
        "name": "⑤ 按地域统计纹样分布",
        "question": "每个地域有多少组纹样、多少张图",
        "cypher": (
            "MATCH (g:MotifGroup)-[:FROM_REGION]->(r:Region)\n"
            "OPTIONAL MATCH (g)-[:HAS_VARIANT]->(i:Image)\n"
            "RETURN r.name AS 地域, count(DISTINCT g) AS 纹样组数,\n"
            "       count(i) AS 图片数\n"
            "ORDER BY 图片数 DESC LIMIT 20"
        ),
        "sql": (
            "SELECT r.name AS 地域, COUNT(DISTINCT g.id) AS 纹样组数,\n"
            "       COUNT(i.id) AS 图片数\n"
            "FROM groups g\n"
            "JOIN regions r ON r.id = g.region_id\n"
            "LEFT JOIN images i ON i.group_id = g.id\n"
            "GROUP BY r.name ORDER BY 图片数 DESC LIMIT 20;"
        ),
        "why": "聚合统计两种范式差别不大，图不占优势。"
               "**但要诚实告诉学生：不是所有问题都该上图。** "
               "单纯的计数、报表，关系型数据库完全够用，甚至更成熟。"
               "图的价值在于**连接**，不在于统计。",
        "params_hint": {},
    },
    {
        "id": "q_tech_region",
        "name": "⑥ 跨维度：技法 × 地域",
        "question": "哪些「地域 + 技法」组合最常见",
        "cypher": (
            "MATCH (g:MotifGroup)-[:USES_TECHNIQUE]->(t:Technique)\n"
            "OPTIONAL MATCH (g)-[:FROM_REGION]->(r:Region)\n"
            "RETURN coalesce(r.name, '未标注') AS 地域, t.name AS 技法,\n"
            "       count(*) AS 组数\n"
            "ORDER BY 组数 DESC LIMIT 20"
        ),
        "sql": (
            "SELECT COALESCE(r.name,'未标注') AS 地域, t.name AS 技法,\n"
            "       COUNT(*) AS 组数\n"
            "FROM groups g\n"
            "JOIN techniques t ON t.id = g.technique_id\n"
            "LEFT JOIN regions r ON r.id = g.region_id\n"
            "GROUP BY 地域, 技法 ORDER BY 组数 DESC LIMIT 20;"
        ),
        "why": "两个维度交叉统计。这类「多对多交叉」在图里就是从一个节点"
               "分别出发走两条边，非常直白。",
        "params_hint": {},
    },
]

#: 关系表 vs 图 的直观对比（教学用，静态内容）
TABLE_VS_GRAPH = {
    "标题": "同一份数据，两种存法",
    "left_name": "关系型（表格 / SQL）",
    "right_name": "图（节点 + 关系 / Cypher）",
    "rows": [
        ("数据长什么样",
         "拆成一堆表：images、groups、colors、\n"
         "image_colors、techniques……表之间靠外键连",
         "一张图：圆圈是节点，箭头是关系，\n"
         "关系自己也能带属性"),
        ("查一层关系",
         "JOIN 一次",
         "写一条边 `-[:DERIVED_FROM]->`"),
        ("查 N 层关系",
         "N 跳就写 N 个 JOIN，**跳数写死在 SQL 里**",
         "`-[:DERIVED_FROM*1..5]->` 想查几跳改数字"),
        ("加一种新关系",
         "新建一张中间表 + 改所有相关查询",
         "直接连一条新边，**已有查询不用改**"),
        ("查「两个东西怎么连起来」",
         "递归 CTE，写起来非常痛苦",
         "`shortestPath()` 一句话"),
        ("适合什么",
         "报表、统计、事务、强一致性",
         "关联、路径、推荐、知识图谱"),
    ],
    "结论": "不是「图数据库比关系数据库好」，而是**它们擅长的问题不同**。"
            "苗绣纹样这种「多对多、关系本身就是知识」的数据，"
            "用图来组织会更自然；而课程成绩统计这种规整数据，"
            "关系表反而是更好的选择。",
}


# ---------------------------------------------------------------------------
# 从 CSV 构建内存图 —— 无 Neo4j 时的等价实现
# ---------------------------------------------------------------------------

def _read_csv(path, encoding="utf-8-sig"):
    if not os.path.exists(path):
        return []
    with io.open(path, encoding=encoding, newline="") as f:
        return list(csv.DictReader(f))


def _g(row, *keys, default=""):
    """从行里按若干候选列名取值，取到第一个非空的。"""
    for k in keys:
        v = (row.get(k) or "").strip()
        if v:
            return v
    return default


class CsvGraph:
    """用 CSV 在内存里建一张等价的小图。

    它 **不是** 要模拟 Neo4j，而是为了让界面在没有数据库时也能跑，
    并且返回与 Neo4j 路径**完全相同**的结构。

    内部就是三张表：节点、关系、以及从节点出发的邻接索引。
    """

    def __init__(self, project_dir):
        self.project_dir = project_dir
        self.nodes = {}      # node_id -> {"id","label","name","props"}
        self.rels = []       # {"type","start","end","props"}
        self._out = {}       # node_id -> [rel_idx]
        self._build()

    # -- 便利方法 ------------------------------------------------------
    def _add_node(self, nid, label, name, props=None):
        """添加节点。props 用字典传，避免与 name 位置参数冲突。"""
        props = props or {}
        props = {k: v for k, v in props.items() if v}
        if nid in self.nodes:
            self.nodes[nid]["props"].update(props)
            return
        self.nodes[nid] = {"id": nid, "label": label, "name": name,
                           "props": props}

    def _add_rel(self, rtype, start, end, props=None):
        if start not in self.nodes or end not in self.nodes:
            return
        idx = len(self.rels)
        self.rels.append({"type": rtype, "start": start, "end": end,
                          "props": props or {}})
        self._out.setdefault(start, []).append(idx)

    # -- 构建 ----------------------------------------------------------
    def _build(self):
        d = self.project_dir
        groups = _read_csv(os.path.join(d, "graph", "csv",
                                        "nodes_groups.csv"))
        images = _read_csv(os.path.join(d, "graph", "csv",
                                        "nodes_images.csv"))
        derived = _read_csv(os.path.join(d, "graph", "csv",
                                         "rels_derived.csv"))
        works = _read_csv(os.path.join(d, "metadata", "works.csv"))

        # ---- 纹样组 ----
        for g_ in groups:
            gid = _g(g_, "group_id")
            if not gid:
                continue
            self._add_node("group:" + gid, "MotifGroup", gid, props={
                "group_id": gid,
                "region": _g(g_, "region"),
                "technique": _g(g_, "technique"),
                "dominant_color": _g(g_, "dominant_color"),
                "pattern_type": _g(g_, "pattern_type"),
                "image_count": _g(g_, "image_count"),
            })
            region = _g(g_, "region")
            if region:
                self._add_node("region:" + region, "Region", region)
                self._add_rel("FROM_REGION", "group:" + gid,
                              "region:" + region)
            tech = _g(g_, "technique")
            if tech:
                self._add_node("tech:" + tech, "Technique", tech)
                self._add_rel("USES_TECHNIQUE", "group:" + gid,
                              "tech:" + tech)

        # ---- 图片 ----
        for im in images:
            iid = _g(im, "image_id")
            if not iid:
                continue
            self._add_node("img:" + iid, "Image", iid, props={
                "image_id": iid,
                "variant": _g(im, "variant"),
                "variant_label": _g(im, "variant_label"),
                "orientation": _g(im, "orientation"),
                "width": _g(im, "width"),
                "height": _g(im, "height"),
                "quality_status": _g(im, "quality_status"),
            })

            form = _g(im, "orientation")
            if form:
                self._add_node("form:" + form, "Form", form)
                self._add_rel("HAS_FORM", "img:" + iid, "form:" + form)

        # ---- 图片 → 纹样组（源图归属）+ 颜色 ----
        # nodes_images.csv 的 dominant_color 是空的，真正的颜色线索在
        # metadata/images.csv 里，所以颜色节点统一在这里建。
        meta = _read_csv(os.path.join(d, "metadata", "images.csv"))
        for m in meta:
            iid = _g(m, "image_id")
            gid = _g(m, "group_id")
            if iid and gid and ("img:" + iid) in self.nodes:
                self._add_rel("HAS_VARIANT", "group:" + gid, "img:" + iid)
            if iid and ("img:" + iid) in self.nodes:
                color = _g(m, "dominant_color")
                if color and ("color:" + color) not in self.nodes:
                    self._add_node("color:" + color, "Color", color)
                if color:
                    self._add_rel("USES_COLOR", "img:" + iid,
                                  "color:" + color)

        # ---- 派生关系 ----
        for r in derived:
            a = _g(r, "image_id")
            b = _g(r, "source_image_id")
            if a and b:
                self._add_rel("DERIVED_FROM", "img:" + a, "img:" + b)

        # ---- 学生作品 ----
        for w in works:
            wid = _g(w, "work_id")
            if not wid:
                continue
            self._add_node("work:" + wid, "GeneratedWork", wid, props={
                "work_id": wid,
                "prompt": _g(w, "prompt"),
                "created_at": _g(w, "created_at"),
                "output_path": _g(w, "output_path"),
            })
            student = _g(w, "student_name", "student_id")
            if student:
                self._add_node("student:" + student, "Student", student)
                self._add_rel("CREATED_BY", "work:" + wid,
                              "student:" + student)
            for src in re.split(r"[,\s]+", _g(w, "source_images")):
                if src and ("img:" + src) in self.nodes:
                    self._add_rel("DERIVED_FROM", "work:" + wid, "img:" + src)

    # -- 统计 ----------------------------------------------------------
    def stats(self):
        by_label = {}
        for n in self.nodes.values():
            by_label[n["label"]] = by_label.get(n["label"], 0) + 1
        by_rel = {}
        for r in self.rels:
            by_rel[r["type"]] = by_rel.get(r["type"], 0) + 1
        return {"nodes": len(self.nodes), "rels": len(self.rels),
                "by_label": by_label, "by_rel": by_rel}

    # -- 查询实现（与预设一一对应）---------------------------------------
    def _neighbors(self, nid):
        """返回与该节点直接相连的 (关系类型, 方向, 邻居节点)。"""
        out = []
        for idx in self._out.get(nid, []):
            r = self.rels[idx]
            out.append((r["type"], "out", r["end"]))
        # 入边：扫一遍（数据量小，够用）
        for r in self.rels:
            if r["end"] == nid:
                out.append((r["type"], "in", r["start"]))
        return out

    def q_variant_chain(self, iid):
        rows = []
        for r in self.rels:
            if r["type"] == "DERIVED_FROM" and r["end"] == "img:" + iid:
                v = self.nodes[r["start"]]
                if v["label"] == "Image":
                    rows.append({"源图": iid, "变体": v["props"].get("image_id"),
                                 "类型": v["props"].get("variant")})
        return sorted(rows, key=lambda x: x["变体"] or "")

    def q_two_hop(self, iid, depth=2):
        seen = set()
        frontier = {"img:" + iid}
        for _ in range(depth):
            nxt = set()
            for r in self.rels:
                if r["type"] == "DERIVED_FROM" and r["end"] in frontier:
                    nxt.add(r["start"])
            frontier = nxt
            seen |= nxt
        rows = []
        for nid in sorted(seen):
            n = self.nodes.get(nid)
            if n and n["label"] == "Image":
                rows.append({"变体": n["props"].get("image_id"),
                             "类型": n["props"].get("variant")})
        return rows

    def q_neighbors(self, iid):
        rows = []
        for rtype, direction, other in self._neighbors("img:" + iid):
            n = self.nodes.get(other)
            if not n:
                continue
            rows.append({"关系": rtype, "邻居类型": n["label"],
                         "邻居": n["name"]})
        return sorted(rows, key=lambda x: x["关系"])

    def q_shared_color(self, iid):
        # 颜色关系方向是 img -> color，所以取 start 为该图的边
        colors = [self.nodes[r["end"]]["name"] for r in self.rels
                  if r["type"] == "USES_COLOR" and r["start"] == "img:" + iid]
        rows = []
        for c in colors:
            cnt = 0
            for r in self.rels:
                if (r["type"] == "USES_COLOR" and r["end"] == "color:" + c
                        and r["start"] != "img:" + iid):
                    cnt += 1
            rows.append({"共同颜色": c, "同色图片数": cnt})
        return sorted(rows, key=lambda x: -x["同色图片数"])

    def q_count_by_region(self):
        agg = {}
        for r in self.rels:
            if r["type"] == "FROM_REGION":
                reg = self.nodes[r["end"]]["name"]
                agg.setdefault(reg, {"组": 0, "图": 0})
                agg[reg]["组"] += 1
        for r in self.rels:
            if r["type"] == "HAS_VARIANT":
                g = self.nodes.get(r["start"])
                if not g:
                    continue
                reg = g["props"].get("region") or "未标注"
                agg.setdefault(reg, {"组": 0, "图": 0})
                agg[reg]["图"] += 1
        rows = [{"地域": k, "纹样组数": v["组"], "图片数": v["图"]}
                for k, v in agg.items()]
        return sorted(rows, key=lambda x: -x["图片数"])[:20]

    def q_tech_region(self):
        agg = {}
        for r in self.rels:
            if r["type"] == "USES_TECHNIQUE":
                g = self.nodes.get(r["start"])
                t = self.nodes.get(r["end"])
                if not (g and t):
                    continue
                reg = (g["props"].get("region") or "未标注")
                key = (reg, t["name"])
                agg[key] = agg.get(key, 0) + 1
        rows = [{"地域": k[0], "技法": k[1], "组数": v}
                for k, v in agg.items()]
        return sorted(rows, key=lambda x: -x["组数"])[:20]

    def run_preset(self, preset_id, params=None):
        params = params or {}
        iid = params.get("iid", "basic_0")
        fn = {
            "q_variant_chain": lambda: self.q_variant_chain(iid),
            "q_two_hop": lambda: self.q_two_hop(iid),
            "q_neighbors": lambda: self.q_neighbors(iid),
            "q_shared_color": lambda: self.q_shared_color(iid),
            "q_count_by_region": self.q_count_by_region,
            "q_tech_region": self.q_tech_region,
        }.get(preset_id)
        if fn is None:
            return []
        return fn()


def preset_by_id(pid):
    for p in QUERY_PRESETS:
        if p["id"] == pid:
            return p
    return None


def build_csv_graph(project_dir):
    """构建 CSV 内存图；任何异常都返回 None，由调用方处理。"""
    try:
        return CsvGraph(project_dir)
    except Exception:
        return None
