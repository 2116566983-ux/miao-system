# -*- coding: utf-8 -*-
"""
苗绣纹样系统 · 视觉主题

设计原则
--------
**不用通用调色板。** 所有颜色都从这套数据集里实际测出的主色调中取，
再按苗绣工艺的真实材料定名——靛蓝是土布，朱红是绣线，银白是饰品。

数据来源：`metadata/images.csv` 的 `dominant_color` 字段实际分布
  （靛蓝 52 / 朱红 112 / 藏青 36 / 明黄 16 / 墨黑 436 …）

排版上只用**一条竖线**做信息层级，不用彩色圆角卡片——
后者是模板化界面的典型特征，且会把视线从纹样本身抢走。
"""

# ---------------------------------------------------------------------------
# 色卡：材料取名，而非「主色/辅色」
# ---------------------------------------------------------------------------

# 靛蓝土布——苗绣最常见的底布，整套界面的根基色
INDIGO = "#243b53"        # 深靛（底布浸染多次）
INDIGO_DEEP = "#1a2b3c"   # 夜靛（正文强调）
INDIGO_SOFT = "#eaeff4"   # 洗淡的靛（浅底）

# 朱砂绣线——传统苗绣最醒目的红，用于关键动作与警示
CINNABAR = "#a8352a"      # 朱红（主强调、警示）
CINNABAR_SOFT = "#fbeeec" # 淡朱（浅底）

# 藏青——比靛蓝更冷，用于次级信息
NAVY = "#3d5a80"

# 银饰——苗族银饰的白，用于分隔、边框
SILVER = "#c9d1d9"
SILVER_DEEP = "#8b949e"

# 素纸——不用纯白，#fafafa 更接近布面
PAPER = "#f8f7f4"
INK = "#2b2b2b"
INK_SOFT = "#5c5c5c"
INK_MUTE = "#8a8a8a"


# ---------------------------------------------------------------------------
# 全局样式
# ---------------------------------------------------------------------------

