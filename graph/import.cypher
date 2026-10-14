// ============================================================================
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
// 节点数量（期望：Image = 1000，MotifGroup = 250）
MATCH (i:Image) RETURN count(i) AS image_count;
MATCH (g:MotifGroup) RETURN count(g) AS group_count;

// 关系数量（期望：BELONGS_TO = 1000，DERIVED_FROM = 750）
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
