# -*- coding: utf-8 -*-
"""
苗绣纹样创作系统 · 完整闭环版

链路：检索纹样 -> 选择元素 -> AI 辅助重组 -> 作品归档

三种运行状态（自动检测，无需配置）：
  1. 图数据库 + ComfyUI  -> 完整闭环
  2. 图数据库 / CSV      -> 检索可用，生成不可用
  3. 仅 CSV             -> 检索可用，生成不可用

启动：
  streamlit run app/streamlit_app.py

可选环境变量：
  NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD     图数据库
  COMFYUI_URL                                  默认 http://127.0.0.1:8188
"""

import csv
import os
import sys
import time
from collections import OrderedDict

import pandas as pd
import streamlit as st

try:
    import numpy as np
except ImportError:          # pragma: no cover
    np = None

try:
    from PIL import Image
except ImportError:          # pragma: no cover
    Image = None

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from neo4j_client import GraphClient
from comfy_client import ComfyClient, ComfyError
from api_image_client import ApiImageClient, ApiImageError
from workflow import (
    load_template, build_img2img, build_multi_motif, minimal_img2img,
    DEFAULT_POSITIVE, DEFAULT_NEGATIVE, DENOISE_PRESETS,
    WorkflowTemplateError, find_placeholders,
    pick_img2img_template, pick_multi_motif_template,
    template_requirements,
)
from archive import WorkArchive

# 「等待 + 取回 + 归档」的业务逻辑抽在 generation.py 里，
# 这里是脚本，没法被单元测试 import，所以逻辑不放在本文件。
from generation import wait_and_archive, describe_wait

# 纹样提取：把绣品照片里的「绣线纹样」从底布上分离出来。
# 提取结果是透明 PNG + 四方连续预览 + 3D 浮雕高度图，
# 对应「纹样是符号/特征，不是附着在物体上的图案」这一命题。
import motif_extract

# 图数据库教学：节点/关系定义、预设查询、无数据库时的内存图等价实现
import graph_teach

# 标注：⓪ 课程导航 与 ⑤ 标注纹样 都要用，所以提到模块级导入
from annotation_store import (
    AnnotationStore, ANNOTATION_FIELDS, REQUIRED_FIELDS,
    VALUE_OPTIONS, FIELD_HINTS,
)

# 课程结构：4 个教学单元、32 课时、分层任务（对齐技术路线方案 §5 §6）。
# 界面只负责渲染，课程结构变了只改 course.py。
import course

# HTML 转义：凡是要把用户输入（设计说明、互评）拼进 unsafe_allow_html
# 的地方都必须先转义，否则学生写个 <script> 就能改页面结构。
from html import escape as _esc

PROJECT_DIR = os.path.dirname(APP_DIR)
CSV_PATH = os.path.join(PROJECT_DIR, "metadata", "images.csv")
WORK_CSV = os.path.join(PROJECT_DIR, "metadata", "works.csv")
GEN_DIR = os.path.join(PROJECT_DIR, "data", "generated")
MOTIF_DIR = os.path.join(PROJECT_DIR, "data", "motifs")
UPLOAD_DIR = os.path.join(PROJECT_DIR, "data", "uploads")
WF_DIR = os.path.join(PROJECT_DIR, "workflows")

# 图片目录：自动探测（环境变量 > 项目内 data/images > 常见位置），
# 不再硬编码绝对路径，换机器/换用户都能跑
from paths import find_image_root, image_root_or_hint

DEFAULT_IMAGE_ROOT = find_image_root() or ""

