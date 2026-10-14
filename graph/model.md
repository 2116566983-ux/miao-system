# 图模型设计说明

## 一、要解决的问题

实测 `archive/byx` 的 1000 张图片后确认：**这不是 1000 个独立样本，而是 250 张原图 × 4 种变体。**

| 变体 | 数量 | 与同组其他图的关系 |
|---|---|---|
| `basic` | 250 | 原始图，组的基准 |
| `texture` | 250 | 内容相同，尺寸不同 |
| `color` | 250 | 与 `texture` 尺寸完全相同，色彩不同 |
| `mixup` | 250 | 多尺度拼贴合成 |

命名规律：序号 `% 4` 与变体严格对应，校验零例外。

**如果直接把 1000 张当作平行节点导入，会产生两个后果：**

1. 检索时同一张苗绣出现 4 次，学生看到的重复内容占 3/4
2. 标注工作量虚高清空，实际上只需标注 250 张原图

所以模型必须表达"变体是派生关系"，而不是"四类并列"。

---

## 二、核心决策：为什么引入 `MotifGroup` 节点

这是之前列为"待验证"的问题。现在的处理方式是**双轨表达**，而不是二选一：

### 轨道 1：`MotifGroup` 节点（分组锚点）

```
(:Image)-[:BELONGS_TO]->(:MotifGroup)
```

- 每个 `MotifGroup` 代表一张来源苗绣，下面的 4 张图是它的四个版本
- 好处：检索界面可以**默认按组聚合**，每组只显示一张代表图，直接消除重复
- 好处：后续标注"地域、技法"等**组级属性**时，只需在组上标一次，4 个变体自动继承

### 轨道 2：`DERIVED_FROM` 关系（派生链路）

```
(:Image)-[:DERIVED_FROM]->(:Image)
```

- 保留精确的"哪张派生自哪张"信息，可以表达多级派生
- 好处：计算机专业学生可以用它练习路径查询，比如"找出所有经过色彩处理的图"
- 好处：数据溯源时能回答"这张图的原始素材是哪张"

**为什么两个都要：** `MotifGroup` 解决**检索聚合**和**属性继承**，`DERIVED_FROM` 解决**数据溯源**和**教学深度**。前者是给学生用的，后者是给技术拓展层和版权追溯用的。只用其中一个都会缺一块。

---

## 三、这样设计后，上次那个"待验证项"有答案了

用户在方案汇报中曾写"可扩展，不用重构系统"。现在可以给出的准确表述是：

> 变体关系通过 `MotifGroup` 节点与 `DERIVED_FROM` 关系双轨表达。
> 已用 1000 张真实图片验证分组规律（250 组，每组 4 张，零例外）。
> `region`、`technique`、`dominant_color` 等标注字段目前留空，回填后**不需要修改节点定义**，只需 `SET` 属性——
> 因为分组锚点已经预留，属性挂在 `MotifGroup` 上即可被 4 个变体共享。

**这说明一件具体的事：** 把标注属性挂在 `MotifGroup` 而非 `Image` 上，是"不用重构"能成立的关键。如果当初把地域、技法直接挂在 `Image` 上，回填时就要重复标 4 次，且改口径要改 1000 条。

---

## 四、节点属性定义

### `Image` 图片

| 属性 | 来源 | 说明 |
|---|---|---|
| `image_id` | 自动 | 唯一标识 |
| `file_name` | 自动 | 文件名 |
| `variant` | 自动 | basic / texture / color / mixup |
| `variant_label` | 自动 | 中文名 |
| `width` / `height` | 自动 | 像素尺寸 |
| `aspect` | 自动 | 宽高比 |
| `orientation` | 自动 | 横图 / 竖图 / 方图 |
| `mode` | 自动 | 色彩模式 |
| `file_size_kb` | 自动 | 文件大小 |
| `quality_status` | 自动 | 可标注 / 尺寸过小 / 拼贴图 |
| `markable` | 自动 | 是 / 否 |
| `is_source` | 自动 | 是否组内源图 |
| `pattern_type` | **待标注** | 纹样类型 |
| `symmetry` | **待标注** | 对称性 |
| `source` | **待标注** | 素材来源 |
| `permission_note` | **待标注** | 授权说明 |

### `MotifGroup` 来源分组

| 属性 | 来源 | 说明 |
|---|---|---|
| `group_id` | 自动 | 分组唯一标识 |
| `source_image_id` | 自动 | 组内源图 |
| `image_count` | 自动 | 组内图片数 |
| `region` | **待标注** | 地域来源（组级） |
| `technique` | **待标注** | 刺绣技法（组级） |
| `dominant_color` | **待标注** | 主色调（组级） |
| `pattern_type` | **待标注** | 纹样类型（组级） |

> 注意：`region`、`technique` 等属性**同时存在于 `Image` 和 `MotifGroup`**。推荐只填 `MotifGroup`，`Image` 上留空。检索时通过关系取组级属性即可，避免重复维护。

### 其他节点

| 节点 | 关键属性 | 数据来源 |
|---|---|---|
| `Motif` 纹样元素 | `motif_id`, `bbox`, `image_id`, `label` | Label Studio 标注导出 |
| `Color` 颜色 | `name`, `hex` | 标注回填 |
| `Form` 构图形态 | `name` | 标注回填 |
| `Technique` 技法 | `name` | 标注回填 |
| `Region` 地域 | `name` | 标注回填 |
| `GeneratedWork` 作品 | `work_id`, `prompt`, `model_or_workflow`, `seed`, `created_at`, `output_path`, `review_status` | ComfyUI 生成后回填 |
| `Student` 学生 | `student_id`, `name`, `group_name` | 课程名单 |
| `CourseTask` 任务 | `task_id`, `title`, `unit` | 课程安排 |

---

## 五、导入顺序

必须按依赖顺序执行，否则关系找不到端点：

```
1. schema.cypher         建约束与索引
2. nodes_groups.csv      创建 MotifGroup
3. nodes_images.csv      创建 Image + BELONGS_TO
4. rels_derived.csv      创建 DERIVED_FROM
5. （标注完成后）         创建 Motif / Color / Technique 等
```

导入命令见 `import.cypher`。

---

## 六、当前数据可落地的部分

| 环节 | 状态 | 说明 |
|---|---|---|
| `MotifGroup` | ✅ 可导入 | 250 组，规律已校验 |
| `Image` | ✅ 可导入 | 1000 张，属性完整 |
| `BELONGS_TO` | ✅ 可导入 | 1000 条 |
| `DERIVED_FROM` | ✅ 可导入 | 750 条（每组 3 个变体） |
| `Color` / `Form` / `Technique` / `Region` | ⏳ 待标注 | 字段为空，需人工回填 |
| `Motif` | ⏳ 待标注 | 需 Label Studio 导出 |
| `GeneratedWork` / `Student` | ⏳ 待生产 | 需 ComfyUI 环节产出 |

**即：结构性骨架现在就能建完，标注类内容留待回填。** 这样做的价值是——先验证图模型和查询逻辑是否成立，避免标注几百张后才发现模型要改。
