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

    # ---- 控件盘点，确认四个标签页都渲染出来了 ----
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

    # 六个标签页：检索 / 创作 / 作品 / 提取 / 标注 / 知识图谱
    if len(at.tabs) < 6:
        ok = False
        print("  [FAIL] 标签页少于 6 个，新增的「知识图谱」页可能没渲染出来")
    else:
        print("  [PASS] 六个标签页均已渲染")

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
    _md_all = " ".join(str(getattr(el, "value", "") or "")
                       for el in getattr(at, "markdown", []))
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