st.set_page_config(
    page_title="苗绣纹样创作系统",
    page_icon="🧵",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 视觉主题：配色取自本数据集实测主色调（靛蓝土布 / 朱砂绣线 / 银饰），
# 见 theme.py 顶部的设计说明。
from theme import global_css, note, caution, done, hint, tag

st.markdown(global_css(), unsafe_allow_html=True)

VARIANT_LABEL = {
    "basic": "原始图", "texture": "纹理变体",
    "color": "色彩变体", "mixup": "拼贴合成图",
}
LABEL_TO_VARIANT = {v: k for k, v in VARIANT_LABEL.items()}


# ==================================================================
# 资源初始化
# ==================================================================
@st.cache_resource(show_spinner=False)
def get_graph(uri, user, password, database):
    return GraphClient(uri=uri, user=user, password=password,
                       database=database or None, timeout=3.0)


@st.cache_resource(show_spinner=False)
def get_comfy(url):
    return ComfyClient(url, timeout=4.0)


@st.cache_resource(show_spinner=False)
def get_api_image():
    """免费云端生图后端（ModelScope / OpenAI 兼容）。

    没配 key 时 available 为 False，界面自动降级，不影响检索功能。
    """
    return ApiImageClient.from_env()


def wait_and_archive_ui(comfy, pend, upload_dir, archive_obj,
                        max_wait=240, interval=1.0, progress_slot=None):
    """在界面上跑 wait_and_archive，并在等待期间往 progress_slot 写进度。

    真正的等待/取回/归档逻辑在 generation.wait_and_archive（可单测）；
    这里只负责把等待过程可视化 —— 用户能看见「已等待 n 秒」而不是干等。
    """
    def _cb(elapsed, q):
        msg = "⏳ 生成中… 已等待 %.0f 秒" % elapsed
        if q.get("running") or q.get("pending"):
            msg += "（运行 %s / 排队 %s）" % (q.get("running"),
                                            q.get("pending"))
        progress_slot.info(msg)

    res = wait_and_archive(
        comfy, pend, upload_dir, archive_obj,
        max_wait=max_wait, interval=interval,
        progress_cb=_cb if progress_slot is not None else None,
    )
    if res.ok or res.get("status") in ("failed", "timeout", "no_output"):
        return {"ok": res.ok, "status": res.get("status"),
                "elapsed": res.get("elapsed"),
                "message": res.get("message"),
                "work_id": res.get("work_id"),
                "paths": res.get("paths") or []}
    return {"ok": False, "status": "failed", "message": "未知错误",
            "paths": [], "elapsed": 0}


@st.cache_data(show_spinner=False)
def _b64_thumb(path, box=420, quality=82):
    """把图片压成缩略图并 base64 内嵌。

    为什么要自己压、而不是用 st.image：
      st.image 会保持原始宽高比且高度不定，4 列网格里若混有竖图与方图，
      下方文字和按钮就会参差不齐。这里统一输出**固定高度方框 + object-fit:cover**，
      网格才真正对齐；顺带把大图压小，检索 24 张时页面明显更轻。

    参数以 _ 开头？不需要 —— 这里 path 是小字符串，缓存它正合适。
    """
    if not os.path.exists(path):
        return ""
    try:
        from PIL import Image as _I
        import base64
        import io as _io
        im = _I.open(path)
        im = im.convert("RGB")
        # 只保留长边 box 像素，足够 4 列网格显示
        im.thumbnail((box, box), _I.LANCZOS)
        buf = _io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return ""


def _course_ctx(archive_obj, ann_store_obj=None, csv_df_obj=None, student_id=""):
    """汇总课程进度要用到的实际数据，交给 course.py 去算。

    为什么在这里聚合、而不是让 course.py 自己读文件：
      course.py 保持**纯计算、无副作用**，才能被单元测试直接调用
      （传一个 dict 进去断言返回值）。读取路径依赖、缓存策略、
      数据库降级这类事属于界面层，不该渗进课程定义里。
    """
    ctx = {
        "annotated_groups": 0,
        "total_groups": 0,
        "extracted_count": 0,
        "works": [],
        "peer_reviews_given": 0,
        "student_id": student_id or "",
    }

    # 总组数：优先取本地清单（它始终加载）
    try:
        if csv_df_obj is not None and "group_id" in csv_df_obj.columns:
            ctx["total_groups"] = int(csv_df_obj["group_id"].nunique())
    except Exception:
        pass

    # 已标注组数（progress 需要总数参数，所以放在总数算完之后）
    try:
        if ann_store_obj is not None:
            prog = ann_store_obj.progress(ctx.get("total_groups") or 0)
            ctx["annotated_groups"] = prog.get("annotated", 0)
    except Exception:
        pass

    # 提取过的纹样数：数 motifs 目录里的正式产出（下划线开头的是调试图）
    try:
        if os.path.isdir(MOTIF_DIR):
            ctx["extracted_count"] = len(
                [f for f in os.listdir(MOTIF_DIR)
                 if f.endswith(".png") and not f.startswith("_")])
    except Exception:
        pass

    # 作品与互评
    try:
        ctx["works"] = archive_obj.load() if archive_obj is not None else []
    except Exception:
        ctx["works"] = []
    try:
        if archive_obj is not None:
            stats = archive_obj.peer_review_stats(student_id or "")
            ctx["peer_reviews_given"] = stats.get("given", 0)
    except Exception:
        pass

    return ctx


@st.cache_data(show_spinner=False)
def _build_3d_payload(_arr_bytes, alpha_bytes, shape, size, max_cells,
                      thickness, theme, cloth_rgb=None):
    """在缓存里构建 3D 网格（顶点/面/颜色），避免每次交互都重算。

    为什么要缓存：
        构建 1 万多个顶点 + 2 万个三角面要 0.1~0.3 秒，看着不长，
        但 Streamlit 每次控件变化都会重跑整个脚本 —— 用户每转一下
        角度、改一次网格密度，都要重新算一遍，页面上会出现明显的
        "卡一下"。网格本身只跟「图像 + 参数」有关，与相机角度无关，
        所以很适合缓存。

    为什么参数用下划线开头（_arr_bytes / _alpha_bytes）：
        st.cache_data 默认按参数值做哈希，大数组做哈希非常慢。
        下划线开头的参数**不参与哈希**，只按 size/max_cells 等
        轻量参数缓存 —— 这正是我们要的：同一张图 + 同一组参数
        直接命中缓存。

    返回 dict（可直接喂给 Plotly），失败返回 None。
    """
    try:
        arr = np.frombuffer(_arr_bytes, dtype=np.uint8).reshape(shape)
        alpha = np.frombuffer(alpha_bytes, dtype=np.uint8).reshape(
            shape[0], shape[1]).astype(bool)
    except Exception:
        return None

    hf = motif_extract.relief_field(arr, alpha, size=size,
                                    cloth_rgb=cloth_rgb)
    if hf is None:
        return None
    mesh = motif_extract.relief_mesh(arr, alpha, height=hf, size=size,
                                     max_cells=max_cells,
                                     thickness=thickness,
                                     cloth_rgb=cloth_rgb)
    return mesh


def _render_3d_stage(arr, alpha, cloth_rgb=None):
    """渲染「可自由环绕观察的 3D 立体展示台」。

    这是 ④ 提取纹样 的主视图。设计意图（对应教学定位）：

        「刺绣是立体的」这件事，用一张平面图是讲不明白的。
        把它放进一个有台面、有光照的三维场景里，学生一转就能看到
        线迹的起伏、纹样片的厚度 —— 纹样才真正是一个**实物符号**，
        而不是一张贴在网页上的图片。

    参数 cloth_rgb: extract 判定的底布颜色。传给高度场，
        用来区分「绣花」与「底布」—— 绣花凸起、底布压平，
        3D 出来才是图案本身的浮雕，而不是一块鼓鼓的布。

    降级策略（很重要，不能因为没装 plotly 就让整页崩掉）：
        1. 有 plotly  → 完整可交互 3D 场景
        2. 无 plotly  → 退回到灰度高度图（静态，但仍能表达起伏）
    """
    st.caption("拖动旋转 · 滚轮缩放 · 右键平移 —— "
               "把纹样转一转，看它的厚度和线迹起伏。")

    try:
        import plotly.graph_objects as go  # noqa: F401
        has_plotly = True
    except ImportError:
        has_plotly = False

    if not has_plotly:
        hm = motif_extract.heightmap(arr, alpha)
        if hm is not None:
            st.image((hm * 255).astype(np.uint8), clamp=True,
                     width="stretch",
                     caption="灰度高度图（未安装 plotly，只能给静态图）。"
                             "亮处 = 凸起的线迹。")
        st.info("想看到**真正可转动的 3D**，请在本地执行：\n\n"
                "```\npip install plotly\n```\n\n然后重启服务。")
        return

    c1, c2, c3 = st.columns(3)
    with c1:
        # 默认从 200 提到 260：轮廓锯齿是这张图最显眼的瑕疵，
        # 而它跟 size 直接相关（size 越大，轮廓阶梯越细）。
        # 260 时网格约 2.6 万面，拖动仍然流畅，
        # 换来的是轮廓明显更干净 —— 这笔账划算。
        size = st.select_slider(
            "网格精细度", options=[140, 200, 260, 320], value=260,
            help="越大越细腻，但预览越重。要转动流畅就调小。")
    with c2:
        max_cells = st.select_slider(
            "网格密度", options=[70, 90, 110, 140], value=110,
            help="决定三角面数量。调小更流畅，调大细节更多。")
    with c3:
        # 厚度上限跟着 RELIEF_Z_SCALE（0.22）走：
        # 正面高度跨度约 0.20，厚度若超过它的一半，
        # 侧面就会比正面还高，看着像一块砖而不是一片绣。
        thickness = st.slider(
            "浮雕厚度", 0.03, 0.14, 0.06, 0.01,
            help="纹样片的物理厚度。太薄像纸，太厚像砖。"
                 "建议保持在浮雕起伏的 1/3 以内，绣片的厚度本来就很小。")

    theme = "dark" if st.get_option("theme.base") == "dark" else "light"

    with st.spinner("正在构建 3D 网格…"):
        mesh = _build_3d_payload(
            arr.tobytes(), alpha.tobytes(), arr.shape,
            int(size), int(max_cells), float(thickness), theme,
            cloth_rgb)

    if mesh is None:
        st.warning("3D 网格构建失败（纹样可能过小或为空）。")
        return

    fig = motif_extract.build_3d_figure(
        arr, alpha, size=int(size), max_cells=int(max_cells),
        thickness=float(thickness), style=theme, mesh=mesh,
        cloth_rgb=cloth_rgb)

    if fig is None:
        st.warning("3D 场景组装失败。")
        return

    st.plotly_chart(fig, width="stretch",
                    config={"displayModeBar": True,
                            "displaylogo": False,
                            "scrollZoom": True})

    n_v = len(mesh["x"])
    n_t = len(mesh["i"])
    st.caption("当前网格：**%s 个顶点 / %s 个三角面**，"
               "厚度 %.2f。这就是「把一张照片还原成一件有体积的绣片」"
               "的代价——数据一旦三维化，信息量会涨好几个量级，"
               "所以采集时更要关注精度与标准化。"
               % (format(n_v, ","), format(n_t, ","), thickness))


@st.cache_data(show_spinner=False)
def load_csv(path):
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
    for c in ("width", "height", "file_size_kb"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


# ------------------------------------------------------------------
# 把 Streamlit secrets 搬进环境变量
# ------------------------------------------------------------------
# 为什么必须有这一步：本系统全部按 `os.environ` 读配置（COMFYUI_URL、
# IMAGE_API_KEY、NEO4J_URI……），但部署到 Streamlit Community Cloud 后，
# 密钥写在云端的 Secrets 面板里，只会出现在 `st.secrets`，
# **不会自动进 os.environ**。不桥接的话，线上那个实例永远读到空值，
# ② 页就一直显示「AI 生成服务未连接」—— 本地明明配好了也没用。
#
# 仓库里只有 secrets.toml.example，真正的 secrets.toml 被忽略、不进 git；
# 本地没有这个文件时 st.secrets 会抛异常，必须兜住，否则整个应用起不来。
# 只搬标量：toml 的嵌套表（如 [some.section]）搬过去只会变成一段 dict 的
# 字符串，毫无意义还可能覆盖同名真配置。
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, (str, int, float, bool)):
            os.environ.setdefault(_k, str(_v))
except (FileNotFoundError, Exception):  # 本地没有 secrets.toml → 正常，跳过
    pass


graph = get_graph(
    os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
    os.environ.get("NEO4J_USER", "neo4j"),
    os.environ.get("NEO4J_PASSWORD", ""),
    os.environ.get("NEO4J_DATABASE", ""),
)
comfy = get_comfy(os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188"))
apiimg = get_api_image()
archive = WorkArchive(WORK_CSV, GEN_DIR)
# 标注存储提到模块级：⓪ 课程导航页要读标注进度，⑤ 标注页要读写，
# 两处共用一个实例，避免各建一份导致状态不一致。
ann_store = AnnotationStore()

DB_OK = graph.available
COMFY_OK = comfy.available
API_OK = apiimg.available

# ------------------------------------------------------------------
# 生成后端选择：ComfyUI(本地) 优先，其次免费云端 API
# 两者都没有时，界面降级为"仅检索"，不报错。
# ------------------------------------------------------------------
if COMFY_OK:
    BACKEND = "comfy"
    GEN_OK = True
elif API_OK:
    BACKEND = "api"
    GEN_OK = True
else:
    BACKEND = None
    GEN_OK = False

if BACKEND == "comfy":
    GEN_BADGE = "生成可用 · " + (comfy.device_summary() or "GPU")
elif BACKEND == "api":
    GEN_BADGE = "生成可用 · 云端 API"
else:
    GEN_BADGE = "生成未连接"

# 本地图库清单：始终加载。
# 注意：它**不是**「数据库不可用时的替补」，而是独立的本地元数据源——
# ④ 提取纹样、⑤ 标注纹样 等页面直接按文件路径工作，和图数据库无关。
# 只有 ① 检索纹样 会在 DB_OK 时优先走 Neo4j。
csv_df = load_csv(CSV_PATH) if os.path.exists(CSV_PATH) else None

# 检索页是否使用图数据库（Neo4j 已连接且可用）
db_search = bool(DB_OK)

# 图片根目录：放在所有标签页共用，避免只在某个标签页内定义导致 NameError
image_root = DEFAULT_IMAGE_ROOT
show_tech = False


# ==================================================================
# 顶部
# ==================================================================
tc, bc = st.columns([3, 2])
with tc:
    st.title("苗绣纹样创作系统")
    st.markdown(
        '<div class="src">《数据采集与清洗》课程 · 纹样数字化教学模块</div>',
        unsafe_allow_html=True)
with bc:
    _db_tag = tag("图数据库已连接", "on") if DB_OK else tag("本地数据")
    _gen_tag = tag(GEN_BADGE, "on" if GEN_OK else "off")
    st.markdown(
        '<div style="text-align:right;padding-top:1.8rem;">%s%s</div>'
        % (_db_tag, _gen_tag),
        unsafe_allow_html=True,
    )

if csv_df is None:
    st.error(
        "**没有可用数据。**\n\n"
        "本地元数据文件不存在：`metadata/images.csv`\n\n"
        "请先运行下面的命令生成元数据：\n\n"
        "```bash\n"
        "python build_metadata.py --src \"你的图片目录\"\n"
        "```"
    )
    if not DEFAULT_IMAGE_ROOT:
        st.warning(image_root_or_hint().replace("\n", "  \n"))
    st.stop()

if not DEFAULT_IMAGE_ROOT:
    st.warning(
        "**未找到纹样图片目录**，检索页将无法显示图片。\n\n"
        + image_root_or_hint().replace("\n", "  \n")
    )

# ==================================================================
# 八个标签页：⓪ 课程导航 + 六个工具页 + ⑦ 作品展示墙
# ==================================================================
# 为什么要在最前面加一个「课程导航」：
#   原来六个标签页并排，学生看到的是**六个工具**，不是**一门课**。
#   他不知道这门课有几个单元、现在讲到哪、自己该做哪一步。
#   ⓪ 页把工具挂回课程结构上（在 app/course.py 里定义），
#   学生打开先看到「第几单元、要做哪几步、做完没」。
#
# 为什么 ③ 和 ⑦ 分开：
#   ③ 是**自己的作品台**（填设计说明、看教师审核）；
#   ⑦ 是**全班展示墙**（浏览同学作品、写互评）。
#   合成一页会让「我的」和「大家的」混在一起，
#   而第四单元的核心动作恰恰是「跳出自己的作品去看别人的」。
tab_course, tab_search, tab_create, tab_works, tab_gallery, \
    tab_extract, tab_annotate, tab_graph = st.tabs(
    ["⓪ 课程导航", "① 检索纹样", "② AI 重组创作", "③ 我的作品",
     "⑦ 作品展示墙", "④ 提取纹样", "⑤ 标注纹样", "⑥ 知识图谱"])


# ------------------------------------------------------------------
# ⓪ 课程导航
# ------------------------------------------------------------------
with tab_course:
    # 当前学生的学号：⓪ 页也提供输入口，因为这是学生进系统
    # 最先看到的一页 —— 不该逼他先去 ② 页填学号。
    st.markdown(
        '<div class="note">这门课共 <b>%d 课时</b>（理论 %d + 实践 %d），'
        '分 <b>%d 个教学单元</b>。下面按顺序走完即可完成全部任务；'
        '每一步都能点进对应的操作页面。</div>'
        % (course.TOTAL_HOURS, course.THEORY_HOURS, course.PRACTICE_HOURS,
           len(course.UNITS)),
        unsafe_allow_html=True)

    st.write("")
    _who_l, _who_r = st.columns([2, 3])
    with _who_l:
        course_sid = st.text_input(
            "你的学号 / 姓名（用于记录进度与作品归属）",
            value=st.session_state.get("student_id", ""),
            key="course_sid",
            placeholder="例如 S2024001")
        if course_sid != st.session_state.get("student_id", ""):
            st.session_state["student_id"] = course_sid
    with _who_r:
        # 分层选择：方案 §5 要求区分基础层与拓展层
        course_track = st.radio(
            "选择你的学习层次",
            [course.TRACKS[course.TRACK_BASIC]["label"],
             course.TRACKS[course.TRACK_ADVANCED]["label"]],
            horizontal=True, key="course_track")
        _is_adv = course_track == course.TRACKS[course.TRACK_ADVANCED]["label"]
        st.caption(course.TRACKS[
            course.TRACK_ADVANCED if _is_adv else course.TRACK_BASIC]["desc"])

    # ---- 汇总进度 ----
    _ctx = _course_ctx(archive, ann_store, csv_df, st.session_state.get("student_id", ""))
    _prog = course.overall_progress(_ctx)

    st.write("")
    _pct_slot = st.container()
    with _pct_slot:
        st.markdown(
            '<div class="src">课程进度：<b>%d</b> / <b>%d</b> 单元已完成'
            '　｜　已标注 <b>%s</b> 组　｜　作品 <b>%d</b> 件</div>'
            % (_prog["units_done"], _prog["units_total"],
               format(_ctx["annotated_groups"], ","),
               len(_ctx["works"])),
            unsafe_allow_html=True)
        st.progress(min(_prog["ratio"], 1.0))

    st.divider()

    # ---- 四个单元 ----
    for _u, _r in zip(course.UNITS, _prog["rows"]):
        _mark = "done" if _r["done"] else "note"
        _badge = tag("已完成", "on") if _r["done"] else tag("进行中", "off")
        st.markdown(
            '<div class="sec-no">第 %d 单元　·　%d 课时（理论 %d / 实践 %d）</div>'
            % (_u["no"], _u["hours"], _u["theory"], _u["practice"]),
            unsafe_allow_html=True)
        st.markdown("### %s　%s" % (_u["title"], _badge),
                    unsafe_allow_html=True)
        st.markdown("**%s**" % _u["theme"], unsafe_allow_html=True)
        st.markdown(
            '<div class="%s">%s</div>' % (_mark, _r["label"]),
            unsafe_allow_html=True)

        _c1, _c2 = st.columns([3, 2])
        with _c1:
            st.markdown("**要做的事（基础层）**")
            for _i, _s in enumerate(_u["steps"], 1):
                st.markdown("%d. %s" % (_i, _s))
        with _c2:
            st.markdown("**本单元产出**")
            for _o in _u["outputs"]:
                st.markdown("- %s" % _o)
            st.markdown("**用到哪些页面**")
            for _t in _u["tabs"]:
                st.markdown("- %s" % _t)

        if _is_adv:
            with st.expander("拓展层：本单元额外要求（计算机/数据专业）"):
                for _i, _a in enumerate(_u["advanced"], 1):
                    st.markdown("%d. %s" % (_i, _a))

        st.caption("配套实验指导书：experiments/%s" % _u["exp_file"])
        st.divider()


# ------------------------------------------------------------------
# ① 检索
# ------------------------------------------------------------------
with tab_search:
    # 提示与计数合成一行：左边说明操作，右边给出状态。
    # 分成两行会占掉首屏黄金位置，而这里的主角应该是纹样。
    _hint_l, _hint_r = st.columns([3, 2])
    with _hint_l:
        st.markdown(
            '<div class="note">按类型筛选纹样，点击「加入我的选择」收集素材，'
            '然后到 <b>② AI 重组创作</b> 标签页生成作品。</div>',
            unsafe_allow_html=True,
        )
    with _hint_r:
        _count_slot = st.empty()
    st.write("")

    with st.sidebar:
        st.header("检索条件")
        selected_labels = st.multiselect(
            "纹样类型", options=list(VARIANT_LABEL.values()),
            default=["原始图", "纹理变体"],
        )
        selected_variants = [LABEL_TO_VARIANT[x] for x in selected_labels]
        orientation = st.radio("画面方向", ["全部", "竖图", "横图", "方图"],
                               horizontal=True)
        only_markable = st.toggle("只显示适合标注的纹样", value=True)
        per_page = st.select_slider("每页显示", [12, 24, 48, 96], value=24)

        # ---- 基于标注数据的属性筛选 ----
        # 这些维度程序推断不出来，靠 ⑤ 标注页人工补充。
        # 还没标注时这里会提示去标注，而不是显示一堆选不动的空下拉框。
        st.divider()
        st.markdown("**纹样属性**")

        attr_cols = OrderedDict([
            ("pattern_type", "题材"),
            ("region", "地域"),
            ("technique", "技法"),
            ("dominant_color", "主色调"),
        ])

        def _attr_options(col):
            if csv_df is None or col not in csv_df.columns:
                return []
            vals = csv_df[col].dropna().astype(str).str.strip()
            vals = vals[vals != ""]
            return sorted(vals.unique().tolist())

        attr_pick = {}
        any_attr_available = False
        for col, label in attr_cols.items():
            opts = _attr_options(col)
            if opts:
                any_attr_available = True
            attr_pick[col] = st.multiselect(
                label, options=opts, default=[],
                placeholder="未标注" if not opts else "全部",
                help="数据来自 ⑤ 标注纹样 标签页；未标注时为不可选状态")

        if not any_attr_available:
            st.caption("还没有标注数据。到 **⑤ 标注纹样** 补上地域、技法等属性后，"
                       "这里就能按这些维度筛选了。")

        st.divider()
        with st.expander("高级设置（教师用）"):
            image_root = st.text_input("图片所在文件夹", value=DEFAULT_IMAGE_ROOT)
            show_tech = st.checkbox("显示技术信息", value=False)
            st.caption("数据源：%s" % ("图数据库" if DB_OK else "本地 CSV"))

    # 侧边栏的修改需回写到外层，供其他标签页使用
    st.session_state["image_root"] = image_root

    if not selected_variants:
        st.warning("请至少选择一种纹样类型。")
        st.stop()

    if "page" not in st.session_state:
        st.session_state["page"] = 0

    if DB_OK:
        total = sum(
            graph.count_groups(
                variant=v,
                orientation=None if orientation == "全部" else orientation,
                markable="是" if only_markable else None,
            ) for v in selected_variants
        )
        total_pages = max(1, (total + per_page - 1) // per_page)
        st.session_state["page"] = min(st.session_state["page"], total_pages - 1)
        start = st.session_state["page"] * per_page
        frames = []
        per = max(1, per_page // len(selected_variants))
        for v in selected_variants:
            d = graph.search_images(
                variant=v,
                orientation=None if orientation == "全部" else orientation,
                markable="是" if only_markable else None,
                skip=start, limit=per,
            )
            if d is not None and len(d):
                frames.append(d)
        page_df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

        # 图查询本身不支持下述属性过滤，这里对结果做二次筛选。
        # （这些属性挂在 MotifGroup 上，后续可在 Cypher 里加条件。）
        for col, picked_vals in attr_pick.items():
            if picked_vals and len(page_df) and col in page_df.columns:
                page_df = page_df[page_df[col].isin(picked_vals)]
    else:
        view = csv_df[csv_df["variant"].isin(selected_variants)]
        if orientation != "全部":
            view = view[view["orientation"] == orientation]
        if only_markable:
            view = view[view["markable"] == "是"]

        # 按标注属性筛选（地域/技法/题材/主色调）
        for col, picked_vals in attr_pick.items():
            if picked_vals and col in view.columns:
                view = view[view[col].isin(picked_vals)]

        view = view.sort_values(["group_id", "is_source"],
                                ascending=[True, False]).drop_duplicates(
            subset="group_id", keep="first").sort_values("image_id")
        total = len(view)
        total_pages = max(1, (total + per_page - 1) // per_page)
        st.session_state["page"] = min(st.session_state["page"], total_pages - 1)
        start = st.session_state["page"] * per_page
        page_df = view.iloc[start:start + per_page]

    picked = st.session_state.setdefault("picked", [])

    # 计数填进上面预留的右侧槽位（不摆成大数字卡——那是仪表盘的做法，
    # 会把视线从纹样本身拉走）。
    _src = "图数据库" if DB_OK else "本地数据"
    _count_slot.markdown(
        '<div class="src" style="text-align:right;padding-top:.7rem;">'
        '找到 <b>%s</b> 组 ｜ 已选 <b>%s</b> 张 ｜ <b>%s</b></div>'
        % (format(total, ","), len(picked), _src),
        unsafe_allow_html=True)

    if page_df is None or len(page_df) == 0:
        st.info("没有符合条件的纹样。试着减少筛选条件，或多选几种类型。")
    else:
        rows = [page_df.iloc[i:i + 4] for i in range(0, len(page_df), 4)]
        for row in rows:
            cols = st.columns(4, gap="medium")
            for col, (_, item) in zip(cols, row.iterrows()):
                with col:
                    p = os.path.join(image_root, item["file_name"])
                    # 图片高度不固定会让下方文字和按钮参差不齐
                    # （竖图/方图混排时尤其明显）。用一个固定高度的
                    # 容器把图片裁进方框，网格才对齐。
                    if os.path.exists(p):
                        st.markdown(
                            '<div class="thumb">'
                            '<img src="data:image/jpeg;base64,%s" />'
                            '</div>' % _b64_thumb(p), unsafe_allow_html=True)
                    else:
                        st.markdown(
                            '<div class="thumb thumb-miss">图片未找到</div>',
                            unsafe_allow_html=True)
                    st.markdown('<div class="motif-title">%s</div>'
                                % item["variant_label"], unsafe_allow_html=True)
                    st.markdown('<div class="motif-meta">%s · %s</div>'
                                % (item["orientation"], item["group_id"]),
                                unsafe_allow_html=True)

                    # 有标注就显示出来，让学生检索时能看到属性
                    tags = []
                    for _c in ("pattern_type", "region",
                               "technique", "dominant_color"):
                        _v = str(item.get(_c) or "").strip()
                        if _v:
                            tags.append(_v)
                    if tags:
                        st.markdown(
                            '<div class="motif-meta">%s</div>'
                            % " · ".join(tags[:3]), unsafe_allow_html=True)
                    else:
                        # 占位，保证同行卡片的按钮仍然对齐
                        st.markdown('<div class="motif-meta">&nbsp;</div>',
                                    unsafe_allow_html=True)

                    if show_tech:
                        st.caption(
                            f"{item['image_id']} · {int(item['width'])}×"
                            f"{int(item['height'])} · {item['quality_status']}")
                    sel = item["image_id"] in picked
                    if st.button("已选" if sel else "加入我的选择",
                                 key="pick_" + item["image_id"],
                                 width="stretch",
                                 type="primary" if sel else "secondary"):
                        picked.remove(item["image_id"]) if sel else picked.append(item["image_id"])
                        st.rerun()

        st.divider()
        q1, q2, q3 = st.columns([1, 2, 1])
        with q1:
            if st.button("← 上一页", disabled=st.session_state["page"] == 0,
                         width="stretch"):
                st.session_state["page"] -= 1
                st.rerun()
        with q2:
            st.markdown(
                '<div style="text-align:center;color:#6b7280;padding-top:.5rem;">'
                '第 %d / %d 页 · 共 %d 组</div>'
                % (st.session_state["page"] + 1, total_pages, total),
                unsafe_allow_html=True)
        with q3:
            if st.button("下一页 →", disabled=st.session_state["page"] >= total_pages - 1,
                         width="stretch"):
                st.session_state["page"] += 1
                st.rerun()


# ------------------------------------------------------------------
# ② 创作
# ------------------------------------------------------------------
with tab_create:
    picked = st.session_state.setdefault("picked", [])

    if not GEN_OK:
        st.markdown(
            '<div class="caution"><b>AI 生成功能未连接。</b><br>'
            '检索与归档功能正常可用。开启生成有两种方式，任选其一：</div>',
            unsafe_allow_html=True)
        st.markdown(
            "**方式一：免费云端 API（推荐，不用装软件）**\n\n"
            "1. 到 [魔搭社区](https://modelscope.cn) 注册并完成实名认证\n"
            "2. 在「访问令牌」页面创建 API Key\n"
            "3. 设置环境变量后重启本系统：\n\n"
            "```bash\n"
            "set IMAGE_API_KEY=你的密钥\n"
            "```\n\n"
            "默认使用 `Qwen/Qwen-Image-Edit`（图生图）。"
            "每日有免费额度，额度用完次日自动重置。\n\n"
            "**方式二：本地 ComfyUI（需 GPU，一次装好永久免费）**\n\n"
            "见 `docs/comfyui-guide.md`，启动后设置 `COMFYUI_URL`。")
        st.caption("ComfyUI 连接原因：%s" % str(comfy.error)[:150])
        st.caption("API 后端连接原因：%s" % str(apiimg.error)[:150])
        if getattr(apiimg, "hint", None):
            st.caption("API 后端处理建议：%s" % str(apiimg.hint)[:300])

    if not picked:
        st.markdown(
            '<div class="note">还没有选择素材。请先到 <b>① 检索纹样</b> '
            '标签页挑选 1–2 张纹样。</div>', unsafe_allow_html=True)
    else:
        st.subheader("已选素材（%d 张）" % len(picked))
        cols = st.columns(min(6, len(picked)), gap="small")
        for col, iid in zip(cols, picked):
            with col:
                fn = iid + ".jpg"
                ip = os.path.join(image_root, fn)
                if os.path.exists(ip):
                    # 与①检索页同一只定高盒子：这排最多 6 张，
                    # 竖图横图混在一起时若各按自身比例撑开，
                    # 下方的编号就会一条高一条低，读起来像没对齐的表。
                    st.markdown(
                        '<div class="thumb"><img src="data:image/jpeg;'
                        'base64,%s" /></div>' % _b64_thumb(ip),
                        unsafe_allow_html=True)
                st.caption(iid)

        st.divider()

        mode_label = st.radio(
            "创作模式",
            ["单图重组（把一张纹样重新演绎）", "多纹样组合（把两张纹样合成新作品）"],
            horizontal=True,
            disabled=not GEN_OK,
        )
        mode = "img2img" if mode_label.startswith("单图") else "multi_motif"

        if mode == "multi_motif" and len(picked) < 2:
            st.warning("多纹样组合需要至少 2 张素材，请再选一张。")

        cc1, cc2 = st.columns(2)
        with cc1:
            student_id = st.text_input("学号 / 姓名", placeholder="例如 S2024001")
            strength_label = st.select_slider(
                "创作强度", options=list(DENOISE_PRESETS.keys()),
                value="中等重组（推荐）", disabled=not GEN_OK)
            if mode == "multi_motif":
                blend = st.slider("纹样融合比例", 0.0, 1.0, 0.5, 0.05,
                                  disabled=not GEN_OK)
            else:
                blend = None
        with cc2:
            prompt = st.text_area("创作描述（可修改）", value=DEFAULT_POSITIVE,
                                  height=110, disabled=not GEN_OK)
            seed_input = st.number_input("随机种子（0 = 随机）", 0, 2 ** 31 - 1, 0,
                                         disabled=not GEN_OK)

        with st.expander("高级参数（教师用）"):
            neg = st.text_area("负向提示词", value=DEFAULT_NEGATIVE, height=80)

            # 模板自动选择：按服务器实际可用的模型挑，避免用错模板
            # （模板与模型不匹配时 ComfyUI 只回 400/500，看不出原因）
            #
            # 单图和多纹样各有一套模板，都必须按模式分别挑 ——
            # 以前多纹样模式硬编码检查点版模板，服务器没有 checkpoint
            # 时必然提交失败，而报错只说「某个 ckpt 不在列表里」，
            # 完全指不到「模板选错了」这个真实原因。
            _inv = comfy.model_inventory() if COMFY_OK else {}
            if mode == "multi_motif":
                auto_tpl, auto_reason, auto_ok = pick_multi_motif_template(
                    PROJECT_DIR, _inv)
            else:
                auto_tpl, auto_reason, auto_ok = pick_img2img_template(
                    PROJECT_DIR, _inv)

            wf_override = st.text_input(
                "工作流模板文件（留空则使用默认）", value=auto_tpl)

            if COMFY_OK:
                if auto_ok:
                    st.caption("自动选择：%s —— %s" % (auto_tpl, auto_reason))
                else:
                    st.caption("%s" % auto_reason)
                    st.caption("   请先给 ComfyUI 放入生成模型，"
                               "或改用免费云端 API。")
            st.caption("用自己的工作流：在 ComfyUI 里做好流程，"
                       "用 Save (API Format) 导出 JSON，覆盖到 workflows/ 目录。"
                       "模板里可以写 `_readme` 之类的注释，提交时会自动剔除。")

        st.write("")
        can_run = GEN_OK and bool(student_id.strip()) and (
            mode == "img2img" or len(picked) >= 2) and bool(prompt.strip())
        if not GEN_OK:
            st.info("按钮已禁用：AI 生成服务未连接。")
        elif not student_id.strip():
            st.info("请填写学号或姓名，作品需要记录作者。")

        if st.button("开始生成", type="primary", disabled=not can_run):
            denoise = DENOISE_PRESETS[strength_label]
            seed = int(seed_input) if seed_input else None
            src_paths = [os.path.join(image_root, i + ".jpg") for i in picked[:2]]
            src_paths = [p for p in src_paths if os.path.exists(p)]

            # ============ 后端 A：免费云端 API（同步返回）============
            if BACKEND == "api":
                try:
                    with st.spinner("正在请求云端生成，通常 10–60 秒..."):
                        ref = src_paths[0] if src_paths else None
                        # 多纹样时把第二张的意象写进提示词——
                        # 多数云端图生图只接受单张参考图，这是务实做法
                        use_prompt = prompt
                        if mode == "multi_motif" and len(picked) >= 2:
                            use_prompt = (prompt + "，融合 %s 与 %s 两种苗绣纹样的"
                                          "构图特征与配色" % (picked[0], picked[1]))
                        paths = apiimg.generate(
                            use_prompt, image_path=ref, out_dir=UPLOAD_DIR,
                            negative=neg, prefix="api")

                    saved = archive.save_output(
                        archive.new_work_id(student_id.strip()), paths)
                    wid = archive.record(
                        student_id=student_id.strip(),
                        source_images=picked[:2],
                        mode=mode, prompt=use_prompt, negative_prompt=neg,
                        seed="", denoise="",
                        blend=blend,
                        model_or_workflow="API:%s" % apiimg.model,
                        workflow_file="cloud_api",
                        output_path=",".join(saved),
                    )
                    st.success("生成完成并已归档，作品编号 %s" % wid)
                    for p in saved:
                        st.image(p, width="stretch")
                    st.session_state["last_work"] = wid
                    st.caption("到「③ 我的作品」查看全部作品。")
                except ApiImageError as e:
                    st.error("生成失败：%s" % e)
                    if getattr(apiimg, "hint", None):
                        st.caption("处理建议：%s" % apiimg.hint)
                except Exception as e:
                    st.error("出错：%s" % e)

            # ============ 后端 B：本地 ComfyUI（异步，需轮询）============
            else:
                try:
                    # 模板加载：**不再**静默回退到 minimal_img2img()。
                    #
                    # 原来的写法 `load_template(p) if exists(p) else minimal_img2img()`
                    # 会在模板路径写错时悄悄换成一个硬编码的 SDXL 检查点模板，
                    # 然后 ComfyUI 回 400「sd_xl_base_1.0.safetensors 不在列表里」——
                    # 报错指向模型，真实原因却是「你的模板没找到」。
                    # 现在路径不对就明确报错并停下。
                    tpl_path = os.path.join(PROJECT_DIR, wf_override.strip())
                    if not os.path.exists(tpl_path):
                        st.error(
                            "**找不到工作流模板文件，已阻止提交。**\n\n"
                            "路径：`%s`\n\n"
                            "请在「高级参数」里检查模板文件名，"
                            "或确认 `workflows/` 目录下有该文件。"
                            % wf_override.strip())
                        st.stop()

                    try:
                        tpl = load_template(tpl_path)
                    except WorkflowTemplateError as e:
                        st.error("**工作流模板格式有误，已阻止提交。**\n\n%s" % e)
                        st.stop()

                    # 提交前先校验模板占位符，避免产出"假组合"作品
                    need = ["INPUT_IMAGE", "POSITIVE", "NEGATIVE", "SEED", "DENOISE"]
                    if mode == "multi_motif":
                        need += ["INPUT_IMAGE_2", "BLEND"]
                    have = find_placeholders(tpl)
                    lack = [x for x in need if x not in have]
                    if lack:
                        st.error(
                            "**工作流模板与创作模式不匹配，已阻止提交。**\n\n"
                            "缺少占位符：%s\n\n"
                            "当前模板含有的占位符：%s\n\n"
                            "请检查「高级参数」里的模板文件，"
                            "或在 ComfyUI 中修正工作流后重新导出覆盖。"
                            % ("、".join("`__%s__`" % x for x in lack),
                               "、".join("`__%s__`" % x for x in sorted(have)) or "无")
                        )
                        st.stop()

                    # 提交前再核对一次模型是否真的在服务器上 ——
                    # 这才是最容易被误判成「模型坏了」的失败点。
                    if COMFY_OK:
                        _req = template_requirements(tpl)
                        _missing = comfy.missing_models(_req)
                        if _missing:
                            st.error(
                                "**工作流需要的模型在服务器上不存在，已阻止提交。**\n\n"
                                + "\n".join("- %s" % m for m in _missing)
                                + "\n\n模板：`%s`\n\n"
                                "请改用与服务器模型匹配的模板"
                                "（在「高级参数」里改），或让服务器管理员放入该模型。"
                                % wf_override.strip())
                            st.stop()

                    with st.spinner("正在上传素材..."):
                        up = []
                        for src in src_paths[:2]:
                            up.append(comfy.upload_image(src, subfolder="miao"))

                    if not up:
                        st.error("没有找到可用的素材文件，请回到检索页重新选择。")
                        st.stop()

                    if mode == "multi_motif" and len(up) >= 2:
                        wf, used_seed = build_multi_motif(
                            tpl, up[0][0], up[1][0], prompt=prompt, negative=neg,
                            seed=seed, denoise=denoise, blend=blend or 0.5,
                            subfolder=up[0][1])
                    else:
                        wf, used_seed = build_img2img(
                            tpl, up[0][0], prompt=prompt, negative=neg,
                            seed=seed, denoise=denoise, subfolder=up[0][1])

                    pid = comfy.submit(wf, client_id=student_id.strip())
                    pend = {
                        "prompt_id": pid, "student_id": student_id.strip(),
                        "mode": mode, "prompt": prompt, "negative": neg,
                        "seed": used_seed, "denoise": denoise, "blend": blend,
                        "sources": picked[:2], "workflow_file": wf_override,
                        "submitted_at": time.strftime("%H:%M:%S"),
                    }
                    st.session_state["pending"] = pend

                    # ---- 自动等待结果（不用手动点）----
                    slot = st.empty()
                    res = wait_and_archive_ui(comfy, pend, UPLOAD_DIR,
                                              archive, progress_slot=slot)
                    slot.empty()

                    if res["ok"]:
                        st.success("%s（用时 %.0f 秒）"
                                   % (res["message"], res["elapsed"]))
                        for p in res["paths"]:
                            st.image(p, width="stretch")
                        st.session_state["last_work"] = res["work_id"]
                        st.session_state.pop("pending", None)
                        st.caption("到「③ 我的作品」查看全部作品。")
                    elif res["status"] == "timeout":
                        # 超时不清理 pending，留着让用户手动取回
                        st.warning(res["message"])
                        st.info("任务可能还在排队。可点下方"
                                "「查询进度 / 取回结果」继续等待。")
                    else:
                        st.error(res["message"])
                        st.session_state.pop("pending", None)
                except ComfyError as e:
                    st.error("提交失败：%s" % e)
                except Exception as e:
                    st.error("出错：%s" % e)

        # ---- 手动取回区（仅超时后需要；正常情况下上面已自动完成）----
        pend = st.session_state.get("pending")
        if pend and BACKEND == "comfy":
            st.divider()
            st.subheader("任务还在排队")
            st.caption("编号 %s · 提交于 %s"
                       % (pend["prompt_id"][:8], pend.get("submitted_at", "—")))
            if st.button("继续等待 / 取回结果", type="primary"):
                slot = st.empty()
                res = wait_and_archive_ui(comfy, pend, UPLOAD_DIR, archive,
                                          max_wait=600, progress_slot=slot)
                slot.empty()
                if res["ok"]:
                    st.success("%s（用时 %.0f 秒）"
                               % (res["message"], res["elapsed"]))
                    for p in res["paths"]:
                        st.image(p, width="stretch")
                    st.session_state["last_work"] = res["work_id"]
                    st.session_state.pop("pending", None)
                    st.rerun()
                elif res["status"] == "failed":
                    st.error(res["message"])
                    st.session_state.pop("pending", None)
                else:
                    st.warning(res["message"])


# ------------------------------------------------------------------
# ③ 作品
# ------------------------------------------------------------------
with tab_works:
    _mine_sid = st.session_state.get("student_id", "").strip()
    st.markdown(
        '<div class="done">这是<b>你自己</b>的作品台。每件作品都记录了原始素材、'
        '提示词、随机种子和生成时间，可作为课程过程性材料。</div>',
        unsafe_allow_html=True)
    st.markdown(
        '<div class="note">第四单元要求为作品补上<b>设计说明</b>——'
        '写清你改了什么、为什么这么改。生成参数是机器记的，'
        '设计思路只有你能写。填完会自动出现在展示墙上。</div>',
        unsafe_allow_html=True)
    st.write("")

    works = archive.load()
    if not works:
        st.markdown(
            '<div class="note">还没有作品记录。到 <b>② AI 重组创作</b> '
            '生成第一件作品，或到 <b>⓪ 课程导航</b> 看第三单元怎么开始。</div>',
            unsafe_allow_html=True)
    else:
        _f1, _f2 = st.columns([2, 2])
        with _f1:
            author = st.text_input("按学号筛选（留空看全部）",
                                   value=_mine_sid, key="works_filter")
        with _f2:
            _only_unnoted = st.checkbox("只看还没写设计说明的", value=False,
                                        key="works_only_unnoted")
        rows_w = [w for w in works if not author.strip()
                  or author.strip() in (w.get("student_id") or "")]
        if _only_unnoted:
            rows_w = [w for w in rows_w
                      if not (w.get("design_note") or "").strip()]

        # 作品缩略图与①检索页共用 .thumb 那只定高盒子。
        # 之前这里直接 st.image(width="stretch")：生成图多为竖构图，
        # 撑满栏宽后会拉成一根很高的柱子，右侧空出一大片灰底，
        # 一条记录就占掉大半屏，翻两下就不知道自己在看第几件了。
        # 定高 + contain 之后，每件作品占的纵向空间一致，能横向扫读。
        _pr_stats = archive.peer_review_stats(author.strip())
        st.markdown(
            '<div class="src">共 <b>%d</b> 件作品%s　｜　'
            '你已互评 <b>%d</b> 件</div>'
            % (len(rows_w),
               "（已按条件筛选）" if (author.strip() or _only_unnoted) else "",
               _pr_stats["given"]),
            unsafe_allow_html=True)

        for w in reversed(rows_w):
            with st.container():
                c1, c2 = st.columns([1, 3])
                with c1:
                    paths = [p for p in (w.get("output_path") or "").split(",")
                             if p and os.path.exists(p)]
                    if paths:
                        # 用 data URI 内嵌缩略图：一是走 .thumb 定高盒子，
                        # 二是避免 st.image 每次 rerun 重新读盘整张原图。
                        st.markdown(
                            '<div class="thumb"><img src="data:image/jpeg;'
                            'base64,%s" /></div>' % _b64_thumb(paths[0]),
                            unsafe_allow_html=True)
                    else:
                        st.markdown('<div class="thumb">'
                                    '<span class="thumb-miss">'
                                    '作品文件未找到</span></div>',
                                    unsafe_allow_html=True)
                with c2:
                    _wid = w.get("work_id") or ""
                    st.markdown(f"**{_wid}** · {w.get('student_id') or '未署名'}")
                    st.caption(
                        "模式：%s ｜ 强度：%s ｜ 种子：%s ｜ 时间：%s"
                        % (w.get("mode"), w.get("denoise"),
                           w.get("seed"), w.get("created_at")))
                    st.caption("来源素材：%s" % (w.get("source_images") or "—"))
                    st.caption("提示词：%s" % (w.get("prompt") or "")[:160])

                    # ---- 设计说明 ----
                    _dn = (w.get("design_note") or "").strip()
                    if _dn:
                        st.markdown(
                            '<div class="done"><b>设计说明</b><br>%s</div>'
                            % _esc(_dn), unsafe_allow_html=True)
                    else:
                        st.markdown(
                            '<div class="caution">还没写设计说明。'
                            '展开下面补填（第四单元要求）。</div>',
                            unsafe_allow_html=True)

                    with st.expander("填写 / 修改设计说明"):
                        _note_val = st.text_area(
                            "写清：你改了什么、为什么这么改、保留了什么苗绣特征",
                            value=_dn, height=110,
                            key="dn_%s" % _wid,
                            placeholder="例如：把底布换成深靛蓝以贴近土布本色，"
                                        "保留花枝的对称骨架，只加强了花瓣的层次。")
                        if st.button("保存设计说明", key="dn_save_%s" % _wid):
                            if not author.strip():
                                st.warning("请先在上面填入你的学号，"
                                           "设计说明要记名才有效。")
                            elif archive.update_design_note(_wid, _note_val.strip()):
                                st.success("已保存")
                                st.rerun()
                            else:
                                st.error("保存失败：没找到这件作品")

                    # ---- 收到的互评（第四单元）----
                    _prs = archive.load_peer_reviews(_wid)
                    if _prs:
                        with st.expander("同学互评（%d 条）" % len(_prs)):
                            for _p in _prs:
                                st.markdown(
                                    "**%s**　%s" % (_p.get("reviewer_name")
                                                    or _p.get("reviewer_id") or "匿名",
                                                    _p.get("created_at") or ""))
                                st.markdown("优点：%s" % (_p.get("merit") or "—"))
                                st.markdown("建议：%s" % (_p.get("suggestion") or "—"))
                                st.divider()

                    status = w.get("review_status") or "待审核"
                    _status_cls = ("done" if status == "通过"
                                   else "caution" if status == "需修改" else "src")
                    st.markdown(
                        '<div class="%s">教师审核：%s%s</div>'
                        % (_status_cls, _esc(str(status)),
                           (" · " + _esc(str(w.get("review_note"))))
                           if w.get("review_note") else ""),
                        unsafe_allow_html=True)
                st.divider()

        with st.expander("导出全部作品记录"):
            st.dataframe(pd.DataFrame(works), width="stretch",
                         hide_index=True)
            st.download_button(
                "下载 works.csv",
                data=open(WORK_CSV, "rb").read() if os.path.exists(WORK_CSV) else b"",
                file_name="works.csv", mime="text/csv")

    # ---- 教师审核 ----
    with st.sidebar:
        st.divider()
        with st.expander("教师审核"):
            if not works:
                st.caption("暂无作品")
            else:
                opts = ["%s · %s" % (w.get("work_id"), w.get("student_id"))
                        for w in works]
                sel = st.selectbox("选择作品", opts, key="review_sel")
                wid = sel.split(" · ")[0]
                new_status = st.radio("审核结果", ["通过", "需修改", "待审核"],
                                      horizontal=True, key="review_status")
                note = st.text_input("评语", key="review_note")
                if st.button("保存审核", key="review_save"):
                    if archive.update_review(wid, new_status, note):
                        # 同步到图数据库（若已连接）
                        if DB_OK:
                            graph.run(
                                "MATCH (w:GeneratedWork {work_id: $id}) "
                                "SET w.review_status = $st, w.review_note = $note",
                                id=wid, st=new_status, note=note)
                        st.success("已保存")
                        st.rerun()

        if DB_OK:
            with st.expander("标注与作品统计"):
                prog = graph.annotation_progress()
                if prog:
                    st.metric("纹样分组", prog.get("total_groups", 0))
                    st.caption("已标注地域：%d" % (prog.get("annotated_region") or 0))
                nw = graph.run("MATCH (w:GeneratedWork) RETURN count(w) AS n")
                if nw:
                    st.metric("图库中的作品", nw[0].get("n", 0))

# ------------------------------------------------------------------
# ⑦ 作品展示墙
# ------------------------------------------------------------------
# 与 ③ 我的作品 的区别：
#   ③ 是个人视角（我的作品、我填说明、教师对我的审核）
#   ⑦ 是班级视角（全班作品、我给别人写互评）
# 方案第四单元要求「完成作品展示、互评和过程归档」，
# 互评必须发生在**别人的**作品上，所以单独一页。
with tab_gallery:
    st.markdown(
        '<div class="done">全班作品展示墙。浏览同学的作品，'
        '为至少 <b>2 件</b> 写下互评 —— 先说看到的优点，再给一条具体建议。'
        '</div>', unsafe_allow_html=True)
    st.write("")

    _all_works = archive.load()
    _gallery = [w for w in _all_works if (w.get("output_path") or "").strip()]
    # 只展示有图的（文件确实存在的），否则展示墙全是破图
    _gallery = [w for w in _gallery
                if any(os.path.exists(p)
                       for p in (w.get("output_path") or "").split(",") if p)]

    if not _gallery:
        st.markdown(
            '<div class="note">展示墙还没有作品。等第一位同学在 '
            '<b>② AI 重组创作</b> 生成作品后，这里会自动出现。</div>',
            unsafe_allow_html=True)
    else:
        _pr_all = archive.peer_review_stats()
        _by_work = _pr_all.get("by_work", {})
        _reviewer = st.session_state.get("student_id", "").strip()

        # ---- 筛选 ----
        _g1, _g2, _g3 = st.columns([2, 2, 3])
        with _g1:
            _g_mode = st.selectbox(
                "创作模式",
                ["全部", "单图重组", "多纹样组合"],
                key="gal_mode")
        with _g2:
            _g_only_unreviewed = st.checkbox("只看我还没评的", value=False,
                                             key="gal_unrev")
        with _g3:
            _g_sort = st.radio("排序", ["最新在前", "互评最多"],
                               horizontal=True, key="gal_sort")

        _rows = list(_gallery)
        if _g_mode == "单图重组":
            _rows = [w for w in _rows if (w.get("mode") or "") == "img2img"]
        elif _g_mode == "多纹样组合":
            _rows = [w for w in _rows if (w.get("mode") or "") == "multi_motif"]

        _my_reviewed = set()
        if _reviewer:
            _my_reviewed = {r.get("work_id")
                            for r in archive.load_peer_reviews()
                            if r.get("reviewer_id") == _reviewer}
        if _g_only_unreviewed and _reviewer:
            _rows = [w for w in _rows if w.get("work_id") not in _my_reviewed]

        if _g_sort == "互评最多":
            _rows.sort(key=lambda w: _by_work.get(w.get("work_id"), 0),
                       reverse=True)
        else:
            _rows.reverse()  # load() 是按写入顺序，倒过来即最新在前

        st.markdown(
            '<div class="src">展示 <b>%d</b> 件作品　｜　'
            '全班互评共 <b>%d</b> 条%s</div>'
            % (len(_rows), _pr_all.get("total", 0),
               ("　｜　你已评 <b>%d</b> 件" % len(_my_reviewed))
               if _reviewer else "　｜　（填学号后可记录你的互评）"),
            unsafe_allow_html=True)
        st.write("")

        # ---- 网格：每行 3 件 ----
        _COLS = 3
        for _i in range(0, len(_rows), _COLS):
            _batch = _rows[_i:_i + _COLS]
            _cols = st.columns(_COLS, gap="medium")
            for _col, _w in zip(_cols, _batch):
                with _col:
                    _wid = _w.get("work_id") or ""
                    _p0 = [p for p in (_w.get("output_path") or "").split(",")
                           if p and os.path.exists(p)]
                    st.markdown(
                        '<div class="gal"><img src="data:image/jpeg;'
                        'base64,%s" /></div>' % _b64_thumb(_p0[0], box=640),
                        unsafe_allow_html=True)
                    st.markdown(
                        '<div class="motif-title">%s</div>'
                        '<div class="motif-meta">%s　·　%s</div>'
                        % (_esc(_wid),
                           _esc(str(_w.get("student_id") or "未署名")),
                           "单图重组" if _w.get("mode") == "img2img"
                           else "多纹样组合"),
                        unsafe_allow_html=True)

                    _dn = (_w.get("design_note") or "").strip()
                    st.markdown(
                        '<div class="gal-note">%s</div>'
                        % (_esc("设计说明：" + _dn) if _dn
                           else "作者还没写设计说明"),
                        unsafe_allow_html=True)

                    _n = _by_work.get(_wid, 0)
                    st.markdown(
                        '<div class="gal-meta">互评 %d 条%s</div>'
                        % (_n, "　· 你已评" if _wid in _my_reviewed else ""),
                        unsafe_allow_html=True)

                    with st.expander("看详情 / 写互评"):
                        st.markdown(
                            "模式：%s ｜ 强度：%s ｜ 种子：%s"
                            % (_w.get("mode"), _w.get("denoise"),
                               _w.get("seed")))
                        st.markdown("提示词：%s" % (_w.get("prompt") or "—"))
                        if _dn:
                            st.markdown("**设计说明**：%s" % _dn)
                        st.divider()

                        # 已有的互评
                        _prs = archive.load_peer_reviews(_wid)
                        if _prs:
                            st.markdown("**已有互评（%d 条）**" % len(_prs))
                            for _p in _prs:
                                st.markdown(
                                    "· %s：优点「%s」　建议「%s」"
                                    % (_p.get("reviewer_name")
                                       or _p.get("reviewer_id") or "匿名",
                                       _p.get("merit") or "—",
                                       _p.get("suggestion") or "—"))
                            st.divider()

                        # 写互评
                        if not _reviewer:
                            st.markdown(
                                '<div class="caution">请先到 <b>⓪ 课程导航</b> '
                                '填上学号，才能记录你的互评。</div>',
                                unsafe_allow_html=True)
                        elif _wid in _my_reviewed:
                            st.markdown(
                                '<div class="done">你已评过这件作品。'
                                '重新提交会覆盖原评价。</div>',
                                unsafe_allow_html=True)

                        _mv = st.text_area(
                            "优点：你看到哪里做得好？",
                            height=70, key="pm_%s" % _wid,
                            placeholder="例如：靛蓝底和桃红花枝的对比很克制，"
                                        "没有堆太多颜色。")
                        _sg = st.text_area(
                            "建议：给一条具体、可操作的建议",
                            height=70, key="ps_%s" % _wid,
                            placeholder="例如：花心可以再收一点，"
                                        "现在和外圈花瓣一样重，注意力散了。")
                        # 自己不能评自己 —— 否则互评环节失去意义
                        if _w.get("student_id") == _reviewer:
                            st.caption("这是你自己的作品，跳过互评。")
                        elif st.button("提交互评", key="pbtn_%s" % _wid):
                            if not _mv.strip() or not _sg.strip():
                                st.warning("优点和建议都要写。"
                                           "只说「挺好的」不算互评。")
                            else:
                                archive.add_peer_review(
                                    _wid, reviewer_id=_reviewer,
                                    reviewer_name=st.session_state.get(
                                        "student_name", "") or _reviewer,
                                    merit=_mv.strip(), suggestion=_sg.strip())
                                st.success("已提交")
                                st.rerun()


# ------------------------------------------------------------------
# ④ 提取纹样
# ------------------------------------------------------------------
with tab_extract:
    st.markdown(
        '<div class="note"><b>纹样是符号，不是照片。</b><br>'
        '一张绣品照片 = 纹样 + 底布 + 拍摄角度 + 光照。这里把<b>绣线纹样本身</b>'
        '从底布上分离出来，得到一张可复用的透明纹样，再演示它作为'
        '<b>图案单元</b>（四方连续）和<b>浮雕特征</b>（3D 高度图）的样子。</div>',
        unsafe_allow_html=True,
    )
    st.write("")

    ex_left, ex_right = st.columns([1, 2], gap="large")

    # ---- 左：选图 ----
    with ex_left:
        st.subheader("1. 选一张绣品照片")

        ex_source = st.radio(
            "图片来源", ["从图库选（推荐）", "上传自己的照片"],
            horizontal=False, label_visibility="collapsed")

        ex_path = None

        if ex_source.startswith("从图库"):
            if csv_df is None:
                st.warning("未读取到图库清单（metadata/images.csv）。"
                           "可改用「上传自己的照片」。")
            else:
                # 只用原始图 variant=basic：变体图是程序合成的，
                # 底布/黑边等特征和真实拍摄不同，提取演示用原始图最干净。
                basics = csv_df[csv_df["variant"] == "basic"]
                gids = basics["group_id"].tolist()
                if not gids:
                    st.warning("图库里没有原始图。")
                else:
                    gid = st.selectbox("纹样分组", gids,
                                       index=0, key="ex_gid")
                    row = basics[basics["group_id"] == gid].iloc[0]
                    cand = os.path.join(image_root, row["file_name"])
                    if os.path.exists(cand):
                        ex_path = cand
                        st.image(cand, width="stretch")
                        st.caption("%s · %s×%s · %s"
                                   % (row["image_id"], row["width"],
                                      row["height"], row["orientation"]))
                    else:
                        st.error("图片文件不存在：%s" % cand)
                        st.caption("可在 ① 检索纹样 → 高级设置里修改图片文件夹。")
        else:
            up = st.file_uploader("上传绣品照片（JPG / PNG）",
                                  type=["jpg", "jpeg", "png"], key="ex_up")
            if up is not None:
                os.makedirs(UPLOAD_DIR, exist_ok=True)
                ex_path = os.path.join(UPLOAD_DIR, "extract_" + up.name)
                with open(ex_path, "wb") as f:
                    f.write(up.getbuffer())
                st.image(ex_path, width="stretch")

        st.divider()
        st.markdown("**2. 提取参数**")
        ex_keep = st.slider(
            "保留最大连通块数", 1, 12, 6,
            help="纹样常由多块（花瓣、花心、边饰）组成。数值越大保留越完整，"
                 "但也可能把底布上的噪点带进来。一般 4–8 合适。")
        ex_components_hint = st.toggle(
            "显示全部连通块（调试）", value=False,
            help="用于判断「纹样被切碎了」还是「底布没去干净」。"
                 "调试完可关掉。")
        ex_keep = 40 if ex_components_hint else ex_keep

    # ---- 右：结果 ----
    with ex_right:
        st.subheader("3. 提取结果")

        if not ex_path:
            st.info("左侧选一张图，这里会显示提取结果。")
        else:
            # 按钮不铺满整栏：铺满会让它看起来像一条警示横幅，
            # 而这里只是个操作入口。宽度交给内容决定。
            _bc, _ = st.columns([2, 3])
            with _bc:
                run = st.button("开始提取纹样", type="primary",
                                width="stretch")
            st.caption("提取是纯本地计算（只用 numpy + PIL），"
                       "不联网、不占用服务器 GPU。")

            cache_key = "ex_res::%s" % ex_path
            if run:
                try:
                    with st.spinner("正在分离绣线与底布…"):
                        res = motif_extract.extract(
                            ex_path, out_dir=os.path.join(PROJECT_DIR,
                                                          "data", "motifs"),
                            keep_components=int(ex_keep))
                    st.session_state[cache_key] = res
                except Exception as e:
                    st.error("提取失败：%s" % e)
                    st.session_state.pop(cache_key, None)

            res = st.session_state.get(cache_key)

            if res is None:
                pass  # 上面的 caption 已说明如何开始，不再重复
            elif not res.get("ok"):
                st.warning(res.get("message", "提取失败"))
                if res.get("cloth_rgb"):
                    st.caption("程序判断的底布颜色：RGB %s（占比 %.0f%%）"
                               % (res["cloth_rgb"],
                                  (res.get("cloth_cover") or 0) * 100))
                st.info("若底布与绣线颜色接近、或画面过暗，可以换一张图，"
                        "或在 ④ 里调小/调大「保留最大连通块数」再试。")
            else:
                st.success(res["message"])
                c1, c2, c3 = st.columns(3)
                c1.metric("纹样占比", "%.0f%%" % (res["coverage"] * 100))
                c2.metric("纹样尺寸", "%d×%d" % res["size"])
                c3.metric("底布判定色", "RGB %s" % str(res["cloth_rgb"]))

                view = st.radio(
                    "查看方式",
                    ["④ 3D 立体展示（可转动）", "② 透明纹样（棋盘底）",
                     "③ 四方连续（2D 图案单元）", "① 原图对照"],
                    horizontal=True,
                    label_visibility="collapsed",
                )

                arr, alpha = res["array"], res["alpha"]

                if view.startswith("④"):
                    _render_3d_stage(arr, alpha, res.get("cloth_rgb"))

                elif view.startswith("②"):
                    # 棋盘底：白线在白底上看不见，必须换背景才判断得准
                    bg = motif_extract.checkerboard(arr.shape[1], arr.shape[0])
                    rgba = np.dstack([arr, (alpha.astype(np.uint8) * 255)])
                    fg = Image.fromarray(rgba, "RGBA")
                    canvas = Image.alpha_composite(
                        Image.fromarray(bg, "RGBA"), fg)
                    st.image(np.asarray(canvas),
                             caption="透明 PNG（棋盘格 = 透明区域）。"
                                     "这就是可复用的纹样符号，不再依附于布料。",
                             width="stretch")
                    if res.get("saved") and os.path.exists(res["saved"]):
                        with open(res["saved"], "rb") as f:
                            st.download_button(
                                "下载透明纹样 PNG", f.read(),
                                file_name=os.path.basename(res["saved"]),
                                mime="image/png", width="stretch")

                elif view.startswith("③"):
                    tile = motif_extract.tile_preview(arr, alpha)
                    if tile is not None:
                        st.image(np.asarray(tile),
                                 caption="镜像四方连续。纹样一旦提取成图案单元，"
                                         "就能无限拼接成布料、壁纸、UI 底纹——"
                                         "这正是「符号」的价值。",
                                 width="stretch")

                else:
                    st.image(ex_path, caption="原始照片（含底布、黑边、拍摄光照）",
                             width="stretch")
                    if res.get("bbox"):
                        x0, y0, x1, y1 = res["bbox"]
                        st.caption("纹样在照片中的位置：(x %d–%d, y %d–%d)，"
                                   "即约 %.0f%% 的画面是底布和背景，"
                                   "真正承载信息的是那块纹样。"
                                   % (x0, x1, y0, y1,
                                      (1 - res["coverage"]) * 100))

                st.divider()
                st.caption(
                    "**教学要点** —— 原始照片 ≠ 纹样数据。照片里混着底布、黑边、"
                    "光照和拍摄角度；只有把纹样分离成带透明通道的图案，"
                    "它才成为可检索、可复用、可计算的特征，"
                    "才能进 ⑤ 标注属性、进图数据库、进 AI 重组。")


# ------------------------------------------------------------------
# ⑤ 标注
# ------------------------------------------------------------------
with tab_annotate:

    st.markdown(
        '<div class="note">为纹样补充「地域、技法、主色调」等属性。'
        '这些信息程序推断不出来，但补上之后，<b>① 检索纹样</b> 就能按这些维度筛选了。'
        '<br>一组只需标一次，全部 4 张变体会自动继承。</div>',
        unsafe_allow_html=True,
    )
    st.write("")

    # 注意：ann_store 现在提到模块级共享区创建（见 archive 那一段）。
    # 原因是 ⓪ 课程导航页也要读标注进度，而标签页代码是按顺序执行的，
    # 在这里才创建的话 ⓪ 页拿不到。

    # ---- 拿到「每组一张代表图」的清单 ----
    if DB_OK:
        ann_df = graph.search_images(limit=2000)
        if ann_df is None or not len(ann_df):
            ann_df = None
    else:
        ann_df = None

    if ann_df is None:
        # 从 CSV 里按组分去重，优先取可标注的源图
        base = csv_df.copy()
        base = base.sort_values(["group_id", "is_source"],
                                ascending=[True, False])
        ann_df = base.drop_duplicates(subset="group_id", keep="first")
        ann_df = ann_df.sort_values("group_id").reset_index(drop=True)

    total_groups = len(ann_df)

    # ---- 机器参考值（帮助判断，不作为结论）----
    ref_map = {}
    ref_csv = os.path.join(PROJECT_DIR, "annotation", "annotation_sheet.csv")
    if os.path.exists(ref_csv):
        try:
            with open(ref_csv, encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    gid = (r.get("分组编号") or "").strip()
                    if gid and gid not in ref_map:
                        ref_map[gid] = {
                            "color": r.get("【参考】机器统计首位色（常为背景，勿直接采信）", ""),
                            "palette": r.get("【参考】机器统计配色线索", ""),
                            "sym": r.get("【参考】机器预判对称", ""),
                        }
        except Exception:
            ref_map = {}

    prog = ann_store.progress(total_groups)

    # ---- 进度总览 ----
    # 进度条保留（它是真实的状态指示），但四个大数字压成一行文本：
    # 四张 metric 卡横排是仪表盘的做法，在这个以「看图点选」
    # 为主任务的页面上，会把注意力从纹样上拉走。
    pct = prog["annotated"] / max(total_groups, 1)
    st.markdown(
        '<div class="src">已标注 <b>%d</b> / %d 组 ｜ 必填齐全 '
        '<b>%d</b> 组 ｜ 完成度 <b>%.0f%%</b> ｜ 参与标注 <b>%d</b> 位</div>'
        % (prog["annotated"], total_groups, prog["complete"],
           pct * 100, len(prog["by_annotator"])),
        unsafe_allow_html=True)
    st.progress(min(pct, 1.0))

    if prog["by_annotator"]:
        st.caption("各人标注量：" + " · ".join(
            "%s %d 组" % (k, v) for k, v in
            sorted(prog["by_annotator"].items(), key=lambda x: -x[1])))

    st.divider()

    # ---- 操作区 ----
    o1, o2, o3 = st.columns([2, 1, 1])
    with o1:
        annotator = st.text_input(
            "标注人（学号 / 姓名）", value=st.session_state.get("annotator", ""),
            placeholder="例如 S2024001",
            help="用于统计每个人的标注量，教师可按此分配任务")
        st.session_state["annotator"] = annotator
    with o2:
        only_todo = st.toggle("只显示未标注", value=True)
    with o3:
        st.write("")
        if st.button("跳到下一组待标", width="stretch"):
            for i in range(total_groups):
                gid = ann_df.iloc[i]["group_id"]
                if gid not in ann_store.load():
                    st.session_state["ann_idx"] = i
                    st.rerun()
            st.info("全部已标注完成。")

    # ---- 选定当前组 ----
    if "ann_idx" not in st.session_state:
        st.session_state["ann_idx"] = 0

    if only_todo:
        done = set(ann_store.load())
        todo_idx = [i for i in range(total_groups)
                    if ann_df.iloc[i]["group_id"] not in done]
        if not todo_idx:
            st.success("**全部 %d 组都已标注完成。**" % total_groups)
            st.caption("关闭上方「只显示未标注」可浏览和修改已有标注。")
            st.stop()
        cur = st.session_state["ann_idx"]
        if cur not in todo_idx:
            cur = todo_idx[0]
            st.session_state["ann_idx"] = cur
    else:
        cur = min(st.session_state["ann_idx"], total_groups - 1)

    item = ann_df.iloc[cur]
    gid = item["group_id"]
    existing = ann_store.get(gid)

    # ---- 主体：左图右表单 ----
    left, right = st.columns([1, 1.5], gap="large")

    with left:
        st.markdown("#### %s" % gid)
        img_path = os.path.join(image_root, item["file_name"])
        if os.path.exists(img_path):
            st.image(img_path, width="stretch")
        else:
            st.warning("图片未找到：%s" % item["file_name"])

        st.caption("第 %d / %d 组 · %s · %s×%s"
                   % (cur + 1, total_groups, item["orientation"],
                      int(item["width"]), int(item["height"])))

        # 机器参考值（提示性质，明确标注不可直接采信）
        ref = ref_map.get(gid)
        if ref:
            st.markdown(
                hint(
                    '<b>机器线索（仅供参考，请以人眼判断为准）</b><br>'
                    '统计首位色：%s<br>配色线索：%s<br>预判对称：%s<br>'
                    '<span class="hint-warn">注意 —— 这类图多带黑色边框，'
                    '机器首位色常是背景而非纹样主色。</span>'
                    % (_esc(str(ref["color"])), _esc(str(ref["palette"])),
                       _esc(str(ref["sym"])))),
                unsafe_allow_html=True)

        if gid in ann_store.load():
            who = existing.get("annotator") or "未署名"
            when = existing.get("updated_at") or ""
            st.info("本组已由 **%s** 标注（%s）。可修改后再次保存。"
                    % (who, when))

    with right:
        # 表单版本号：保存后自增，用于重置所有控件
        ver = st.session_state.setdefault("ann_ver", 0)

        with st.form(key="ann_form_%s_%d" % (gid, ver)):
            st.markdown("##### 必填项（决定检索可用性）")
            values = {}

            for field in REQUIRED_FIELDS:
                label = ANNOTATION_FIELDS[field] + " *"
                opts = VALUE_OPTIONS.get(field, [])
                cur_val = existing.get(field, "")

                if opts:
                    choices = ["（未填）"] + opts + ["其他（手动填写）"]
                    idx = 0
                    if cur_val in opts:
                        idx = choices.index(cur_val)
                    elif cur_val:
                        idx = choices.index("其他（手动填写）")
                    sel = st.selectbox(label, choices, index=idx,
                                       key="f_%s_%s_%d" % (field, gid, ver),
                                       help=FIELD_HINTS.get(field, ""))
                    if sel == "其他（手动填写）":
                        values[field] = st.text_input(
                            "→ %s（手动填写）" % ANNOTATION_FIELDS[field],
                            value=cur_val if cur_val not in opts else "",
                            key="fx_%s_%s_%d" % (field, gid, ver))
                    elif sel == "（未填）":
                        values[field] = ""
                    else:
                        values[field] = sel
                else:
                    values[field] = st.text_input(label, value=cur_val,
                                                  key="f_%s_%s_%d" % (field, gid, ver))

            st.markdown("##### 加分项（可选，能填尽量填）")
            for field in ANNOTATION_FIELDS:
                if field in REQUIRED_FIELDS:
                    continue
                label = ANNOTATION_FIELDS[field]
                opts = VALUE_OPTIONS.get(field, [])
                cur_val = existing.get(field, "")

                if opts:
                    choices = ["（未填）"] + opts + ["其他（手动填写）"]
                    idx = 0
                    if cur_val in opts:
                        idx = choices.index(cur_val)
                    elif cur_val:
                        idx = choices.index("其他（手动填写）")
                    sel = st.selectbox(label, choices, index=idx,
                                       key="f_%s_%s_%d" % (field, gid, ver),
                                       help=FIELD_HINTS.get(field, ""))
                    if sel == "其他（手动填写）":
                        values[field] = st.text_input(
                            "→ %s（手动填写）" % label,
                            value=cur_val if cur_val not in opts else "",
                            key="fx_%s_%s_%d" % (field, gid, ver))
                    elif sel == "（未填）":
                        values[field] = ""
                    else:
                        values[field] = sel
                else:
                    values[field] = st.text_input(
                        label, value=cur_val,
                        key="f_%s_%s_%d" % (field, gid, ver),
                        help=FIELD_HINTS.get(field, ""))

            st.write("")
            b1, b2 = st.columns([1, 1])
            with b1:
                save_next = st.form_submit_button(
                    "保存并看下一组 →", type="primary", width="stretch")
            with b2:
                save_only = st.form_submit_button("仅保存", width="stretch")

        if save_next or save_only:
            missing = [ANNOTATION_FIELDS[f] for f in REQUIRED_FIELDS
                       if not values.get(f, "").strip()]
            if missing:
                st.warning("必填项还没填完：%s" % "、".join(missing))
                st.caption("确实看不出来时，请选择「未确定」，不要留空。")
            elif not annotator.strip():
                st.warning("请先填写标注人（学号或姓名），便于统计工作量。")
            else:
                is_new, filled = ann_store.save(gid, values, annotator.strip())
                st.session_state["ann_ver"] = ver + 1
                st.success("已保存 **%s**（填写 %d 项）%s"
                           % (gid, filled, "，新增" if is_new else "，已更新"))
                if save_next:
                    if only_todo:
                        remaining = [i for i in todo_idx if i > cur]
                        st.session_state["ann_idx"] = \
                            remaining[0] if remaining else cur
                    else:
                        st.session_state["ann_idx"] = min(cur + 1, total_groups - 1)
                st.rerun()

    # ---- 翻页 ----
    st.divider()
    n1, n2, n3, n4 = st.columns([1, 1, 1, 1])
    with n1:
        if st.button("← 上一组", width="stretch", disabled=cur == 0):
            st.session_state["ann_idx"] = cur - 1
            st.rerun()
    with n2:
        if st.button("下一组 →", width="stretch",
                     disabled=cur >= total_groups - 1):
            st.session_state["ann_idx"] = cur + 1
            st.rerun()
    with n3:
        jump = st.number_input("跳到第 N 组", min_value=1, max_value=total_groups,
                               value=cur + 1, step=1, label_visibility="collapsed",
                               key="ann_jump")
        if st.button("跳转", width="stretch", key="ann_jump_btn"):
            st.session_state["ann_idx"] = int(jump) - 1
            st.rerun()
    with n4:
        st.write("")
        if st.button("清空本组标注", width="stretch"):
            if ann_store.delete(gid):
                st.session_state["ann_ver"] = ver + 1
                st.success("已清空 %s" % gid)
                st.rerun()
            else:
                st.info("本组本来就没有标注。")

    # ---- 教师区：合并与导出 ----
    with st.expander("教师操作用：合并标注 / 导出 / 字段统计"):
        st.caption(
            "标注保存后，需要合并回元数据，检索界面的地域、技法、色彩筛选才会生效。")

        t1, t2 = st.columns(2)
        with t1:
            if st.button("合并到检索数据", type="primary", width="stretch"):
                try:
                    res = ann_store.merge_to_images(CSV_PATH)
                    st.success(
                        "合并完成：%d 个分组、%d 行图片已更新（写入 %d 个字段）"
                        % (res["groups_applied"], res["updated_rows"],
                           res["fields_written"]))
                    st.caption("接着可运行 `python build_graph_csv.py` "
                               "重新生成图数据库导入文件。")
                except Exception as e:
                    st.error("合并失败：%s" % e)

        with t2:
            if st.button("导出标注表（发助教用）", width="stretch"):
                try:
                    out = ann_store.export_sheet(
                        os.path.join(PROJECT_DIR, "annotation",
                                     "annotations_export.csv"))
                    st.success("已导出：%s" % out)
                except Exception as e:
                    st.error("导出失败：%s" % e)

        # 各字段填写率
        st.markdown("**各字段填写情况**")
        st.dataframe(
            pd.DataFrame([
                {"字段": ANNOTATION_FIELDS[k],
                 "已填": prog["by_field"][k],
                 "占已标注比": ("%.0f%%" % (prog["by_field"][k] /
                                        max(prog["annotated"], 1) * 100))}
                for k in ANNOTATION_FIELDS
            ]),
            width="stretch", hide_index=True)

        if not prog["annotated"]:
            st.info("还没有任何标注数据。上面随便填一组试试，"
                    "保存后这里就会出现统计。")


# ------------------------------------------------------------------
# ⑥ 知识图谱（图数据库教学）
# ------------------------------------------------------------------
with tab_graph:
    try:
        import graph_teach as gteach
    except ImportError:
        gteach = None

    if gteach is None:
        st.error("图教学模块未就绪（app/graph_teach.py）")
    else:
        st.markdown(
            '<div class="note">'
            '<b>为什么纹样要用「图」来存，而不是表格？</b><br>'
            '一朵苗绣花不只是「一张图」——它属于某个地域、用了某种技法、'
            '有若干变体、被某件学生作品引用……<b>关系本身就是知识</b>。'
            '<br>这个页面带你亲手看清：同一个问题，表格要 JOIN 三层，'
            '图只要走一步。</div>',
            unsafe_allow_html=True,
        )
        st.write("")

        # ---- 数据来源：优先真实 Neo4j，否则 CSV 兜底 ----
        @st.cache_resource(show_spinner=False)
        def _get_csv_graph(_proj):
            return gteach.build_csv_graph(_proj)

        g_obj = None
        g_src = "CSV"
        if DB_OK and graph is not None and getattr(graph, "available", False):
            g_obj = graph
            g_src = "Neo4j"

        if g_obj is None:
            g_obj = _get_csv_graph(PROJECT_DIR)

        # ---- 顶部：数据源状态 ----
        # 状态提示与图规模合并成一条，信息成组；
        # 原来把「节点/关系」摆成两张大数字卡挂在右边，
        # 既和左侧提示脱节，也抢了下方教学内容的注意力。
        _src_slot = st.empty()

        if g_obj is None:
            st.error("无法构建图数据（缺少 graph/csv/ 下的 CSV 文件）。")
        else:
            if g_src == "Neo4j":
                try:
                    _rec = g_obj.run(
                        "MATCH (n) RETURN count(n) AS n") or [{"n": 0}]
                    _n_nodes = _rec[0].get("n", 0)
                    _rec2 = g_obj.run(
                        "MATCH ()-[r]->() RETURN count(r) AS n") or [{"n": 0}]
                    _n_rels = _rec2[0].get("n", 0)
                    stats = {"nodes": _n_nodes, "rels": _n_rels,
                             "by_label": {}, "by_rel": {}}
                    _lb = g_obj.run(
                        "MATCH (n) RETURN labels(n)[0] AS l, count(*) AS c")
                    for r in (_lb or []):
                        stats["by_label"][r["l"]] = r["c"]
                    _rb = g_obj.run(
                        "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c")
                    for r in (_rb or []):
                        stats["by_rel"][r["t"]] = r["c"]
                except Exception:
                    stats = g_obj.stats()
            else:
                stats = g_obj.stats()

            # 状态行：数据源 + 图规模，一次说清
            if g_src == "Neo4j":
                _badge = ("已连接真实图数据库（Neo4j）—— "
                          "下面所有查询都在数据库里真实执行")
                _cls = "done"
            else:
                _why = ""
                if not DB_OK:
                    _why = "（未安装 neo4j 驱动或未配置连接）"
                elif graph is not None and getattr(graph, "error", None):
                    _why = "（%s）" % graph.error
                _badge = ("CSV 离线模式 —— 完整数据已加载到内存，"
                          "查询结果真实，只是由本地引擎执行而非数据库%s。"
                          "配置 NEO4J_URI / NEO4J_PASSWORD 后重启即可切换"
                          % _why)
                _cls = "note"
            _src_slot.markdown(
                '<div class="%s">%s<br>当前图规模：<b>%s</b> 个节点、'
                '<b>%s</b> 条关系</div>'
                % (_cls, _badge, format(stats["nodes"], ","),
                   format(stats["rels"], ",")),
                unsafe_allow_html=True)

            # ==============================================================
            # 板块一：什么是图数据库（知识科普）
            # ==============================================================
            st.divider()
            st.subheader("一、图数据库的三个基本概念")

            k1, k2, k3 = st.columns(3)
            with k1:
                st.markdown("**① 节点（Node）**\n\n"
                            "一个**实体**。在苗绣里就是一张图、一朵纹样、"
                            "一个地域、一种技法。\n\n"
                            "节点可以带**属性**（相当于表格的列），"
                            "比如一张图有 `宽/高/方向`。")
            with k2:
                st.markdown("**② 关系（Relationship）**\n\n"
                            "两个实体之间的**连接**。比如"
                            "「这张图 ——派生自→ 那张图」。\n\n"
                            "关键区别：关系在表里是**外键列**（藏在数据里），"
                            "在图里是**一等公民**（自己也能带属性）。")
            with k3:
                st.markdown("**③ 属性（Property）**\n\n"
                            "挂在节点或关系上的键值对，用来描述特征。\n\n"
                            "比如关系「派生自」可以带一个属性 "
                            "`处理方式: color`，说明是色调变换得来的。")

            st.write("")
            # Markdown 表格必须**一次性**提交：表头与数据行分两次
            # st.markdown 调用时，Streamlit 会把数据行当成普通文本原样输出。
            _lines = ["**本项目实际的图模型**（这就是我们要教的「数据建模」）：",
                      "",
                      "| 节点类型 | 含义 | 当前数量 |",
                      "|---|---|---|"]
            for lbl, (cn, desc, mark) in gteach.NODE_LABELS.items():
                _c = stats["by_label"].get(lbl, 0)
                if _c:
                    _lines.append("| `%s` %s | %s | %s |"
                                  % (lbl, cn, desc, format(_c, ",")))
            st.markdown("\n".join(_lines))

            with st.expander("关系类型一览（点开看每种关系的含义）"):
                _rr = []
                for rtype, (a, b, cn, desc) in gteach.REL_TYPES.items():
                    _c = stats["by_rel"].get(rtype, 0)
                    if _c:
                        _rr.append({
                            "关系": rtype,
                            "中文": cn,
                            "方向": "%s → %s" % (
                                gteach.NODE_LABELS.get(a, (a,))[0],
                                gteach.NODE_LABELS.get(b, (b,))[0]),
                            "含义": desc,
                            "数量": _c,
                        })
                if _rr:
                    st.dataframe(pd.DataFrame(_rr), width="stretch",
                                 hide_index=True)
                else:
                    st.caption("暂无关系数据。")

            # ==============================================================
            # 板块二：表格 vs 图（对照教学）
            # ==============================================================
            st.divider()
            st.subheader("二、同一份数据，两种存法")
            st.caption("这是本页最想让学生记住的一页 —— "
                       "不是「图比表好」，而是**它们擅长的问题不同**。")

            tv = gteach.TABLE_VS_GRAPH
            lc, rc = st.columns(2)
            with lc:
                st.markdown("#### %s" % tv["left_name"])
            with rc:
                st.markdown("#### %s" % tv["right_name"])

            for name, left, right in tv["rows"]:
                c1, c2, c3 = st.columns([1, 2, 2])
                with c1:
                    st.markdown("**%s**" % name)
                with c2:
                    st.markdown(left.replace("\n", "  \n"))
                with c3:
                    st.markdown(right.replace("\n", "  \n"))

            st.info("**结论**：" + tv["结论"])

            # ==============================================================
            # 板块三：动手查询（核心交互）
            # ==============================================================
            st.divider()
            st.subheader("三、动手查询：点一下，看结果，读 Cypher")
            st.caption("每个查询都同时给出 **Cypher（图的写法）** 和 "
                       "**SQL（表的写法）**，对照着看差别在哪里。")

            _pid_list = [p["id"] for p in gteach.QUERY_PRESETS]
            _names = ["%s —— %s" % (p["name"], p["question"])
                      for p in gteach.QUERY_PRESETS]
            _sel = st.selectbox("选一个问题", _names, key="gq_preset")
            _preset = gteach.QUERY_PRESETS[_names.index(_sel)]

            # 需要参数的查询，给个下拉选图
            _params = dict(_preset.get("params_hint") or {})
            if "iid" in _params:
                _opts = ["basic_0", "basic_8", "basic_432", "basic_592",
                         "basic_996"]
                _params["iid"] = st.selectbox(
                    "选择一张源图", _opts,
                    index=max(0, _opts.index(_params["iid"]))
                    if _params["iid"] in _opts else 0,
                    key="gq_iid")

            qc1, qc2 = st.columns(2)
            with qc1:
                st.markdown("**Cypher（图数据库）**")
                st.code(_preset["cypher"], language="cypher")
            with qc2:
                st.markdown("**SQL（关系型数据库）**")
                st.code(_preset["sql"], language="sql")

            st.info(_preset["why"])

            if st.button("▶ 执行查询（跑真实数据）", type="primary",
                         key="gq_run"):
                with st.spinner("查询中…"):
                    try:
                        if g_src == "Neo4j":
                            _recs = g_obj.run(
                                _preset["cypher"], **_params) or []
                            rows = _recs
                        else:
                            rows = g_obj.run_preset(_preset["id"], _params)
                    except Exception as e:
                        rows = None
                        st.error("查询失败：%s" % e)

                if rows is None:
                    pass
                elif not rows:
                    if _preset["id"] in ("q_count_by_region", "q_tech_region"):
                        st.warning(
                            "查询执行成功，但**返回 0 行**。\n\n"
                            "这不是报错 —— 而是说明：现有数据里还没有满足"
                            "条件的记录。**数据不完整本身就是《数据采集"
                            "与清洗》要处理的问题**：纹样的「地域」「技法」"
                            "字段还是空的，所以按它们分组自然分不出东西。"
                            "去 ⑤ 标注纹样 补上属性，这里立刻就有数据了。")
                    else:
                        st.warning("查询执行成功，但没有匹配的记录。")
                else:
                    df = pd.DataFrame(rows)
                    st.success("返回 **%d** 行" % len(df))
                    st.dataframe(df, width="stretch", hide_index=True)
                    if len(df) == 1:
                        st.caption("只有一行 —— 说明这个关系在当前数据里"
                                   "比较集中（或该维度尚未标注）。")

            st.divider()
            st.caption(
                "**教学要点** —— 图数据库的价值不在「存」，而在「**连**」。"
                "当你的问题变成「这两个东西之间怎么走过去的」、"
                "「和它有关系的还有谁」时，图的优势会立刻显现；"
                "而单纯的求和、计数、报表，关系型数据库依然是更成熟的选择。")


# ------------------------------------------------------------------
st.divider()
st.caption(
    "《数据采集与清洗》课程 · 苗绣纹样数字化教学模块 · "
    "公开展示时请注明原始素材来源与 AI 辅助生成情况")
