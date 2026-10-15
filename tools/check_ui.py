# -*- coding: utf-8 -*-
"""
界面无头自测

为什么需要：
    Streamlit 应用只在浏览器连接时才执行脚本，所以「HTTP 200」
    只能证明服务起来了，证明不了页面逻辑没问题 —— 某个标签页里
    有个未定义变量，curl 是发现不了的。

    这个脚本用 Streamlit 官方的 AppTest 无头跑一遍完整页面，
    把运行期异常、错误提示、控件数量都打出来。

用法：
    python tools/check_ui.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import traceback

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, ROOT)

APP = os.path.join(ROOT, "app", "streamlit_app.py")


def e2e_annotate(at, tmp_csv):
    """端到端验证标注流程：填表 → 提交 → 落盘。

    这是最有价值的一段 —— 它证明「学生点几下就能完成标注」这条路径
    真的走得通，而不只是页面能渲染。

    注意：通过 ANNOTATION_CSV 环境变量把数据写到临时文件，
          不会污染真实的 annotation/annotations.csv。
    """
    print()
    print("-" * 62)
    print("端到端：标注填表 → 提交 → 落盘")
    print("-" * 62)

    ok = True

    # 1. 填标注人（这个输入框在表单外，可以单独生效）
    ann_inputs = [t for t in at.text_input if "标注人" in t.label]
    if not ann_inputs:
        print("  [FAIL] 找不到「标注人」输入框")
        return False
    ann_inputs[0].input("E2E_TESTER")
    at.run()

    # 2. 填必填的四个下拉框
    #
    # 注意 Streamlit 的表单语义：表单内的控件在「提交前」不会随 rerun 更新，
    # 所以设完值必须直接提交，中间不能插 at.run()，否则选择会被冲掉。
    # （第一版测试就是踩了这个坑，误报成应用有 bug。）
    picks = {
        "f_pattern_type_": "花卉纹",
        "f_region_": "贵州黔东南",
        "f_technique_": "破线绣",
        "f_dominant_color_": "靛蓝",
    }
    chosen = {}
    for sb in at.selectbox:
        key = sb.key or ""
        for prefix, value in picks.items():
            if key.startswith(prefix):
                sb.select(value)
                chosen[prefix] = value

    if len(chosen) < 4:
        print("  [FAIL] 只找到 %d / 4 个必填下拉框" % len(chosen))
        ok = False

    # 3. 提交表单（紧接着上一步，不插 rerun）
    submit = [b for b in at.button if "保存并看下一组" in (b.label or "")]
    if not submit:
        print("  [FAIL] 找不到「保存并看下一组」按钮")
        return False

    submit[0].click()
    at.run()

    # 提交后如果出现必填项警告，说明值没带上
    warns = [str(w.value) for w in at.warning]
    if any("必填项还没填完" in w for w in warns):
        print("  [FAIL] 提交后提示必填项缺失，表单值未随提交传递")
        print("         警告：%s" % warns)
        return False
    if len(chosen) >= 4:
        print("  [PASS] 四个必填项已随表单提交")

    # 4. 校验落盘
    if not os.path.exists(tmp_csv):
        print("  [FAIL] 提交后标注文件未生成：%s" % tmp_csv)
        return False
    print("  [PASS] 标注文件已生成")

    import csv as _csv
    with open(tmp_csv, encoding="utf-8-sig", newline="") as f:
        rows = list(_csv.DictReader(f))

    if not rows:
        print("  [FAIL] 标注文件为空")
        return False

    row = rows[0]
    checks = [
        ("记录标注人", row.get("annotator") == "E2E_TESTER"),
        ("记录纹样类型", row.get("pattern_type") == "花卉纹"),
        ("记录地域来源", row.get("region") == "贵州黔东南"),
        ("记录刺绣技法", row.get("technique") == "破线绣"),
        ("记录主色调", row.get("dominant_color") == "靛蓝"),
        ("记录时间戳", bool(row.get("updated_at"))),
        ("记录分组编号", bool(row.get("group_id"))),
    ]
    for name, cond in checks:
        print("  [%s] %s" % ("PASS" if cond else "FAIL", name))
        if not cond:
            ok = False

    print()
    print("  落盘内容：group_id=%s annotator=%s 类型=%s 地域=%s 技法=%s 主色=%s"
          % (row.get("group_id"), row.get("annotator"),
             row.get("pattern_type"), row.get("region"),
             row.get("technique"), row.get("dominant_color")))

    return ok


def children_of(el):
    """容器的子节点。AppTest 有的容器用 dict 存，有的用 list 存。"""
    ch = getattr(el, "children", None)
    return list(ch.values()) if isinstance(ch, dict) else list(ch or [])


def walk(el):
    yield el
    for c in children_of(el):
        for x in walk(c):
            yield x


def text_of(el):
    """取元素文本。dataframe 的 value 是 DataFrame，别拿去做真值判断。"""
    v = getattr(el, "value", None)
    return v if isinstance(v, str) else ""


def find_plots(container):
    """取出容器里所有 plotly 图的 (traces, layout)。

    AppTest 没有 plotly 专用访问器，图在元素树里表现为
    UnknownElement，真正的类型得看 proto 的类名。
    """
    out = []
    for e in walk(container):
        if type(e).__name__ != "UnknownElement":
            continue
        if type(getattr(e, "proto", None)).__name__ != "PlotlyChart":
            continue
        try:
            fig = json.loads(e.proto.spec)
        except (ValueError, AttributeError, TypeError):
            continue
        out.append((fig.get("data", []), fig.get("layout", {})))
    return out


def main():
    print("=" * 62)
    print("界面无头自测（AppTest）")
    print("=" * 62)
    print("应用：%s" % APP)
    print()

    try:
        from streamlit.testing.v1 import AppTest
    except ImportError as e:
        print("[跳过] 当前 streamlit 版本不支持 AppTest：%s" % e)
        print("       streamlit >= 1.28 才有 st.testing.v1")
        return 0

    # 标注数据写到临时文件，避免污染真实数据
    import tempfile
    tmp_csv = os.path.join(tempfile.mkdtemp(prefix="uite2e_"), "annotations.csv")
    os.environ["ANNOTATION_CSV"] = tmp_csv
    print("标注测试文件：%s" % tmp_csv)
    print()

    at = AppTest.from_file(APP, default_timeout=120)

    try:
        at.run()
    except Exception:
        print("[失败] 页面运行抛出未捕获异常：")
        traceback.print_exc()
        return 1

    ok = True

    # ---- 运行期异常 ----
    n_exc = len(at.exception)
    print("运行期异常：%d" % n_exc)
    for e in at.exception:
        ok = False
        print("  !! %s" % e.value)
        st_trace = getattr(e, "stack_trace", None)
        if st_trace:
            for line in str(st_trace).splitlines()[:12]:
                print("     %s" % line)
    if n_exc == 0:
        print("  [PASS] 无未捕获异常")
    print()

    # ---- st.error ----
    errs = list(at.error)
    print("st.error 数量：%d" % len(errs))
    for e in errs:
        print("  [error] %s" % str(e.value)[:300])
    if not errs:
        print("  [PASS] 无错误提示")
    print()

    # ---- st.warning（降级提示属正常，只展示）----
    warns = list(at.warning)
    if warns:
        print("st.warning 数量：%d（降级提示属正常）" % len(warns))
        for w in warns[:5]:
            print("  [warn] %s" % str(w.value)[:200])
        print()

    # ---- 控件盘点，确认各标签页都渲染出来了 ----
    print("控件盘点：")
    print("  标签页    %d" % len(at.tabs))
    print("  按钮      %d" % len(at.button))
    print("  下拉框    %d" % len(at.selectbox))
    print("  文本框    %d" % len(at.text_input))
    print("  多选框    %d" % len(at.multiselect))
    print("  数据表格  %d" % len(at.dataframe))
    print("  指标卡    %d" % len(at.metric))
    # 注：AppTest 在本版本没有 progress 访问器，进度条用 metric 间接确认
    print()

    # 八个标签页：课程导航 / 检索 / 创作 / 作品 / 展示墙 / 提取 / 标注 / 知识图谱
    #
    # 为什么要写成「>= 8」而不是「== 8」：
    # 课程项目会持续加页（例如以后可能加「课堂讨论」），断言等号会让
    # 每次加页都得改测试。这里真正要守住的是「已有的页不能掉」，
    # 所以用下限；具体哪几页存在，由下面几条按内容的关键词断言来保证。
    if len(at.tabs) < 8:
        ok = False
        print("  [FAIL] 标签页少于 8 个，「课程导航」或「作品展示墙」可能没渲染出来")
    else:
        print("  [PASS] 八个标签页均已渲染（当前 %d 个）" % len(at.tabs))

    # ---- 页签顺序 ----
    #
    # 本文件早年有一句注释说「AppTest 读不到页签标题文本」，那是过时的：
    # 实测 streamlit 1.65 的 Tab 元素有 .label。能读到就应该守顺序 ——
    # 用户明确要求「作品展示墙移到最后面去」，搬错位置这里必须报出来。
    # 读不到 label 的环境（老版本）则整段跳过，不误判失败。
    _labels = [getattr(t, "label", "") for t in at.tabs]
    if any(not L for L in _labels):
        print("  [跳过] 页签顺序 —— 当前 streamlit 读不到 Tab 标题文本")
    else:
        # ⑦ 在最后是用户要求的；其余按 ⓪→⑥ 的业务链路顺序。
        _want = ["⓪ 课程导航", "① 检索纹样", "② AI 重组创作",
                 "③ 我的作品", "④ 提取纹样", "⑤ 标注纹样",
                 "⑥ 知识图谱", "⑦ 作品展示墙"]
        if _labels == _want:
            print("  [PASS] 页签顺序正确：%s" % " ".join(_labels))
        elif _labels[-1] == "⑦ 作品展示墙":
            print("  [PASS] ⑦ 作品展示墙 排在最后（顺序为 %s）"
                  % " ".join(_labels))
        else:
            ok = False
            print("  [FAIL] ⑦ 作品展示墙 不在最后，当前顺序：%s"
                  % " ".join(_labels))

    # ---- 每个页面都画出了东西（空页是最容易漏的一类回归）----
    #
    # 移动页面时最容易出的错：整块搬过去了，但漏搬了收尾的 with，
    # 结果是页面存在却空空如也 —— 数量对、内容是空的，前面几条
    # 按按钮/关键词的断言全都发现不了。
    _empty = []
    for _t in at.tabs:
        if len(list(walk(_t))) <= 1:      # Tab 容器自己算一个
            _empty.append(getattr(_t, "label", "?"))
    if _empty:
        ok = False
        print("  [FAIL] 存在空页面：%s" % "、".join(_empty))
    else:
        print("  [PASS] 八个页面全部有内容，无空页")

    # ---- ⓪ 课程导航：单元与分层说明确实渲染出来了 ----
    #
    # 说明：页签标题文本在 streamlit 1.65 上是可以读到的（见上面
    # 「页签顺序」那段），但**页签内的内容**仍只能回到 markdown 正文
    # 找关键词 —— 因为 at.markdown 是全页合计，没有按页拆分。
    # 缺了这几个词，就说明 course.UNITS 被改坏了、或页面里的循环没跑全
    #
    # 关键词怎么选的（对着 app/streamlit_app.py 的 ⓪ 页实现核对过）：
    #   ·「课时」   —— 出现在单元小标题「第 N 单元 · X 课时（理论…）」里，
    #                  四个单元循环渲染，所以出现次数 = 单元数
    #   ·「基础层」 —— 每个单元都印「要做的事（基础层）」
    #   ·「单元已完成」—— 进度行「课程进度：N / 4 单元已完成」
    # 这里**不**断言「拓展层」：它只在选中拓展层时才渲染（默认基础层，
    # 拓展要求收在 expander 里），断言它会误报失败。
    # _md_all 在下面的「标注进度区」还要复用，所以在这里先算出来。
    _md_all = " ".join(str(getattr(el, "value", "") or "")
                       for el in getattr(at, "markdown", []))
    _course_keys = ["课时", "基础层", "单元已完成"]
    _miss = [k for k in _course_keys if k not in _md_all]
    if _miss:
        ok = False
        print("  [FAIL] 课程导航页内容不完整，缺少关键词：%s" % "、".join(_miss))
    else:
        print("  [PASS] 课程导航页已渲染（含课时、基础层任务与进度行）")

    # 四个单元都该渲染出标题。数「单元 ·」的出现次数 —— 比数「课时」
    # 更准，因为「课时」在顶部总述里也出现一次（会多算 1）。
    _n_unit = _md_all.count("单元　·　")
    if _n_unit < 4:
        ok = False
        print("  [FAIL] 只渲染出 %d 个单元标题，应该有 4 个" % _n_unit)
    else:
        print("  [PASS] 四个教学单元均已渲染")

    # ---- ⓪ 课程导航：学号输入框（分层与进度都挂在它上面）----
    _sid_boxes = [t for t in at.text_input
                  if "学号" in (getattr(t, "label", "") or "")]
    if not _sid_boxes:
        ok = False
        print("  [FAIL] 找不到「学号」输入框，课程进度无法按人统计")
    else:
        print("  [PASS] 学号输入框已渲染")

    # ---- ⓪ 课程导航：分层单选（基础层 / 拓展层）----
    _track_radios = [r for r in at.radio
                     if "学习层次" in (getattr(r, "label", "") or "")]
    if not _track_radios:
        ok = False
        print("  [FAIL] 找不到「选择你的学习层次」单选，课程导航的分层入口丢了")
    else:
        print("  [PASS] 分层选择已渲染（全页单选合计 %d 组）" % len(at.radio))

    # ---- ⑦ 作品展示墙：筛选与排序控件 ----
    #
    # 展示墙的核心交互是三个控件（方式下拉 / 只看没评过的多选 / 排序下拉）。
    # 但 at.selectbox 是**全页合计**，没法按页拆分，所以这里只在总数
    # 明显偏少时报错 —— 宁可漏报也不要误判，误判会让别人不再信这个脚本。
    if len(at.selectbox) < 6:
        ok = False
        print("  [FAIL] 下拉框总数偏少（%d），展示墙的筛选/排序可能没渲染"
              % len(at.selectbox))
    else:
        print("  [PASS] 展示墙筛选与排序控件已渲染（下拉框合计 %d 个）"
              % len(at.selectbox))

    # 提取页应有「开始提取纹样」按钮
    ex_btns = [b for b in at.button if "开始提取纹样" in (b.label or "")]
    if not ex_btns:
        ok = False
        print("  [FAIL] 找不到「开始提取纹样」按钮")
    else:
        print("  [PASS] 提取纹样页已渲染")

    # 知识图谱页应有「执行查询」按钮（图数据库教学的核心交互）
    gq_btns = [b for b in at.button if "执行查询" in (b.label or "")]
    if not gq_btns:
        ok = False
        print("  [FAIL] 找不到「执行查询」按钮，知识图谱页可能没渲染")
    else:
        print("  [PASS] 知识图谱页已渲染")

    # ---- ⑥ 知识图谱：结构图真的画出来了 ----
    #
    # 「页面渲染了」和「图真的画出来了」是两件事：只要 plotly 没装、
    # 或图数据没加载，那段代码会静默走降级分支，页面照样有按钮、
    # 照样没异常 —— 但学生看到的是一片空白。
    # 所以这里既要有正面证据（trace 里带真实坐标），
    # 也要有反面证据（没有落到「未安装 plotly / 无图数据」的提示）。
    _tab6 = at.tabs[6] if len(at.tabs) >= 7 else None
    if _tab6 is None:
        ok = False
        print("  [FAIL] 找不到第 6 个页面（知识图谱）")
    else:
        _t6_text = [text_of(e) for e in walk(_tab6)]
        _t6_all = " ".join(_t6_text)

        if "图结构一览" not in _t6_all:
            ok = False
            print("  [FAIL] ⑥ 页缺少「图结构一览」小节")
        else:
            print("  [PASS] ⑥ 页结构图小节已渲染")

        _fallback = [s for s in _t6_text
                     if "未安装 plotly" in s or "无图数据" in s]
        if _fallback:
            ok = False
            print("  [FAIL] ⑥ 页结构图落到了降级分支：%s" % _fallback)
        else:
            print("  [PASS] ⑥ 页结构图没有降级（图是真的画了）")

        _plots = find_plots(_tab6)
        if len(_plots) != 1:
            ok = False
            print("  [FAIL] ⑥ 页应有 1 张结构图，实际 %d 张" % len(_plots))
        else:
            _traces, _layout = _plots[0]
            _marks = [t for t in _traces
                      if "markers" in (t.get("mode") or "")]
            _lines = [t for t in _traces
                      if (t.get("mode") or "") == "lines"]
            _blank = [t for t in _traces if not t.get("x")]

            if len(_marks) >= 2 and len(_lines) >= 1:
                print("  [PASS] 结构图有 %d 个节点、%d 条关系边"
                      % (len(_marks), len(_lines)))
            else:
                ok = False
                print("  [FAIL] 结构图节点或边缺失（节点 %d，边 %d）"
                      % (len(_marks), len(_lines)))

            if _blank:
                ok = False
                print("  [FAIL] 有 %d 条 trace 没有坐标数据（画出来是隐形的）"
                      % len(_blank))
            else:
                print("  [PASS] 全部 %d 条 trace 都带坐标" % len(_traces))

            # 节点标签必须是「类型<br>真实数量」，光有圆点不算数
            _labelled = [t for t in _marks
                         if t.get("text") and "<br>" in str(t["text"][0])]
            if len(_labelled) == len(_marks) and _marks:
                print("  [PASS] 节点标注了真实数量：%s"
                      % "、".join(str(t["text"][0]).replace("<br>", " ")
                                  for t in _marks))
            else:
                ok = False
                print("  [FAIL] 只有 %d/%d 个节点标了数量"
                      % (len(_labelled), len(_marks)))

            # 自环（如 Image -[派生自]-> Image）必须画成外侧弧；
            # 若退化成直线，会在环内穿过圆心，把整张图搅乱。
            _arcs = [t for t in _lines if len(t.get("x", [])) > 2]
            if _arcs:
                print("  [PASS] %d 条自环画成了外侧弧线" % len(_arcs))
            else:
                print("  [提示] 当前数据集没有同类之间的关系，弧线分支未触发")

            if _layout.get("height") == 560:
                print("  [PASS] 图高固定 560，不会撑变形")
            else:
                ok = False
                print("  [FAIL] 图高不是 560，实际 %s" % _layout.get("height"))

    # ---- 界面上不该再出现的文案（被用户点名删掉的）----
    #
    # 这类文案删掉之后最容易「活过来」：有人照着旧文档改界面，
    # 一眼看过去界面正常，但那句话又回来了。扫源码 + 扫渲染结果各一次。
    for _bad in ["（可选，能填尽量填）"]:
        try:
            with open(APP, encoding="utf-8") as _f:
                _src = _f.read()
        except OSError:
            _src = ""
        if _bad in _src or _bad in _md_all:
            ok = False
            print("  [FAIL] 已删除的文案又出现了：%s" % _bad)
        else:
            print("  [PASS] 已删除的文案没有复活：%s" % _bad)

    # 标注页应产生若干下拉框（11 个字段，其中 6 个是下拉）
    if len(at.selectbox) < 4:
        ok = False
        print("  [FAIL] 下拉框过少，标注表单可能没渲染")
    else:
        print("  [PASS] 标注表单已渲染（下拉框 %d 个）" % len(at.selectbox))

    # 标注进度区。
    # 注意：这里**不再**检查 metric 卡数量 —— 标注页的进度总览
    # 已从「四张大数字卡」改为「一行紧凑状态文本」（见 streamlit_app.py
    # 里的注释：仪表盘式布局会把注意力从纹样上拉走）。
    # 所以改为在 markdown 文本里找「已标注 / 完成度」这两个关键词。
    # _md_all 已在上面「课程导航」那段算好并复用。
    if "已标注" in _md_all and "完成度" in _md_all:
        print("  [PASS] 标注进度区已渲染（进度状态行）")
    elif len(at.metric) >= 1:
        print("  [PASS] 标注进度区已渲染（指标卡 %d 个）" % len(at.metric))
    else:
        ok = False
        print("  [FAIL] 找不到标注进度区（既无进度行也无指标卡）")

    # ---- 端到端：真的填一次表并提交 ----
    if n_exc == 0 and not errs:
        if not e2e_annotate(at, tmp_csv):
            ok = False
    else:
        print()
        print("[跳过] 端到端测试 —— 页面本身就有异常，先修上面那些")

    print()
    print("=" * 62)
    print("结果：%s" % ("通过" if ok else "存在问题，见上方 [FAIL]"))
    print("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
