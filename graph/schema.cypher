// ============================================================================
// 苗绣纹样图数据库 · 结构定义（Schema）
// ============================================================================
// 用途：初始化 Neo4j 数据库的约束与索引
// 执行：在 Neo4j Browser 中粘贴运行，或 cypher-shell -f schema.cypher
//
// 图模型说明见同目录 model.md
// ============================================================================


// ---------------------------------------------------------------------------
// 一、节点唯一性约束
// ---------------------------------------------------------------------------
// 约束同时创建索引，保证导入可重复执行（MERGE 不会产生重复节点）

CREATE CONSTRAINT image_id_unique IF NOT EXISTS
FOR (n:Image) REQUIRE n.image_id IS UNIQUE;

CREATE CONSTRAINT group_id_unique IF NOT EXISTS
FOR (n:MotifGroup) REQUIRE n.group_id IS UNIQUE;

CREATE CONSTRAINT motif_id_unique IF NOT EXISTS
FOR (n:Motif) REQUIRE n.motif_id IS UNIQUE;

CREATE CONSTRAINT color_name_unique IF NOT EXISTS
FOR (n:Color) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT form_name_unique IF NOT EXISTS
FOR (n:Form) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT technique_name_unique IF NOT EXISTS
FOR (n:Technique) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT region_name_unique IF NOT EXISTS
FOR (n:Region) REQUIRE n.name IS UNIQUE;

CREATE CONSTRAINT student_id_unique IF NOT EXISTS
FOR (n:Student) REQUIRE n.student_id IS UNIQUE;

CREATE CONSTRAINT work_id_unique IF NOT EXISTS
FOR (n:GeneratedWork) REQUIRE n.work_id IS UNIQUE;

CREATE CONSTRAINT task_id_unique IF NOT EXISTS
FOR (n:CourseTask) REQUIRE n.task_id IS UNIQUE;


// ---------------------------------------------------------------------------
// 二、查询索引（加速教学检索界面的筛选）
// ---------------------------------------------------------------------------

CREATE INDEX image_variant_idx IF NOT EXISTS
FOR (n:Image) ON (n.variant);

CREATE INDEX image_orientation_idx IF NOT EXISTS
FOR (n:Image) ON (n.orientation);

CREATE INDEX image_quality_idx IF NOT EXISTS
FOR (n:Image) ON (n.quality_status);

CREATE INDEX image_markable_idx IF NOT EXISTS
FOR (n:Image) ON (n.markable);

CREATE INDEX image_pattern_type_idx IF NOT EXISTS
FOR (n:Image) ON (n.pattern_type);

CREATE INDEX work_review_idx IF NOT EXISTS
FOR (n:GeneratedWork) ON (n.review_status);


// ---------------------------------------------------------------------------
// 三、图模型总览（执行后可用命令查看）
// ---------------------------------------------------------------------------
// 全部节点标签：
//   Image          图片（含原始图与全部变体）
//   MotifGroup     纹样来源分组（同一张原图的所有变体归为一组）
//   Motif          纹样元素（标注后从图片中裁剪出的具体纹样）
//   Color          主色或配色
//   Form           构图与几何形态
//   Technique      刺绣技法
//   Region         地域来源
//   GeneratedWork  学生生成作品
//   Student        学生或小组
//   CourseTask     课程实验任务
//
// 全部关系类型：
//   (:Image)-[:BELONGS_TO]->(:MotifGroup)          图片归属来源分组
//   (:Image)-[:DERIVED_FROM]->(:Image)             变体派生自源图
//   (:Image)-[:CONTAINS]->(:Motif)                 图片包含纹样元素
//   (:Image)-[:USES_COLOR]->(:Color)               图片使用颜色
//   (:Image)-[:HAS_FORM]->(:Form)                  图片构图形态
//   (:Image)-[:USES_TECHNIQUE]->(:Technique)       图片技法
//   (:Image)-[:FROM_REGION]->(:Region)             图片来源地域
//   (:Motif)-[:HAS_COLOR]->(:Color)                纹样元素颜色
//   (:Motif)-[:HAS_FORM]->(:Form)                  纹样元素形态
//   (:GeneratedWork)-[:DERIVED_FROM]->(:Image)     作品派生自图片
//   (:GeneratedWork)-[:COMBINES]->(:Motif)         作品组合了纹样元素
//   (:GeneratedWork)-[:USES_COLOR]->(:Color)       作品使用颜色
//   (:GeneratedWork)-[:CREATED_BY]->(:Student)     作品作者
//   (:GeneratedWork)-[:SUBMITTED_FOR]->(:CourseTask) 作品提交至任务