def global_css() -> str:
    """返回注入 <style> 的整段 CSS。"""
    return """
    <style>
      /* ---- 基础排版 ---- */
      .block-container { padding-top: 1.6rem; max-width: 1500px; }
      html, body, [class*="css"] {
        font-family: "Noto Sans SC", "Source Han Sans SC",
                     "PingFang SC", "Microsoft YaHei", sans-serif;
      }

      /* ---- 标题：左侧一道竖线代替分割线 ---- */
      h1 { font-size: 1.55rem; font-weight: 600;
           letter-spacing: .06em; color: %(ink)s; }
      h2 { font-size: 1.18rem; font-weight: 600;
           letter-spacing: .04em; color: %(indigo_deep)s;
           border-left: 3px solid %(cinnabar)s;
           padding-left: .6rem; margin-top: 1.6rem; }
      h3 { font-size: 1rem; font-weight: 600; color: %(indigo)s; }

      /* ---- 分割线压细，别喧宾夺主 ---- */
      hr { border-color: %(silver)s; opacity: .5; margin: 1.4rem 0; }

      /* ---- 图片：直角为主，只给极轻的圆角 ---- */
      div[data-testid="stImage"] img {
        border-radius: 3px;
        border: 1px solid %(silver)s;
      }

      /* ---- 检索页缩略图：固定高度方框，保证网格对齐 ----
         混排竖图/方图时，不定高的图片会把下方按钮推得参差不齐，
         固定高度是唯一稳妥的做法。
         但用 cover 会**裁掉纹样主体**（本数据集竖图多，一裁就是花心），
         所以用 contain + 淡靛蓝底色：完整显示优先于填满。
         这是纹样库，纹理的完整性比画面饱满更重要。 */
      .thumb {
        height: 190px; overflow: hidden; border-radius: 3px;
        border: 1px solid %(silver)s; background: %(indigo_soft)s;
        display: flex; align-items: center; justify-content: center;
      }
      .thumb img { max-width: 100%%; max-height: 100%%;
                   width: auto; height: auto; display: block; }
      .thumb-miss { color: %(ink_mute)s; font-size: .84rem; }

      /* ---- 按钮 ----
         主按钮用深靛蓝而非朱砂：朱砂留给「警示」和「强调标记」，
         到处用红会迅速贬值，也会让整页显得焦躁。
         靛蓝是这套数据的底色（土布），拿来做主色更稳。 */
      .stButton > button {
        border-radius: 3px;
        border: 1px solid %(silver)s;
        font-weight: 500;
        letter-spacing: .03em;
        transition: none;
      }
      .stButton > button[kind="primary"] {
        background: %(indigo)s;
        border-color: %(indigo)s;
        color: #fff;
      }
      .stButton > button[kind="primary"]:hover {
        background: %(indigo_deep)s; border-color: %(indigo_deep)s;
      }

      /* ---- 输入控件统一描边 ---- */
      .stTextInput input, .stSelectbox div[data-baseweb="select"],
      .stMultiSelect div[data-baseweb="select"], .stNumberInput input {
        border-radius: 3px;
      }

      /* ---- Tabs：下划线式，去掉胶囊底 ---- */
      .stTabs [data-baseweb="tab-list"] {
        gap: .2rem; border-bottom: 1px solid %(silver)s;
      }
      .stTabs [data-baseweb="tab"] {
        border-radius: 0; padding: .5rem .9rem;
        font-size: .92rem; letter-spacing: .03em;
        color: %(ink_soft)s;
      }
      .stTabs [aria-selected="true"] {
        color: %(indigo_deep)s; font-weight: 600;
        border-bottom: 2px solid %(cinnabar)s;
      }

      /* ---- 指标卡：去卡片化，只留数字与细标 ---- */
      div[data-testid="stMetricValue"] {
        font-size: 1.45rem; font-weight: 600; color: %(indigo_deep)s;
      }
      div[data-testid="stMetricLabel"] {
        font-size: .78rem; color: %(ink_mute)s; letter-spacing: .04em;
      }

      /* ================================================================
         以下为语义块。全部用「一条竖线 + 淡底」，
         不用彩色圆角卡片。
         ================================================================ */

      /* 说明 / 提示 */
      .note {
        border-left: 3px solid %(navy)s; background: %(indigo_soft)s;
        padding: .65rem .9rem; font-size: .88rem; color: %(indigo_deep)s;
        line-height: 1.7;
      }
      /* 注意 / 需判断 */
      .caution {
        border-left: 3px solid %(cinnabar)s; background: %(cinnabar_soft)s;
        padding: .65rem .9rem; font-size: .88rem; color: #7a241c;
        line-height: 1.7;
      }
      /* 已完成 / 通过 */
      .done {
        border-left: 3px solid #4f7a52; background: #f0f4f0;
        padding: .65rem .9rem; font-size: .88rem; color: #33503a;
        line-height: 1.7;
      }
      /* 机器给出的线索（需要人再判断）
         单独一档：它既不是「说明」（.note 平静），也不是「警示」
         （.caution 会让整块变红）。机器线索的正确读法是「摆在旁边
         供参考的证据」，所以用旧纸的黄褐色，语气是存疑而非命令。 */
      .hint {
        border-left: 3px solid #b58900; background: #fbf7e8;
        padding: .6rem .9rem; font-size: .84rem; color: #6b5416;
        line-height: 1.75;
      }
      .hint b { color: #8a6a12; }
      .hint .hint-warn { color: %(cinnabar)s; }

      /* 纹样卡片文字 */
      .motif-title { font-size: .92rem; font-weight: 600;
                     margin: .4rem 0 .1rem 0; color: %(ink)s; }
      .motif-meta  { font-size: .76rem; color: %(ink_mute)s;
                     margin-bottom: .2rem; letter-spacing: .02em; }

      /* 状态标记：不发光、不圆角，像印章 */
      .tag { display: inline-block; font-size: .7rem; letter-spacing: .06em;
             padding: .1rem .4rem; border: 1px solid %(silver_deep)s;
             border-radius: 2px; color: %(ink_soft)s; margin-left: .3rem; }
      .tag-off { border-color: %(cinnabar)s; color: %(cinnabar)s; }
      .tag-on  { border-color: #4f7a52; color: #4f7a52; }

      /* 数据源标识 */
      .src { font-size: .74rem; color: %(ink_mute)s; letter-spacing: .04em; }
      .src b { color: %(indigo_deep)s; font-weight: 600; }

      /* 章节序号 */
      .sec-no { font-size: .74rem; letter-spacing: .18em;
                color: %(cinnabar)s; font-weight: 600; }
    </style>
    """ % dict(
        indigo=INDIGO, indigo_deep=INDIGO_DEEP, indigo_soft=INDIGO_SOFT,
        cinnabar=CINNABAR, cinnabar_soft=CINNABAR_SOFT,
        navy=NAVY, silver=SILVER, silver_deep=SILVER_DEEP,
        ink=INK, ink_soft=INK_SOFT, ink_mute=INK_MUTE,
    )


# ---------------------------------------------------------------------------
# 语义块的便捷包装
# ---------------------------------------------------------------------------

def note(html: str) -> str:
    """说明性文字。对应原来的 st.info（蓝色圆角框）。"""
    return '<div class="note">%s</div>' % html


def caution(html: str) -> str:
    """需要留意/判断的文字。对应原来的 st.warning。"""
    return '<div class="caution">%s</div>' % html


def done(html: str) -> str:
    """已完成/通过。对应原来的 st.success（绿色圆角框）。"""
    return '<div class="done">%s</div>' % html


def hint(html: str) -> str:
    """机器给出、需人工复核的线索。

    与 caution 的区别：caution 是「你要小心」；hint 是「机器这么猜，
    你自己看」。教学上这个区别很重要——不能让机器结论看起来像权威
    指令，否则学生就停止观察了。
    """
    return '<div class="hint">%s</div>' % html


def tag(text: str, kind: str = "") -> str:
    """状态标记。kind: '' | 'on' | 'off'"""
    cls = "tag" + (" tag-%s" % kind if kind else "")
    return '<span class="%s">%s</span>' % (cls, text)
