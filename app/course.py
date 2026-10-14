"""课程结构数据层。

这个模块回答一个界面问题：**学生打开网站，怎么知道"我现在该做哪一步"？**

原来的系统是六个并排的标签页（检索 / 创作 / 作品 / 提取 / 标注 / 知识图谱），
对学生来说它们是**工具**，不是**课程**。学生看不出：
  - 这门课有几个单元、现在讲到哪
  - 这一单元对应哪个标签页
  - 我是非计算机专业，要做到什么程度算完成

所以把课程结构（单元、课时、分层任务、对应实验）集中在这里定义，
界面只负责渲染。**课程结构变了只改这一个文件**，不用翻界面代码。

数据来源对齐《技术路线与可行性方案》：
  §5 面向两类学生的分层路线（基础层 / 拓展层）
  §6 32 课时教学安排（4 个教学单元）
  §10 建议形成的结题成果
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 分层：方案 §5 把学生分成两类，任务深度不同
# ---------------------------------------------------------------------------

#: 基础层 —— 全校公选课学生的共同要求
TRACK_BASIC = "basic"
#: 拓展层 —— 计算机专业学生在共同任务上追加的要求
TRACK_ADVANCED = "advanced"

TRACKS = {
    TRACK_BASIC: {
        "label": "基础层（全体学生）",
        "desc": "通过网页完成全部任务，不需要安装任何软件、不需要写代码。",
        "hint": "所有操作系统、所有专业的学生都按这一层要求完成。",
    },
    TRACK_ADVANCED: {
        "label": "拓展层（计算机/数据专业）",
        "desc": "在基础层之上，额外学习数据结构、图模型、Cypher 与 Python 调用。",
        "hint": "选做，用于数据科学方向的技术加深。",
    },
}


# ---------------------------------------------------------------------------
# 教学单元：方案 §6 的 4 个单元 / 32 课时
# ---------------------------------------------------------------------------

#: 每个单元一个 dict。字段说明：
#:   no        单元序号（1-4），用于显示"第几单元"
#:   title     单元名
#:   theme     一句话讲这个单元在解决什么（学生视角）
#:   hours     课时数
#:   theory    理论部分课时
#:   practice  实践部分课时
#:   tabs      本单元要用到的标签页名称（必须与界面里 st.tabs 的名字一致）
#:   outputs   本单元的产出物（学生交什么）
#:   steps     基础层学生按顺序要做的操作步骤
#:   advanced  拓展层学生额外要做的事
#:   exp_file  对应实验指导书文件名（在 experiments/ 下）
UNITS = [
    {
        "no": 1,
        "title": "数据与纹样",
        "theme": "我们采到的到底是什么数据？",
        "hours": 8,
        "theory": 4,
        "practice": 4,
        "tabs": ["① 检索纹样", "④ 提取纹样"],
        "outputs": ["规范化图片和基础 CSV", "一份纹样数据整理记录"],
        "steps": [
            "到 ① 检索纹样 页，浏览数据集，观察原始图 / 纹理变体 / 色彩变体 / 拼贴图四类的区别",
            "到 ④ 提取纹样 页，选一张绣品照片，点「开始提取纹样」",
            "切换四种视图（原图对照 / 透明纹样 / 四方连续 / 3D 立体），记录每一版看到什么",
            "回答：这张照片里，哪些像素是纹样，哪些是底布、黑边、拍摄光照？",
        ],
        "advanced": [
            "阅读 build_metadata.py，理解 25 个字段是怎么算出来的",
            "用 Pillow/OpenCV 自己写一遍尺寸统计与质量筛选",
            "对比 250 张原始图的宽高比分布，说明为什么归为横图/竖图/方图三类",
        ],
        "exp_file": "实验一_纹样数据整理.md",
    },
    {
        "no": 2,
        "title": "关系与图谱",
        "theme": "数据之间的关系，应该存在哪里？",
        "hours": 10,
        "theory": 4,
        "practice": 6,
        "tabs": ["⑥ 知识图谱", "⑤ 标注纹样"],
        "outputs": ["图数据库案例", "一份 Cypher 查询记录"],
        "steps": [
            "到 ⑥ 知识图谱 页，读「三个基本概念」，理解节点 / 关系 / 属性",
            "看「同一份数据，两种存法」对照表，重点看 Cypher 与 SQL 并排的区别",
            "依次点 6 个预设查询，观察每一个返回多少行",
            "到 ⑤ 标注纹样 页，标注 3 组纹样（题材 / 地域 / 技法 / 主色调）",
            "回到 ⑥ 知识图谱 页，重新点 ⑤⑥ 两个查询 —— 数据变了，结果也变了",
        ],
        "advanced": [
            "读懂 graph/model.md 的节点与关系设计，说明为什么标注挂在 MotifGroup 而不是 Image 上",
            "自己写 3 条 Cypher：按地域统计、找出共色的图、查两跳变体",
            "用 Neo4j Python Driver 在脚本里执行查询并输出结果",
        ],
        "exp_file": "实验二_图数据库构建.md",
    },
    {
        "no": 3,
        "title": "检索与生成",
        "theme": "纹样从「被收藏」到「能被用」，缺什么？",
        "hours": 10,
        "theory": 4,
        "practice": 6,
        "tabs": ["① 检索纹样", "② AI 重组创作", "⑤ 标注纹样"],
        "outputs": ["纹样重组作品", "作品的设计说明"],
        "steps": [
            "到 ① 检索纹样 页，用筛选条件找到 1–2 张想用的纹样，点「加入我的选择」",
            "到 ② AI 重组创作 页，填学号 / 姓名，选择「单图重组」或「多纹样组合」",
            "调整创作强度（轻微调整 / 中等重组 / 大幅创作），写下你的创作描述",
            "点「开始生成」，等待出图；生成结果会自动归档",
            "到 ③ 我的作品 页，为作品填写**设计说明**（你改了什么、为什么这么改）",
        ],
        "advanced": [
            "读 workflows/*.json，理解占位符 __INPUT_IMAGE__ / __DENOISE__ 是怎么被替换的",
            "用 ComfyUI 自己搭一个工作流，导出 API Format 替换模板",
            "比较不同 denoise 强度对「保留原始纹样结构」的影响，写结论",
        ],
        "exp_file": "实验三_检索与AI重组.md",
    },
    {
        "no": 4,
        "title": "表达与评价",
        "theme": "一件数字作品，凭什么说是「你的」？",
        "hours": 4,
        "theory": 4,
        "practice": 0,
        "tabs": ["③ 我的作品", "⑥ 知识图谱"],
        "outputs": ["课程作品和评价记录", "过程归档材料"],
        "steps": [
            "到 ③ 我的作品 页，确认作品的设计说明已填写完整",
            "进入作品展示墙，浏览同班同学的作品",
            "为至少 2 件同学作品写互评（先说看到的优点，再给一条具体建议）",
            "回答：你的作品里，哪些是你决定的（选素材 / 强度 / 描述词），哪些是 AI 生成的？",
        ],
        "advanced": [
            "查看图数据库里 GeneratedWork 节点的溯源关系链，说明过程可追溯是怎么实现的",
            "设计一个字段，用来记录你更想保留的过程信息",
        ],
        "exp_file": "实验四_作品展示与评价.md",
    },
]


#: 32 课时 = 16 理论 + 16 实践（方案 §6）
TOTAL_HOURS = sum(u["hours"] for u in UNITS)
THEORY_HOURS = sum(u["theory"] for u in UNITS)
PRACTICE_HOURS = sum(u["practice"] for u in UNITS)


# ---------------------------------------------------------------------------
# 单元 ↔ 标签页 的反查
# ---------------------------------------------------------------------------

def unit_for_tab(tab_name: str):
    """给定标签页名，返回它属于哪个单元（可能多个，取第一个）。

    用途：把六个并排的工具页挂回课程结构上。学生点开一个工具页时，
    能知道"我现在是在做第几单元"。
    """
    for u in UNITS:
        if tab_name in u["tabs"]:
            return u
    return None


def tab_unit_map() -> dict:
    """标签页名 → 单元 dict。方便界面批量查。"""
    m = {}
    for u in UNITS:
        for t in u["tabs"]:
            m.setdefault(t, u)
    return m


# ---------------------------------------------------------------------------
# 进度计算
# ---------------------------------------------------------------------------

def unit_progress(unit: dict, ctx: dict) -> dict:
    """算一个单元的完成情况。

    参数 ctx 由调用方（界面）从实际数据算好传进来，避免这里去读文件：
        annotated_groups  已标注组数
        total_groups      总组数
        works             作品记录列表（dict 列表）
        student_id        当前学生学号（用来只统计他自己的作品）

    返回 dict：
        done    bool   是否算完成
        ratio   float  0~1
        label   str    一句话进度描述
    """
    no = unit["no"]

    # 单元 1：提取纹样。用"是否提取过纹样"判断 —— 目录里有产出即算完成
    if no == 1:
        n = ctx.get("extracted_count", 0)
        return {
            "done": n > 0,
            "ratio": 1.0 if n > 0 else 0.0,
            "label": ("已提取 %d 张纹样" % n) if n else "还没提取过纹样",
        }

    # 单元 2：标注 + 图查询。标注是这一单元的核心动作
    if no == 2:
        a = ctx.get("annotated_groups", 0)
        total = ctx.get("total_groups", 0) or 1
        need = 3  # 方案里要求至少标 3 组
        ratio = min(a / need, 1.0)
        return {
            "done": a >= need,
            "ratio": ratio,
            "label": "已标注 %d 组（本单元要求至少 %d 组）" % (a, need),
        }

    # 单元 3：生成作品 + 填设计说明
    if no == 3:
        mine = _my_works(ctx)
        with_note = [w for w in mine if (w.get("design_note") or "").strip()]
        if not mine:
            return {"done": False, "ratio": 0.0,
                    "label": "还没有作品，去 ② 生成第一件"}
        if not with_note:
            return {"done": False, "ratio": 0.5,
                    "label": "有 %d 件作品，但都还没填设计说明" % len(mine)}
        return {"done": True, "ratio": 1.0,
                "label": "已有 %d 件作品，其中 %d 件填了设计说明"
                         % (len(mine), len(with_note))}

    # 单元 4：互评。给同学作品写过评价即算完成
    if no == 4:
        given = ctx.get("peer_reviews_given", 0)
        need = 2
        return {
            "done": given >= need,
            "ratio": min(given / need, 1.0),
            "label": "已互评 %d 件作品（本单元要求至少 %d 件）" % (given, need),
        }

    return {"done": False, "ratio": 0.0, "label": ""}


def _my_works(ctx: dict) -> list:
    """从 ctx 里挑出当前学生的作品。学号为空时返回全部（游客视角）。"""
    works = ctx.get("works") or []
    sid = (ctx.get("student_id") or "").strip()
    if not sid:
        return list(works)
    return [w for w in works if sid in str(w.get("student_id") or "")]


def overall_progress(ctx: dict) -> dict:
    """全部单元的总体进度，用于首页顶部那一条状态。"""
    rows = [unit_progress(u, ctx) for u in UNITS]
    done = sum(1 for r in rows if r["done"])
    ratio = sum(r["ratio"] for r in rows) / len(rows) if rows else 0.0
    return {
        "units_done": done,
        "units_total": len(UNITS),
        "ratio": ratio,
        "rows": rows,
    }
