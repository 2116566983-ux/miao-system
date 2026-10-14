# -*- coding: utf-8 -*-
"""
Neo4j 连接与查询层

设计要点：
  - 连不上数据库时自动降级，不阻塞界面（教学场景网络不稳定）
  - 所有查询参数化，符合 Neo4j Driver 6.x 用法
  - 查询结果统一返回 pandas DataFrame，与 CSV 模式接口一致

用法：
    from neo4j_client import GraphClient
    client = GraphClient(uri, user, password)
    if client.available:
        df = client.search_images(variant='basic', markable='是')
"""

from __future__ import annotations

import os

try:
    import pandas as pd
except ImportError:
    pd = None

try:
    from neo4j import GraphDatabase
    from neo4j.exceptions import Neo4jError, ServiceUnavailable, AuthError
    DRIVER_AVAILABLE = True
except ImportError:
    DRIVER_AVAILABLE = False


# ---------------------------------------------------------------------------
# 查询语句集中管理
# ---------------------------------------------------------------------------

# 按条件检索图片，每组只取一张代表图（消除变体重复）
Q_SEARCH_IMAGES = """
MATCH (g:MotifGroup)<-[:BELONGS_TO]-(i:Image)
WHERE ($variant IS NULL OR i.variant = $variant)
  AND ($orientation IS NULL OR i.orientation = $orientation)
  AND ($markable IS NULL OR i.markable = $markable)
  AND ($only_source = false OR i.is_source = '是')
WITH g, i
ORDER BY i.is_source DESC, i.image_id
WITH g, collect(i)[0] AS rep
RETURN rep.image_id       AS image_id,
       rep.file_name      AS file_name,
       rep.variant        AS variant,
       rep.variant_label  AS variant_label,
       rep.orientation    AS orientation,
       rep.width          AS width,
       rep.height         AS height,
       rep.file_size_kb   AS file_size_kb,
       rep.quality_status AS quality_status,
       rep.markable       AS markable,
       g.group_id         AS group_id
ORDER BY image_id
SKIP $skip LIMIT $limit
"""

# 统计符合条件的分组总数
Q_COUNT_GROUPS = """
MATCH (g:MotifGroup)<-[:BELONGS_TO]-(i:Image)
WHERE ($variant IS NULL OR i.variant = $variant)
  AND ($orientation IS NULL OR i.orientation = $orientation)
  AND ($markable IS NULL OR i.markable = $markable)
  AND ($only_source = false OR i.is_source = '是')
RETURN count(DISTINCT g) AS total
"""

# 取某个分组的全部变体
Q_GROUP_VARIANTS = """
MATCH (g:MotifGroup {group_id: $group_id})<-[:BELONGS_TO]-(i:Image)
RETURN i.image_id AS image_id, i.file_name AS file_name,
       i.variant AS variant, i.variant_label AS variant_label,
       i.width AS width, i.height AS height
ORDER BY i.variant
"""

# 追溯派生链路（版权溯源）
Q_DERIVATION_CHAIN = """
MATCH path = (i:Image {image_id: $image_id})-[:DERIVED_FROM*0..3]->(root:Image)
RETURN [n IN nodes(path) | n.image_id] AS chain
LIMIT 1
"""

# 标注进度看板（教师端）
Q_ANNOTATION_PROGRESS = """
MATCH (g:MotifGroup)
RETURN count(g) AS total_groups,
       sum(CASE WHEN g.region IS NOT NULL THEN 1 ELSE 0 END) AS annotated_region,
       sum(CASE WHEN g.technique IS NOT NULL THEN 1 ELSE 0 END) AS annotated_technique,
       sum(CASE WHEN g.dominant_color IS NOT NULL THEN 1 ELSE 0 END) AS annotated_color
"""

# 按方向 + 可标注筛选（供界面下拉选项）
Q_FILTER_OPTIONS = """
MATCH (i:Image)
WHERE i.markable = '是'
RETURN DISTINCT i.orientation AS orientation
ORDER BY orientation
"""


class GraphClient:
    """Neo4j 客户端，带自动降级。

    连不上数据库时 available 为 False，调用方应回退到 CSV 模式。
    """

    def __init__(self, uri="bolt://localhost:7687", user="neo4j",
                 password="", database=None, timeout=3.0):
        self.uri = uri
        self.user = user
        self.database = database
        self.available = False
        self.error = None
        self._driver = None

        if not DRIVER_AVAILABLE:
            self.error = "未安装 neo4j 驱动（pip install neo4j）"
            return

        try:
            self._driver = GraphDatabase.driver(
                uri, auth=(user, password),
                connection_timeout=timeout,
            )
            self._driver.verify_connectivity()
            self.available = True
        except AuthError as e:
            self.error = "认证失败：用户名或密码不正确（%s）" % e
        except ServiceUnavailable as e:
            self.error = "无法连接数据库：%s" % e
        except Exception as e:
            self.error = "连接异常：%s" % e

    # ------------------------------------------------------------------
    # 底层执行
    # ------------------------------------------------------------------
    def run(self, query, **params):
        """执行查询，返回记录列表。数据库不可用时返回 None。"""
        if not self.available or self._driver is None:
            return None
        try:
            with self._driver.session(database=self.database) as session:
                result = session.run(query, **params)
                return [dict(r) for r in result]
        except Exception as e:
            self.error = "查询失败：%s" % e
            return None

    def _to_df(self, records):
        if records is None:
            return None
        if pd is None or not records:
            return records if records is not None else None
        return pd.DataFrame(records)

    # ------------------------------------------------------------------
    # 业务查询
    # ------------------------------------------------------------------
    def search_images(self, variant=None, orientation=None,
                      markable='是', only_source=False, skip=0, limit=24):
        """检索图片，按分组去重后返回代表图。"""
        records = self.run(
            Q_SEARCH_IMAGES,
            variant=variant,
            orientation=orientation,
            markable=markable,
            only_source=only_source,
            skip=int(skip),
            limit=int(limit),
        )
        return self._to_df(records)

    def count_groups(self, variant=None, orientation=None,
                     markable='是', only_source=False):
        """统计符合条件的分组总数。"""
        records = self.run(
            Q_COUNT_GROUPS,
            variant=variant,
            orientation=orientation,
            markable=markable,
            only_source=only_source,
        )
        if not records:
            return 0
        return records[0].get("total", 0)

    def group_variants(self, group_id):
        """取某分组的全部变体。"""
        records = self.run(Q_GROUP_VARIANTS, group_id=group_id)
        return self._to_df(records)

    def derivation_chain(self, image_id):
        """追溯派生链路。"""
        records = self.run(Q_DERIVATION_CHAIN, image_id=image_id)
        if not records:
            return []
        return records[0].get("chain", [])

    def annotation_progress(self):
        """标注进度看板。"""
        records = self.run(Q_ANNOTATION_PROGRESS)
        if not records:
            return None
        return records[0]

    def filter_options(self):
        """可用的方向选项。"""
        records = self.run(Q_FILTER_OPTIONS)
        if records is None:
            return None
        return [r["orientation"] for r in records]

    def close(self):
        if self._driver is not None:
            try:
                self._driver.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 从环境变量创建客户端（推荐方式，避免密码写进代码）
# ---------------------------------------------------------------------------
def client_from_env():
    """从环境变量读取连接信息。

    支持的环境变量：
        NEO4J_URI       默认 bolt://localhost:7687
        NEO4J_USER      默认 neo4j
        NEO4J_PASSWORD  默认空
        NEO4J_DATABASE  默认使用服务端默认库
    """
    return GraphClient(
        uri=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        user=os.environ.get("NEO4J_USER", "neo4j"),
        password=os.environ.get("NEO4J_PASSWORD", ""),
        database=os.environ.get("NEO4J_DATABASE") or None,
    )


if __name__ == "__main__":
    import json

    print("neo4j 驱动可用：", DRIVER_AVAILABLE)
    c = client_from_env()
    print("连接目标：", c.uri)
    print("连接状态：", "已连接" if c.available else "未连接")
    if not c.available:
        print("原因：", c.error)
        print()
        print("这是预期结果 —— 本机尚未安装 Neo4j 服务。")
        print("界面会自动降级为 CSV 模式，功能不受影响。")
    else:
        print(json.dumps(c.annotation_progress(), ensure_ascii=False, indent=2))
    c.close()
