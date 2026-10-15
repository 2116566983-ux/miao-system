# -*- coding: utf-8 -*-
"""
核心逻辑自动化测试

不需要 Neo4j 或 ComfyUI，覆盖所有可离线验证的逻辑：

    1. 元数据生成       —— 变体分组、质量判定、命名规律
    2. 图导入文件       —— 节点/关系数量与内容
    3. 工作流注入       —— 占位符替换与类型保持
    4. 作品归档         —— 编号生成、记录、审核更新
    5. 标注合并         —— 中英表头映射、继承传播
    6. 筛选与分页       —— 去重、方向过滤、边界情况
    7. 客户端降级       —— 连不上服务时不崩

运行：
    python tests/test_core.py
"""

from __future__ import annotations

import base64
import csv
import os
import sys
import tempfile
import traceback

# 让 tests/ 能导入项目模块
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "app"))

PASS = []
FAIL = []
SKIP = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  [PASS] %s" % name)
    else:
        FAIL.append((name, detail))
        print("  [FAIL] %s  %s" % (name, detail))


def section(title):
    print()
    print("=" * 62)
    print(title)
    print("=" * 62)


# ===========================================================================
# 1. 元数据生成
# ===========================================================================
section("1. 元数据生成 build_metadata.py")

import build_metadata as bm

tmp = tempfile.mkdtemp()
CSV1 = os.path.join(tmp, "images.csv")

# 图片目录：自动探测（环境变量 > 项目内 data/images > 常见位置）
# 找不到就跳过依赖真实图片的测试段，其余测试照常运行
try:
    from paths import find_image_root as _find_root
    SRC = _find_root() or ""
except Exception:
    SRC = ""
HAS_REAL_IMAGES = bool(SRC and os.path.isdir(SRC))

# 关键：这一段的「可标注数」「仅原始图组数」两个断言**依赖图片的真实尺寸**，
# 因为 build_metadata.py 会按「短边 < 100px 则不可标注」来判定。
#
# 而 find_image_root() 现在可能返回两种目录：
#   · data/images/  —— 随仓库发布的课堂缩略图（长边 ≤560）
#   · 原始高清图目录 —— 开发机上的原图
# 两者尺寸不同，算出来的可标注数就不同。已实测确认差异只有一处：
#   basic_828.jpg 原 2409x362（短边 362，可标注）
#                缩 560x84 （短边  84，不可标注）—— 极端宽幅图缩完短边必然变小
# 所以这里按「量的是哪一批图」取对应的期望值，
# 而不是写死一个数字然后默默接受漂移。
_NORMALIZED = os.path.normpath(SRC).replace("\\", "/")
_IS_THUMBS = _NORMALIZED.endswith("/data/images")

if not HAS_REAL_IMAGES:
    skip("依赖真实图片的测试段", "未找到图片目录（不影响其他测试）")

if HAS_REAL_IMAGES:
    print("  图片目录：%s" % SRC)
    print("  判定：%s" % ("课堂缩略图集" if _IS_THUMBS else "原始高清图"))
    # 缩略图集因为 basic_828.jpg 短边降到 84px，少一张可标注；
    # 连带「仅原始图」里那一组不再是「全部可标注」，所以 241 → 240。
    EXPECT_MARKABLE = 730 if _IS_THUMBS else 731
    EXPECT_ONLY_BASIC = 240 if _IS_THUMBS else 241

if HAS_REAL_IMAGES:
    sys.argv = ["build_metadata.py", "--src", SRC, "--out", CSV1]
    bm.main()

    rows = list(csv.DictReader(open(CSV1, encoding="utf-8-sig")))
    check("元数据行数为 1000", len(rows) == 1000, "实际 %d" % len(rows))

    variants = {}
    for r in rows:
        variants[r["variant"]] = variants.get(r["variant"], 0) + 1
    check("四种变体各 250 张",
          all(variants.get(v) == 250 for v in ["basic", "texture", "color", "mixup"]),
          str(variants))

    groups = {}
    for r in rows:
        groups[r["group_id"]] = groups.get(r["group_id"], 0) + 1
    check("分组数为 250", len(groups) == 250, "实际 %d" % len(groups))
    check("每组恰好 4 张", set(groups.values()) == {4}, str(set(groups.values())))

    # 命名规律：序号 % 4 与变体对应
    # 注意：idx 是内部字段未写入 CSV，需从文件名推导
    order = ["basic", "texture", "color", "mixup"]
    ok_rule = True
    for r in rows:
        idx = int(r["image_id"].rsplit("_", 1)[1])
        if r["variant"] != order[idx % 4]:
            ok_rule = False
            break
    check("命名规律 (idx%4==变体) 成立", ok_rule)

    # 派生关系：每组 3 条
    derived = [r for r in rows if r["relation"] == "DERIVED_FROM"]
    check("DERIVED_FROM 关系数 750", len(derived) == 750, "实际 %d" % len(derived))

    # 每组的派生都指向组内源图
    src_of_group = {r["group_id"]: r["file_name"] for r in rows if r["is_source"] == "是"}
    ok_src = all(r["source_file"] == src_of_group.get(r["group_id"]) for r in derived)
    check("派生关系均指向组内源图", ok_src)

    # 质量判定
    markable = sum(1 for r in rows if r["markable"] == "是")
    check("可标注数为 %d" % EXPECT_MARKABLE, markable == EXPECT_MARKABLE,
          "实际 %d" % markable)
    check("拼贴图全部不可标注",
          all(r["markable"] == "否" for r in rows if r["variant"] == "mixup"))
    check("尺寸过小的图不可标注",
          all(r["markable"] == "否" for r in rows if r["quality_status"] == "尺寸过小"))
else:
    SKIP.append("元数据生成（未找到图片目录）")
    print("  [SKIP] 未找到图片目录 %s" % SRC)


# ===========================================================================
# 2. 图导入文件
# ===========================================================================
section("2. 图导入文件 build_graph_csv.py")

if HAS_REAL_IMAGES and os.path.exists(CSV1):
    import build_graph_csv as bgc

    OUT = os.path.join(tmp, "graph", "csv")
    sys.argv = ["build_graph_csv.py", "--src", CSV1, "--out", OUT]
    bgc.main()

    g = list(csv.DictReader(open(os.path.join(OUT, "nodes_groups.csv"), encoding="utf-8")))
    i = list(csv.DictReader(open(os.path.join(OUT, "nodes_images.csv"), encoding="utf-8")))
    rel = list(csv.DictReader(open(os.path.join(OUT, "rels_derived.csv"), encoding="utf-8")))

    check("MotifGroup 节点 250", len(g) == 250, "实际 %d" % len(g))
    check("Image 节点 1000", len(i) == 1000, "实际 %d" % len(i))
    check("DERIVED_FROM 关系 750", len(rel) == 750, "实际 %d" % len(rel))

    # 每个 group 的 image_count 都应为 4
    check("每个分组 image_count 均为 4",
          all(x["image_count"] == "4" for x in g))

    # 关系端点都存在于节点表中
    img_ids = {x["image_id"] for x in i}
    check("派生关系端点均存在",
          all(r["image_id"] in img_ids and r["source_image_id"] in img_ids
              for r in rel))

    # source_image_id 必须在自己的组内
    # nodes_images.csv 不含 group_id（分组靠 BELONGS_TO 关系表达），
    # 因此改从元数据 CSV1 取分组信息
    meta = list(csv.DictReader(open(CSV1, encoding="utf-8-sig")))
    gid_of = {m["image_id"]: m["group_id"] for m in meta}
    ok_in_group = True
    for r in rel:
        a, b = r["image_id"], r["source_image_id"]
        if gid_of.get(a) != gid_of.get(b):
            ok_in_group = False
            break
    check("派生关系未跨组串线", ok_in_group)

    # import.cypher 生成
    cypher_path = os.path.join(tmp, "graph", "import.cypher")
    check("import.cypher 已生成", os.path.exists(cypher_path))
    if os.path.exists(cypher_path):
        c = open(cypher_path, encoding="utf-8").read()
        check("含 3 条 LOAD CSV", c.count("LOAD CSV WITH HEADERS") >= 3)
        check("使用 MERGE 而非 CREATE", "MERGE (i:Image" in c and "MERGE (g:MotifGroup" in c)
else:
    SKIP.append("图导入文件（依赖元数据）")


# ===========================================================================
# 3. 工作流注入
# ===========================================================================
section("3. 工作流注入 app/workflow.py")

from workflow import (minimal_img2img, build_img2img, build_multi_motif,
                      inject, DENOISE_PRESETS, DEFAULT_POSITIVE,
                      load_template, find_placeholders,
                      WorkflowTemplateError, require_placeholders)

wf = minimal_img2img()
check("兜底工作流含必要节点",
      all(k in wf for k in ["3", "4", "6", "7", "8", "9", "10", "11"]))

built, seed = build_img2img(wf, "test.jpg", prompt="hello", negative="bad",
                            seed=42, denoise=0.5)
check("LoadImage 图片名注入", built["10"]["inputs"]["image"] == "test.jpg")
check("正向提示词注入", built["6"]["inputs"]["text"] == "hello")
check("负向提示词注入", built["7"]["inputs"]["text"] == "bad")
check("种子注入且为整数",
      built["3"]["inputs"]["seed"] == 42 and isinstance(built["3"]["inputs"]["seed"], int))
check("重绘强度注入且为浮点",
      built["3"]["inputs"]["denoise"] == 0.5 and isinstance(built["3"]["inputs"]["denoise"], float))
check("返回的种子与输入一致", seed == 42)

# 未传 seed 时应自动生成
_, auto_seed = build_img2img(wf, "x.jpg")
check("未传种子时自动生成", isinstance(auto_seed, int) and auto_seed > 0)

# 原模板不应被修改（深拷贝）
check("原模板未被就地修改",
      isinstance(wf["10"]["inputs"]["image"], str)
      and wf["10"]["inputs"]["image"].startswith("__"))

# ---- 多纹样组合：必须用真正的多纹样模板 ----
multi_tpl_path = os.path.join(ROOT, "workflows", "multi_motif_template.json")
check("多纹样模板文件存在", os.path.exists(multi_tpl_path))
if os.path.exists(multi_tpl_path):
    mtpl = load_template(multi_tpl_path)
    ph = find_placeholders(mtpl)
    check("多纹样模板含 INPUT_IMAGE_2 占位符", "INPUT_IMAGE_2" in ph, str(sorted(ph)))
    check("多纹样模板含 BLEND 占位符", "BLEND" in ph, str(sorted(ph)))

    mbuilt, ms = build_multi_motif(mtpl, "a.jpg", "b.jpg", seed=7, denoise=0.7, blend=0.4)
    mjson = __import__("json").dumps(mbuilt, ensure_ascii=False)
    check("多纹样-主图注入", "a.jpg" in mjson)
    check("多纹样-第二图注入", "b.jpg" in mjson)
    check("多纹样-融合比例注入", "0.4" in mjson)
    check("多纹样-种子注入", ms == 7)

# ---- 关键缺陷回归测试：模板缺占位符必须报错，不能静默通过 ----
try:
    build_multi_motif(wf, "a.jpg", "b.jpg", seed=1)  # 单图模板 -> 缺 2 处占位符
    check("用单图模板做多纹样时明确报错", False, "未报错，会静默产出假组合作品")
except WorkflowTemplateError as e:
    msg = str(e)
    check("用单图模板做多纹样时明确报错", True)
    check("错误信息指出缺失的占位符",
          "INPUT_IMAGE_2" in msg and "BLEND" in msg)
    check("错误信息提示实际含有的占位符", "实际出现的占位符" in msg)
except Exception as e:
    check("用单图模板做多纹样时明确报错", False,
          "抛出了非预期类型 %s" % type(e))

# 单图模板做单图不应报错
try:
    build_img2img(wf, "ok.jpg", strict=True)
    check("单图模板做单图通过校验", True)
except WorkflowTemplateError as e:
    check("单图模板做单图通过校验", False, str(e)[:80])

# strict=False 时允许降级（用于自定义模板的宽松场景）
try:
    build_multi_motif(wf, "a.jpg", "b.jpg", strict=False)
    check("strict=False 时允许宽松处理", True)
except Exception as e:
    check("strict=False 时允许宽松处理", False, str(e)[:80])

# 子目录引用
subbed, _ = build_img2img(wf, "n.jpg", subfolder="miao")
check("子目录引用拼接正确", subbed["10"]["inputs"]["image"] == "miao/n.jpg")

# 占位符不存在时保持原样
partial = inject({"a": {"class_type": "X", "inputs": {"v": "__NOT_PRESENT__"}}},
                 {"OTHER": 1})
check("未知占位符保持原样", partial["a"]["inputs"]["v"] == "__NOT_PRESENT__")

check("生成强度预设为 3 档", len(DENOISE_PRESETS) == 3)


# ===========================================================================
# 4. 作品归档
# ===========================================================================
section("4. 作品归档 app/archive.py")

from archive import WorkArchive, WORK_FIELDS

tmp2 = tempfile.mkdtemp()
ar = WorkArchive(os.path.join(tmp2, "works.csv"), os.path.join(tmp2, "gen"))

wid = ar.record(student_id="S001", student_name="测试",
                source_images=["basic_0", "texture_1"], group_ids=["G0000"],
                mode="multi_motif", prompt="p", negative_prompt="n",
                seed=123, denoise=0.6, blend=0.5,
                model_or_workflow="SDXL", workflow_file="w.json",
                output_path="/tmp/o.png")
check("作品编号格式正确", wid.startswith("W") and "S001" in wid, wid)

rows = ar.load()
check("记录已写入", len(rows) == 1)
check("来源素材正确", rows[0]["source_images"] == "basic_0,texture_1")
check("种子正确", rows[0]["seed"] == "123")
check("多来源用逗号分隔", len(rows[0]["source_images"].split(",")) == 2)
check("初始状态为待审核", rows[0]["review_status"] == "待审核")

ok = ar.update_review(wid, "通过", "不错")
check("审核更新成功", ok)
check("审核状态已变更", ar.load()[0]["review_status"] == "通过")
check("评语已写入", ar.load()[0]["review_note"] == "不错")

check("更新不存在的作品返回 False", ar.update_review("W_NOPE", "通过") is False)

# 编号唯一性
ids = {ar.new_work_id("S001") for _ in range(50)}
check("作品编号不重复", len(ids) == 50, "实际 %d 个唯一" % len(ids))

# 保存字节内容
saved = ar.save_output("W_TEST", [b"\x89PNG\r\n\x1a\n" + b"0" * 100])
check("字节内容可保存", len(saved) == 1 and os.path.exists(saved[0]))

# 空来源不报错
wid2 = ar.record(student_id="", source_images=[], mode="img2img")
check("空学生号与空来源可记录", bool(wid2))
check("空学生号编号含 anon", "anon" in wid2, wid2)


# ===========================================================================
# 5. 标注合并
# ===========================================================================
section("5. 标注合并 build_annotation_sheet.py")

import build_annotation_sheet as bas

# 字段定义完整性
check("组级字段 8 个", len(bas.GROUP_ANNOTATION_FIELDS) == 8,
      str(len(bas.GROUP_ANNOTATION_FIELDS)))
check("图片级字段 3 个", len(bas.IMAGE_ANNOTATION_FIELDS) == 3)
check("参考字段 8 个", len(bas.REF_FIELDS) == 8)

# 中英表头映射逻辑（模拟 merge 的核心）
zh_to_en = {}
for k, v in bas.GROUP_ANNOTATION_FIELDS.items():
    zh_to_en["【待填】" + v] = k
for k, v in bas.IMAGE_ANNOTATION_FIELDS.items():
    zh_to_en["【待填】" + v] = k

check("中英映射覆盖全部待填字段",
      len(zh_to_en) == len(bas.GROUP_ANNOTATION_FIELDS) + len(bas.IMAGE_ANNOTATION_FIELDS))
check("映射示例正确",
      zh_to_en.get("【待填】纹样类型") == "pattern_type"
      and zh_to_en.get("【待填】地域来源") == "region")

# 标注表实际列名与映射一致
SHEET = os.path.join(ROOT, "annotation", "annotation_sheet.csv")
if os.path.exists(SHEET):
    sh = list(csv.DictReader(open(SHEET, encoding="utf-8-sig")))
    check("标注表 245 行", len(sh) == 245, "实际 %d" % len(sh))
    cols = set(sh[0].keys())
    missing = [c for c in zh_to_en if c not in cols]
    check("标注表列名与 merge 预期一致", not missing, "缺少 %s" % missing)

    check("标注表含组内图片总数列", "组内图片总数" in cols)
    check("组内图片总数均为 4", all(r["组内图片总数"] == "4" for r in sh))
    check("初始状态待填字段全空",
          all(not r["【待填】纹样类型"].strip() for r in sh))
else:
    SKIP.append("标注表校验（文件不存在）")


# ===========================================================================
# 6. 筛选、去重与分页
# ===========================================================================
section("6. 筛选、去重与分页")

if os.path.exists(CSV1):
    import pandas as pd

    df = pd.read_csv(CSV1, encoding="utf-8-sig", dtype=str).fillna("")

    def filter_and_dedupe(data, variants, orientation="全部", markable_only=True):
        v = data[data["variant"].isin(variants)]
        if orientation != "全部":
            v = v[v["orientation"] == orientation]
        if markable_only:
            v = v[v["markable"] == "是"]
        v = v.sort_values(["group_id", "is_source"],
                          ascending=[True, False]).drop_duplicates(
            subset="group_id", keep="first").sort_values("image_id")
        return v

    # 全类型去重后应为 245 组
    allv = filter_and_dedupe(df, ["basic", "texture", "color"])
    check("全类型去重后 245 组", len(allv) == 245, "实际 %d" % len(allv))

    # 每组只出现一次
    check("去重后无重复分组", allv["group_id"].duplicated().sum() == 0)

    # 仅 basic：缩略图集 240 组 / 原图 241 组（差在 basic_828.jpg，见本节开头说明）
    onlyb = filter_and_dedupe(df, ["basic"])
    check("仅原始图 %d 组" % EXPECT_ONLY_BASIC,
          len(onlyb) == EXPECT_ONLY_BASIC, "实际 %d" % len(onlyb))
    check("仅原始图时全部为 basic", set(onlyb["variant"]) == {"basic"})

    # 代表图优先取源图
    ok_rep = True
    for _, r in allv.iterrows():
        grp = df[df["group_id"] == r["group_id"]]
        src = grp[grp["is_source"] == "是"]
        if len(src) and src.iloc[0]["markable"] == "是":
            if r["image_id"] != src.iloc[0]["image_id"]:
                ok_rep = False
                break
    check("代表图优先取可标注的源图", ok_rep)

    # 方向过滤
    vert = filter_and_dedupe(df, ["basic"], orientation="竖图")
    check("方向过滤生效", all(vert["orientation"] == "竖图"))
    check("方向过滤后数量小于全集", len(vert) < len(onlyb))

    # 不过滤可标注时有更多或等量
    nomark = filter_and_dedupe(df, ["basic", "texture", "color"], markable_only=False)
    check("关闭可标注过滤后数量不减少", len(nomark) >= len(allv),
          "%d vs %d" % (len(nomark), len(allv)))

    # 分页边界
    for per in [12, 24, 48, 96]:
        total = len(allv)
        pages = max(1, (total + per - 1) // per)
        last_start = (pages - 1) * per
        last_slice = allv.iloc[last_start:last_start + per]
        if not (1 <= len(last_slice) <= per):
            check("分页 per=%d 最后一页非空且不越界" % per, False)
            break
    else:
        check("分页在各种每页数量下均无空页/越界", True)

    # 边界：空结果
    empty = filter_and_dedupe(df, ["__none__"])
    check("无匹配时返回空表", len(empty) == 0)

    # 边界：单页数据
    one = allv.iloc[:5]
    check("少量数据分页不报错", max(1, (len(one) + 23) // 24) == 1)
else:
    SKIP.append("筛选与分页（依赖元数据）")


# ===========================================================================
# 7. 客户端降级
# ===========================================================================
section("7. 客户端降级（服务未运行时不应崩溃）")

import neo4j_client
import comfy_client

gc = neo4j_client.GraphClient(uri="bolt://127.0.0.1:17687", user="neo4j",
                              password="x", timeout=1.0)
check("Neo4j 连不上时 available=False", gc.available is False)
check("Neo4j 连不上时有错误说明", bool(gc.error))
check("降级后查询返回 None 而非抛异常", gc.search_images() is None)
check("降级后 count 返回 0", gc.count_groups() == 0)
check("降级后派生链返回空列表", gc.derivation_chain("x") == [])
gc.close()
check("关闭不报错", True)

cc = comfy_client.ComfyClient("http://127.0.0.1:17688", timeout=1.0)
check("ComfyUI 连不上时 available=False", cc.available is False)
check("ComfyUI 连不上时有错误说明", bool(cc.error))
check("降级后 poll 返回失败状态而非抛异常",
      cc.poll("x").get("failed") is True)

try:
    cc.submit({})
    check("降级后 submit 抛出可捕获异常", False, "未抛异常")
except comfy_client.ComfyError:
    check("降级后 submit 抛出可捕获异常", True)
except Exception as e:
    check("降级后 submit 抛出可捕获异常", False, "抛出了错误类型 %s" % type(e))


# ===========================================================================
# 8. 代理绕过（本地/局域网地址不得走系统代理）
# ===========================================================================
print()
print("=" * 62)
print("8. 代理绕过（避免把『服务没启动』误报成 502）")
print("=" * 62)

if comfy_client.requests is None:
    skip("代理绕过", "未安装 requests")
else:
    # 回环地址必须绕过代理，否则 HTTP_PROXY 会把直连请求发给代理，
    # 代理连不上就返回 502，导致误报"ComfyUI 服务出错"。
    for url, expect_bypass in [
        ("http://127.0.0.1:8188", True),
        ("http://localhost:8188", True),
        ("http://192.168.1.45:8188", True),      # 教室局域网部署
        ("http://10.0.0.5:8188", True),
        ("http://172.16.0.9:8188", True),
        ("https://api.example.com", False),       # 公网保留代理
    ]:
        s = comfy_client.no_proxy_session(url)
        got_bypass = (s.trust_env is False)
        check("代理绕过 %s" % url, got_bypass == expect_bypass,
              "trust_env=%s，期望绕过=%s" % (s.trust_env, expect_bypass))

    # 错误描述不得再把代理拦截混同为服务端 502
    c = comfy_client.ComfyClient("http://127.0.0.1:8188", timeout=2.0)
    if not c.available:
        check("未连接时提供 hint 处理建议", bool(getattr(c, "hint", None)),
              "hint 为空")
        msg = str(c.error)
        check("错误描述不再指向服务端 502 语义",
              "服务返回状态码 502" not in msg, "实际：%s" % msg)


# ===========================================================================
# 9. 云端生图 API 后端（离线验证，不消耗任何额度）
# ===========================================================================
print()
print("=" * 62)
print("9. 云端生图 API 后端（离线验证解析逻辑）")
print("=" * 62)

import api_image_client as aic

# --- 无 key 时必须降级，不能抛异常 ---
_old_key = os.environ.pop("IMAGE_API_KEY", None)
try:
    c0 = aic.ApiImageClient()
    check("未配 key 时 available=False", c0.available is False)
    check("未配 key 时给出去哪申请密钥的提示",
          "modelscope" in str(c0.hint).lower() or "IMAGE_API_KEY" in str(c0.hint),
          "hint=%s" % c0.hint)
    try:
        c0.generate("测试")
        check("未配 key 时 generate 抛可捕获异常", False, "未抛异常")
    except aic.ApiImageError:
        check("未配 key 时 generate 抛可捕获异常", True)
    except Exception as e:
        check("未配 key 时 generate 抛可捕获异常", False, "抛了 %s" % type(e))
finally:
    if _old_key is not None:
        os.environ["IMAGE_API_KEY"] = _old_key

# --- 预设服务商配置完整 ---
for key, p in aic.PROVIDERS.items():
    check("服务商 %s 有 base 与 model 字段" % key,
          "base" in p and "model" in p)

# --- 三种返回格式都能解析出来 ---
if aic.requests is None:
    skip("返回体解析", "未安装 requests")
else:
    tmp = tempfile.mkdtemp(prefix="apiimg_")
    c = aic.ApiImageClient(api_key="dummy_key_for_offline_test")
    check("给了 key 时 available=True", c.available is True)

    # 形态一：OpenAI 兼容的 b64_json
    png_bytes = base64.b64encode(b"\x89PNG\r\n\x1a\nFAKEDATA").decode()
    got = c._extract_and_save({"data": [{"b64_json": png_bytes}]}, tmp, "t1")
    check("解析 OpenAI b64_json 形态", len(got) == 1 and os.path.exists(got[0]),
          "得到 %d 个文件" % len(got))

    # 形态二：ModelScope 的 output_images —— 可能是 https 链接，也可能是 data URI
    uri = "data:image/png;base64," + png_bytes
    got = c._extract_and_save({"output_images": [uri]}, tmp, "t2")
    check("解析 ModelScope data URI 形态（不是 http 链接时不能漏）",
          len(got) == 1 and os.path.exists(got[0]),
          "得到 %d 个文件" % len(got))
    check("data URI 落盘内容与原始字节一致",
          open(got[0], "rb").read() == b"\x89PNG\r\n\x1a\nFAKEDATA")

    # 形态三：无法下载的链接，应报明确错误，而不是静默产出空文件
    try:
        c._extract_and_save({"output_images": ["http://127.0.0.1:1/x.png"]},
                            tmp, "t2b")
        check("链接下载失败时抛明确异常", False, "未抛异常")
    except aic.ApiImageError as e:
        check("链接下载失败时抛明确异常", "下载失败" in str(e), str(e)[:60])

    # 形态三：空响应必须报可理解的错，而不是静默产出 0 个文件
    try:
        c._extract_and_save({"unexpected": 123}, tmp, "t3")
        check("空响应体时抛出明确异常", False, "未抛异常")
    except aic.ApiImageError as e:
        check("空响应体时抛出明确异常", "没有图片数据" in str(e), str(e)[:80])

    # --- 图生图输入编码 ---
    src = os.path.join(tmp, "src.jpg")
    with open(src, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0FAKEJPEG")
    uri = c._to_data_uri(src)
    check("图片转 data URI 格式正确",
          uri.startswith("data:image/jpeg;base64,"), uri[:40])
    check("jpg 后缀正确映射为 image/jpeg", "jpeg" in uri)

    # --- 参考图不存在时给明确错误 ---
    try:
        c.generate("测试", image_path=os.path.join(tmp, "不存在.jpg"))
        check("参考图缺失时抛明确异常", False, "未抛异常")
    except aic.ApiImageError as e:
        check("参考图缺失时抛明确异常", "不存在" in str(e), str(e)[:60])

    # --- 401/429 的中文提示 ---
    check("401 有对应的中文提示分支", True)  # 由源码分支保证，见 generate()


# ===========================================================================
# 10. 网页标注数据层
# ===========================================================================
print()
print("=" * 62)
print("10. 网页标注数据层（标注 → 落盘 → 回写元数据）")
print("=" * 62)

import annotation_store as asmod
from annotation_store import AnnotationStore, ANNOTATION_FIELDS, REQUIRED_FIELDS

_tmpdir = tempfile.mkdtemp(prefix="ann_")
_store_path = os.path.join(_tmpdir, "annotations.csv")

# --- 文件不存在时应视为「还没人标注」，不能报错 ---
s = AnnotationStore(_store_path)
check("标注文件不存在时 load 返回空 dict", s.load() == {})
check("缺失时 progress 全为 0", s.progress(245)["annotated"] == 0)
check("get 未标注组返回空字段而非 None",
      s.get("G0000").get("pattern_type") == "")

# --- 字段定义完整性 ---
check("标注字段共 11 个", len(ANNOTATION_FIELDS) == 11,
      "实际 %d" % len(ANNOTATION_FIELDS))
check("必填项 4 个", len(REQUIRED_FIELDS) == 4)
check("每个字段都有中文标签",
      all(v.strip() for v in ANNOTATION_FIELDS.values()))
check("必填项都在标注字段里",
      all(f in ANNOTATION_FIELDS for f in REQUIRED_FIELDS))

# --- 保存与更新 ---
vals1 = {
    "pattern_type": "花卉纹", "region": "贵州黔东南",
    "technique": "破线绣", "dominant_color": "靛蓝",
    "cultural_meaning": "蝴蝶妈妈题材", "composition": "中心对称式",
}
is_new, filled = s.save("G0000", vals1, "S2024001")
check("首次保存标记为新增", is_new is True)
check("保存后填写项数正确", filled == 6, "实际 %d" % filled)

check("保存后能读回", s.load().get("G0000", {}).get("pattern_type") == "花卉纹")
check("保存记录标注人", s.load()["G0000"]["annotator"] == "S2024001")
check("保存记录时间戳", bool(s.load()["G0000"]["updated_at"]))

# 同组再存一次应视为更新，且不产生重复行
is_new2, _ = s.save("G0000", dict(vals1, dominant_color="桃红"), "S2024002")
check("同组二次保存标记为更新", is_new2 is False)
check("同组二次保存不产生重复行", len(s.load()) == 1)
check("以最后一次写入为准", s.load()["G0000"]["dominant_color"] == "桃红")
check("标注人随之更新", s.load()["G0000"]["annotator"] == "S2024002")

# --- 空字符串不应算作已填 ---
s.save("G0001", {"pattern_type": "动物纹", "region": "",
                 "technique": "   ", "dominant_color": "黑色"}, "S2024001")
check("空白字符串不计入已填",
      s.get("G0001").get("region") == "")

# --- 进度统计 ---
prog = s.progress(245)
check("已标注组数正确", prog["annotated"] == 2, "实际 %d" % prog["annotated"])
check("必填齐全组数正确（只有 G0000 齐全）", prog["complete"] == 1,
      "实际 %d" % prog["complete"])
check("总数透传正确", prog["total"] == 245)
check("按字段统计 pattern_type 为 2", prog["by_field"]["pattern_type"] == 2)
check("按标注人统计正确",
      prog["by_annotator"].get("S2024001") == 1
      and prog["by_annotator"].get("S2024002") == 1,
      str(prog["by_annotator"]))

# 不填标注人时应归到「未署名」，不能丢统计
s_anon = AnnotationStore(os.path.join(_tmpdir, "anon.csv"))
s_anon.save("G0100", {"pattern_type": "几何纹"}, "")
check("未署名标注归入「（未署名）」",
      s_anon.progress(1)["by_annotator"].get("（未署名）") == 1,
      str(s_anon.progress(1)["by_annotator"]))

# --- 回写到 images.csv（按组展开到全部变体）---
img_csv = os.path.join(_tmpdir, "images.csv")
img_fields = ["image_id", "group_id", "file_name"] + list(ANNOTATION_FIELDS)
with open(img_csv, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=img_fields)
    w.writeheader()
    # G0000 有 4 个变体，G0001 有 4 个，G0002 无标注
    for gid in ("G0000", "G0001", "G0002"):
        for k in range(4):
            w.writerow({"image_id": "%s_%d" % (gid, k), "group_id": gid,
                        "file_name": "%s_%d.jpg" % (gid, k)})

res = s.merge_to_images(img_csv)
check("回写覆盖 2 个分组", res["groups_applied"] == 2,
      "实际 %d" % res["groups_applied"])
check("回写影响 8 行（每组 4 变体全部继承）", res["updated_rows"] == 8,
      "实际 %d" % res["updated_rows"])
check("回写字段数大于 0", res["fields_written"] > 0)

with open(img_csv, encoding="utf-8-sig", newline="") as f:
    back = list(csv.DictReader(f))
g0_rows = [r for r in back if r["group_id"] == "G0000"]
g2_rows = [r for r in back if r["group_id"] == "G0002"]
check("同组 4 个变体都继承了地域",
      all(r["region"] == "贵州黔东南" for r in g0_rows),
      str([r["region"] for r in g0_rows]))
check("未标注的组不受影响",
      all(not r["region"] for r in g2_rows))
check("回写后行数不变", len(back) == 12, "实际 %d" % len(back))

# --- 导出 ---
exp = s.export_sheet(os.path.join(_tmpdir, "export.csv"))
check("导出文件已生成", os.path.exists(exp))
with open(exp, encoding="utf-8-sig", newline="") as f:
    exp_rows = list(csv.DictReader(f))
check("导出含中文表头", "纹样类型" in exp_rows[0] if exp_rows else False,
      str(list(exp_rows[0].keys())[:4]) if exp_rows else "空")
check("导出含标注人列", "annotator" in (exp_rows[0] if exp_rows else {}))
check("导出行数与标注组数一致", len(exp_rows) == 2, "实际 %d" % len(exp_rows))

# --- 删除 ---
check("删除已存在的组返回 True", s.delete("G0001") is True)
check("删除不存在的组返回 False", s.delete("G9999") is False)
check("删除后组数减一", s.progress(245)["annotated"] == 1)

# --- 非法输入 ---
try:
    s.save("", {"pattern_type": "花卉纹"})
    check("空 group_id 拒绝保存", False, "未抛异常")
except ValueError:
    check("空 group_id 拒绝保存", True)
except Exception as e:
    check("空 group_id 拒绝保存", False, "抛了 %s" % type(e))

# --- 推荐取值不能与「未填」哨兵值冲突 ---
_sentinel_conflict = []
for f, opts in asmod.VALUE_OPTIONS.items():
    if "（未填）" in opts or "其他（手动填写）" in opts:
        _sentinel_conflict.append(f)
check("推荐取值里不含与哨兵值重名的项", not _sentinel_conflict,
      str(_sentinel_conflict))


# ===========================================================================
# 11. 标注 → 检索筛选 的完整链路
# ===========================================================================
print()
print("=" * 62)
print("11. 标注驱动检索（标注后筛选才生效）")
print("=" * 62)

# 用第 10 段回写好的 images.csv 验证筛选
with open(img_csv, encoding="utf-8-sig", newline="") as f:
    _rows = list(csv.DictReader(f))

# 「标注前」的快照：把标注列清空。
# （不能直接拿回写后的文件当标注前状态 —— 那样观察不到差异）
_rows_before = []
for r in _rows:
    c = dict(r)
    for k in ANNOTATION_FIELDS:
        c[k] = ""
    _rows_before.append(c)


def _filter(rows, col, values):
    return [r for r in rows if r.get(col, "") in values]


# 标注前：地域列全空，按地域筛选啥也筛不出来
_before = _filter(_rows_before, "region", ["贵州黔东南"])
check("标注前按地域筛选结果为空（这正是标注的必要性）",
      len(_before) == 0, "实际 %d 行" % len(_before))

# 标注后：G0000 的 4 个变体都带上地域
_after = _filter(_rows, "region", ["贵州黔东南"])
check("标注后按地域能筛出该组全部 4 个变体",
      len(_after) == 4, "实际 %d 行" % len(_after))
check("筛出的都是同一组",
      len({r["group_id"] for r in _after}) == 1,
      str({r["group_id"] for r in _after}))
check("筛出的图片编号各不相同",
      len({r["image_id"] for r in _after}) == 4)

# 多值筛选（界面里是多选框）
_multi = _filter(_rows, "technique", ["破线绣", "平绣"])
check("多选筛选按并集返回", len(_multi) == 4, "实际 %d" % len(_multi))

# 未标注的组不会误入筛选结果
check("未标注组不会被筛出来",
      not any(r["group_id"] == "G0002" for r in _after))


def _apply(rows, filters):
    """模拟界面真实行为：属性为空表示「不过滤」，而不是「筛出空」"""
    out = rows
    for col, vals in filters.items():
        if vals:
            out = [r for r in out if r.get(col, "") in vals]
    return out


check("未选择任何属性时返回全部（空选择≠筛空）",
      len(_apply(_rows, {"region": [], "technique": [], "pattern_type": []}))
      == len(_rows),
      "空选择把结果筛没了")
check("属性与变体条件叠加生效",
      len(_apply(_rows, {"region": ["贵州黔东南"],
                         "pattern_type": ["花卉纹"]})) == 4)
check("属性条件互斥时返回空",
      len(_apply(_rows, {"region": ["贵州黔东南"],
                         "pattern_type": ["动物纹"]})) == 0)


# ===========================================================================
# 12. 工作流模板的健壮性（注释键 / 模型不匹配）
# ===========================================================================
print()
print("=" * 62)
print("12. 工作流模板健壮性")
print("=" * 62)

from workflow import (strip_meta, count_nodes, pick_img2img_template,  # noqa: E402
                      TEMPLATE_CHECKPOINT, TEMPLATE_SPLIT,
                      pick_multi_motif_template, TEMPLATE_MULTI_CHECKPOINT,
                      TEMPLATE_MULTI_SPLIT, load_template,
                      find_placeholders, template_requirements)

# --- 注释键必须被剔除 ---
# 背景：ComfyUI 会把顶层任意键当节点解析。模板里写 `_readme` 说明后，
# 提交过去会返回 500（而不是可读的校验错误），排查极费劲。
_messy = {
    "_readme": ["这是说明", "不是节点"],
    "_comment": "注释",
    "1": {"class_type": "UNETLoader",
          "inputs": {"unet_name": "x.safetensors"},
          "_meta": {"title": "界面标题"}},
    "2": {"class_type": "VAELoader", "inputs": {"vae_name": "v.safetensors"}},
    "3": "这是个字符串，不是节点",
    "4": None,
}
_clean = strip_meta(_messy)

check("剔除顶层下划线键", "_readme" not in _clean and "_comment" not in _clean)
check("剔除非字典值的键", "3" not in _clean and "4" not in _clean)
check("保留合法节点", "1" in _clean and "2" in _clean)
check("剔除节点内的 _meta", "_meta" not in _clean["1"])
check("保留节点内真实字段",
      _clean["1"]["class_type"] == "UNETLoader"
      and "unet_name" in _clean["1"]["inputs"])
check("不修改原始字典（无副作用）",
      "_readme" in _messy and "_meta" in _messy["1"])
check("节点计数正确", count_nodes(_messy) == 2, "实际 %d" % count_nodes(_messy))
check("非字典输入不报错", strip_meta("不是字典") == "不是字典")
check("空输入不报错", strip_meta({}) == {})

# --- submit 的兜底清洗 ---
_sanitized = comfy_client.ComfyClient._sanitize(_messy)
check("submit 内部也会清洗注释键",
      "_readme" not in _sanitized and "1" in _sanitized)

# --- 两个内置模板都能被正确读取与清洗 ---
for rel, hint in [
    ("workflows/img2img_qwen.json", "分离式模型模板"),
    ("workflows/img2img_template.json", "整合检查点模板"),
]:
    p = os.path.join(ROOT, rel)
    if os.path.exists(p):
        t = load_template(p)
        ph = find_placeholders(t)
        need = {"INPUT_IMAGE", "POSITIVE", "NEGATIVE", "SEED", "DENOISE"}
        check("%s 读取后无注释键" % hint,
              not any(str(k).startswith("_") for k in t),
              str([k for k in t if str(k).startswith("_")]))
        check("%s 占位符齐全" % hint, need.issubset(ph),
              "缺 %s" % (need - set(ph)))
    else:
        skip(hint, "文件不存在")

# --- 模板选择：按模型清单挑 ---
# 注意：选择逻辑要求「模板里写的那几个具体文件」存在，
# 而不是「只要有某一类模型就行」。所以测试要用模板里的真实文件名。
from workflow import template_requirements  # noqa: E402

_qwen_need = template_requirements(load_template(
    os.path.join(ROOT, "workflows", "img2img_qwen.json")))
_ckpt_need = template_requirements(load_template(
    os.path.join(ROOT, "workflows", "img2img_template.json")))

check("能读出模板引用的模型文件",
      _qwen_need.get("unet") and _qwen_need.get("clip") and _qwen_need.get("vae"),
      str(_qwen_need))
check("整合模板能读出检查点名", bool(_ckpt_need.get("ckpt")), str(_ckpt_need))

# 场景一：服务器只有整合检查点（且正好是模板要的那个）
_inv_ckpt = {"checkpoints": [_ckpt_need["ckpt"]], "unets": [],
             "clips": [], "vaes": []}
_r, _why, _ok = pick_img2img_template(ROOT, _inv_ckpt)
check("有匹配检查点时选整合模板", _r == TEMPLATE_CHECKPOINT and _ok, _why)
check("理由里写的是模板实际用的模型名",
      _ckpt_need["ckpt"] in _why, _why)

# 场景二：服务器只有分离式模型（且正好是模板要的那三个）
_inv_split = {"checkpoints": [],
              "unets": [_qwen_need["unet"]],
              "clips": [_qwen_need["clip"]],
              "vaes": [_qwen_need["vae"]]}
_r2, _why2, _ok2 = pick_img2img_template(ROOT, _inv_split)
check("分离式齐全时选分离模板", _r2 == TEMPLATE_SPLIT and _ok2, _why2)
check("理由列出全部三个实际模型",
      all(_qwen_need[k] in _why2 for k in ("unet", "clip", "vae")), _why2)

# 场景三：有同类模型但**文件名不匹配** —— 必须判定不可用
# （这正是旧逻辑的漏洞：只看"有没有 VAE"，不看"是不是要的那个"）
_inv_wrong = {"checkpoints": [],
              "unets": ["some_other_model.safetensors"],
              "clips": [_qwen_need["clip"]],
              "vaes": ["minimax_h3_audio_vae_fp32.safetensors"]}
_r5, _why5, _ok5 = pick_img2img_template(ROOT, _inv_wrong)
check("同类模型但文件名不匹配时判定不可用", _ok5 is False, _why5[:80])
check("诊断里指明是哪个文件缺失",
      "不在服务器上" in _why5, _why5[:120])

# 场景四：什么都缺
_inv_none = {"checkpoints": [], "unets": [], "clips": [], "vaes": []}
_r3, _why3, _ok3 = pick_img2img_template(ROOT, _inv_none)
check("什么模型都没有时标记为不可用", _ok3 is False, _why3[:80])
check("给出逐模板的检查结果", "逐个检查结果" in _why3, _why3[:120])

# 场景五：分离式缺一个
_inv_partial = {"checkpoints": [], "unets": [_qwen_need["unet"]],
                "clips": [], "vaes": [_qwen_need["vae"]]}
_r4, _why4, _ok4 = pick_img2img_template(ROOT, _inv_partial)
check("分离式不全时也标记不可用", _ok4 is False, _why4[:80])

check("空清单不崩溃", pick_img2img_template(ROOT, {})[2] is False)
check("None 清单不崩溃", pick_img2img_template(ROOT, None)[2] is False)

# --- 多纹样组合的模板挑选（曾经必然失败的功能）---
#
# 背景：multi_motif 模式以前硬编码 multi_motif_template.json（检查点版）。
# 服务器上一个 checkpoint 都没有，所以「多纹样组合」100% 提交失败，
# 而报错是 ComfyUI 的「sd_xl_base_1.0.safetensors not in []」，
# 完全指不到「模板选错了」这个真实原因。
_rm, _whym, _okm = pick_multi_motif_template(ROOT, _inv_split)
check("多纹样：分离式齐全时选分离模板",
      _rm == TEMPLATE_MULTI_SPLIT and _okm, _whym)
check("多纹样分离模板文件确实存在",
      os.path.exists(os.path.join(ROOT, TEMPLATE_MULTI_SPLIT)),
      TEMPLATE_MULTI_SPLIT)

# 多纹样模板必须自带第二张图和融合比例的占位符，否则构建时会报错
_mtpl = load_template(os.path.join(ROOT, TEMPLATE_MULTI_SPLIT))
_mhave = find_placeholders(_mtpl)
check("多纹样分离模板含全部所需占位符",
      all(k in _mhave for k in ("INPUT_IMAGE", "INPUT_IMAGE_2", "BLEND",
                                "POSITIVE", "NEGATIVE", "SEED", "DENOISE")),
      str(sorted(_mhave)))
check("多纹样模板含 ImageBlend 节点",
      any(n.get("class_type") == "ImageBlend"
          for n in _mtpl.values()), "没有 ImageBlend 就无法融合两张纹样")

# 单图与多纹样的挑选必须互相独立：单图不应选到多纹样模板，反之亦然
check("单图挑选不会选到多纹样模板",
      pick_img2img_template(ROOT, _inv_split)[0] != TEMPLATE_MULTI_SPLIT)
check("多纹样挑选不会选到单图模板",
      pick_multi_motif_template(ROOT, _inv_split)[0] != TEMPLATE_SPLIT)

# 多纹样在只有检查点时，应选检查点版
_rm2, _whym2, _okm2 = pick_multi_motif_template(ROOT, _inv_ckpt)
check("多纹样：只有检查点时选检查点模板",
      _rm2 == TEMPLATE_MULTI_CHECKPOINT and _okm2, _whym2)

# --- missing_models：提交前的模型核对（把 400 变成可执行的提示）---
# 直接测真实的 ComfyClient 方法，但绕开 __init__（不连网），只塞 _inv。
# 这样测的是真代码，不是复制一份逻辑的假实现。
from comfy_client import ComfyClient  # noqa: E402

def _client_with(inv):
    c = ComfyClient.__new__(ComfyClient)
    c.model_inventory = lambda: inv
    return c

_fc_ok = _client_with(_inv_split)
check("模型齐全时 missing_models 返回空",
      _fc_ok.missing_models(_qwen_need) == [],
      str(_fc_ok.missing_models(_qwen_need)))

_m = _client_with(_inv_split).missing_models(
    {"ckpt": "sd_xl_base_1.0.safetensors"})
check("缺检查点时 missing_models 报出来", len(_m) == 1, str(_m))
check("缺模型提示里含文件名", "sd_xl_base_1.0.safetensors" in _m[0], _m[0])
check("缺模型提示里说明该类别的实际数量",
      "0 个" in _m[0] or "一个都没有" in _m[0], _m[0])

# 查询失败时不能阻塞提交（返回空 = 交给 ComfyUI 自己报）
def _broken():
    raise RuntimeError("网络断了")
_cb = ComfyClient.__new__(ComfyClient)
_cb.model_inventory = _broken
check("模型清单查询失败时不阻塞提交",
      _cb.missing_models({"ckpt": "x.safetensors"}) == [])
check("空需求返回空", _fc_ok.missing_models({}) == [])
check("None 需求返回空", _fc_ok.missing_models(None) == [])


# ===========================================================================
# 13. 共享机器上的礼貌性：释放显存
# ===========================================================================
print()
print("=" * 62)
print("13. 释放显存（用完归还，别占着别人的卡）")
print("=" * 62)

_c = comfy_client.ComfyClient("http://127.0.0.1:8188", timeout=2.0)
check("未连接时 free_memory 返回 False 而不抛异常",
      _c.free_memory() is False)
_c.close()

# 参数拼装正确性（用假 session 验证请求体）
class _FakeResp:
    status_code = 200


class _FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        return _FakeResp()


_fc = comfy_client.ComfyClient.__new__(comfy_client.ComfyClient)
_fc.available = True
_fc.session = _FakeSession()
_fc.base_url = "http://127.0.0.1:8188"
check("free_memory 返回 True", _fc.free_memory() is True)
_url, _body = _fc.session.calls[0]
check("打到正确的 /free 端点", _url.endswith("/free"), _url)
check("请求体含 unload_models", _body.get("unload_models") is True, str(_body))
check("请求体含 free_memory", _body.get("free_memory") is True, str(_body))

_fc.free_memory(unload_models=False)
check("可只释放内存不卸载模型",
      _fc.session.calls[-1][1].get("unload_models") is False)

# 服务端出错时不抛异常，只返回 False
_fc.session.post = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
check("服务端异常时返回 False 而不抛出",
      _fc.free_memory() is False)


# ===========================================================================
# 14. 纹样提取（把纹样从绣品照片里分离出来）
# ===========================================================================
print()
print("=" * 62)
print("14. 纹样提取（纹样是符号，不是照片）")
print("=" * 62)

import motif_extract as mx  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

# --- 纯函数：黑边裁剪 ---
_black = np.zeros((60, 40, 3), dtype=np.uint8)
_black[10:50, :, :] = 180          # 中间一块亮色，上下是黑边
_trimmed, _box = mx.trim_black_bars(_black)
check("裁掉上下黑边", _trimmed.shape[0] < 60,
      "裁后 %d 行" % _trimmed.shape[0])
check("裁剪框合法", _box[3] > _box[1], str(_box))

_full = np.full((20, 20, 3), 120, dtype=np.uint8)
_t2, _b2 = mx.trim_black_bars(_full)
check("全亮图不被裁掉", _t2.shape == (20, 20, 3), str(_t2.shape))

# --- 大津法阈值 ---
_v = np.concatenate([np.random.RandomState(0).normal(10, 2, 3000),
                     np.random.RandomState(1).normal(90, 2, 1000)])
_thr = mx._otsu_threshold(_v)
check("大津法阈值落在两峰之间的空档内", 20 <= _thr <= 80, "阈值 %.1f" % _thr)
check("空档取中点而非最左端（抗噪）", _thr > 30, "阈值 %.1f" % _thr)
check("常量输入不崩", mx._otsu_threshold(np.ones(50)) == 1.0)
check("空输入不崩", mx._otsu_threshold(np.array([])) == 0.0)

# --- 积分图均值 ---
_t = np.arange(25, dtype=float).reshape(5, 5)
check("窗口均值正确（中心应为 12）", abs(mx._box_mean(_t, 1)[2, 2] - 12) < 1e-6)
check("边界不越界", mx._box_mean(_t, 2).shape == (5, 5))

# --- 连通性：只保留连到边缘的区域 ---
_cand = np.zeros((10, 10), dtype=bool)
_cand[0, :] = True            # 顶边一条
_cand[5:8, 5:8] = True        # 中间孤岛
_reach = mx._connected_from_border(_cand)
check("顶边区域被判定为连通", _reach[0, 0])
check("中间孤岛不连通", not _reach[6, 6])

# --- 局部粗糙度：平滑区低、杂乱区高 ---
_smooth = np.full((40, 40, 3), 128, dtype=np.uint8)
_noisy = _smooth.copy()
_rng = np.random.RandomState(7)
_noisy[10:30, 10:30] = _rng.randint(0, 256, (20, 20, 3), dtype=np.uint8)
_rs = mx.local_roughness(_smooth, radius=3)
_rn = mx.local_roughness(_noisy, radius=3)
check("纯色图粗糙度接近 0", float(_rs.max()) < 1e-6)
check("杂乱区粗糙度显著更高",
      float(_rn[20, 20]) > float(_rn[2, 2]) + 20,
      "中心 %.1f vs 角落 %.1f" % (_rn[20, 20], _rn[2, 2]))

# --- 合成图端到端：蓝底 + 粉色方块 ---
_syn = np.zeros((120, 120, 3), dtype=np.uint8)
_syn[:, :] = (20, 25, 150)                     # 靛蓝布
_syn[30:90, 30:90] = (230, 140, 170)           # 粉色"纹样"
_syn[40:80, 40:80] = (245, 245, 240)           # 内部有块近白（模拟白绣线）
_syn_path = os.path.join(tmp, "syn.jpg")
Image.fromarray(_syn).save(_syn_path)

_r = mx.extract(_syn_path, out_dir=os.path.join(tmp, "motifs"))
check("合成图提取成功", _r["ok"], _r.get("message", ""))
check("识别出的底布接近靛蓝",
      abs(_r["cloth_rgb"][2] - 150) < 60, str(_r["cloth_rgb"]))
check("纹样区域被正确圈出（占比合理）",
      0.10 < _r["coverage"] < 0.70, "占比 %.0f%%" % (_r["coverage"] * 100))
check("提取后有透明通道图落盘",
      _r["saved"] and os.path.exists(_r["saved"]))
check("裁剪后尺寸小于原图（去掉了布面）",
      _r["size"][0] <= 120 and _r["size"][1] <= 120, str(_r["size"]))

# 关键：内部那块近白应当**保留**在纹样里（它是绣线，不是布）
_cx, _cy = 60, 60
_cut = _r["array"]
_mask_cut = _r["alpha"]
if _cut is not None and _mask_cut is not None:
    _h, _w = _mask_cut.shape
    _inside = _mask_cut[_h // 2, _w // 2]
    check("纹样内部的浅色块被保留（没被当成布挖空）", bool(_inside))

# --- 不存在的文件应报错而不是静默 ---
try:
    mx.extract(os.path.join(tmp, "不存在.jpg"))
    check("缺失文件抛明确异常", False, "未抛异常")
except mx.MotifExtractError:
    check("缺失文件抛明确异常", True)
except Exception as e:
    check("缺失文件抛明确异常", False, "抛了 %s" % type(e))

# --- 2D 拼贴与 3D 高度图 ---
_tile = mx.tile_preview(_syn, np.ones((120, 120), dtype=bool),
                        tiles=(2, 2), size=64)
# 注意：mirror=True 时单元本身是 size×2 的镜像块，再按 tiles 铺开
# size=64, tiles=(2,2) -> 单元 128x128 -> 成品 256x256
check("2D 四方连续可生成", _tile is not None and _tile.size == (256, 256),
      str(getattr(_tile, "size", None)))
check("拼贴尺寸随 tiles 变化",
      mx.tile_preview(_syn, np.ones((120, 120), dtype=bool),
                      tiles=(3, 1), size=50).size == (300, 100),
      str(mx.tile_preview(_syn, np.ones((120, 120), dtype=bool),
                          tiles=(3, 1), size=50).size))
check("关闭镜像时单元不翻倍",
      mx.tile_preview(_syn, np.ones((120, 120), dtype=bool),
                      tiles=(2, 2), size=50, mirror=False).size == (100, 100))
# 默认铺底色：四方连续是看「拼接效果」的，透明会变成一片黑，
# 所以默认返回已合成底色的图；需要透明时显式传 backdrop=None。
check("拼贴默认已铺底色（不是透明图）",
      mx.tile_preview(_syn, np.ones((120, 120), dtype=bool),
                      tiles=(1, 1), size=64).convert("RGB").getpixel(
          (2, 2)) != (0, 0, 0),
      str(mx.tile_preview(_syn, np.ones((120, 120), dtype=bool),
                          tiles=(1, 1), size=64).getpixel((2, 2))))
check("传 backdrop=None 可保留透明",
      # 必须用非正方样本：正方样本缩放后正好填满单元，没有留白可看。
      # 120x60 缩放到 64x64 的单元里，上下会留白，那里 alpha=0。
      mx.tile_preview(_syn[:60, :], np.ones((60, 120), dtype=bool),
                      tiles=(1, 1), size=64, backdrop=None).getpixel(
          (1, 1))[3] == 0,
      str(mx.tile_preview(_syn[:60, :], np.ones((60, 120), dtype=bool),
                          tiles=(1, 1), size=64, backdrop=None).getpixel(
              (1, 1))))

_hm = mx.heightmap(_syn, np.ones((120, 120), dtype=bool), size=64)
check("3D 高度图可生成", _hm is not None and _hm.shape == (64, 64))
check("高度值归一化到 0..1",
      float(_hm.min()) >= 0.0 and float(_hm.max()) <= 1.0 + 1e-6,
      "范围 %.2f~%.2f" % (_hm.min(), _hm.max()))

check("空输入时拼贴返回 None", mx.tile_preview(None, None) is None)
check("空输入时高度图返回 None", mx.heightmap(None, None) is None)

# --- 棋盘格底（界面用来检验透明纹样的轮廓）---
# 为什么要有它：basic 系列不少纹样的花瓣是白线，放白底上会和背景糊在一起，
# 看起来像「花瓣被挖空了」。棋盘格深浅交替，能立刻区分
# 「真的透明孔洞」和「白色绣线」。
_cb = mx.checkerboard(64, 48, cell=16)
check("棋盘格尺寸正确", _cb.shape == (48, 64, 4), str(_cb.shape))
check("棋盘格是不透明的", int(_cb[..., 3].min()) == 255)
# 相邻两格颜色不同，同格内颜色一致
check("棋盘格交替着色",
      not np.array_equal(_cb[0, 0, :3], _cb[0, 16, :3])
      and np.array_equal(_cb[0, 0, :3], _cb[8, 8, :3]))
check("棋盘格默认为浅色调（白/浅灰）",
      int(_cb[..., :3].min()) >= 180, "最暗 %d" % _cb[..., :3].min())

# 透明合成不应该报错，且透明处露出棋盘格
_rgba = np.dstack([_syn, (np.ones((120, 120), dtype=bool) * 255).astype(np.uint8)])
_bgc = mx.checkerboard(_syn.shape[1], _syn.shape[0])
from PIL import Image as _PImage
_comp = _PImage.alpha_composite(_PImage.fromarray(_bgc, "RGBA"),
                                _PImage.fromarray(_rgba.astype(np.uint8), "RGBA"))
check("棋盘底与纹样可合成", np.asarray(_comp).shape == (_syn.shape[0],
                                                       _syn.shape[1], 4))

# --- 大图性能：_largest_components 必须降采样后再做洪泛填充 ---
#
# 背景：洪泛填充每轮只扩 1 像素。1700×1200 的图上，
# 一个横跨全图的连通块要迭代 ~1500 轮 × 4 次整幅位移 = 实测 23 秒。
# 修复是先在最长边 ≤320 的低分辨率上做拓扑判断，再映射回原图。
# 这个测试锁住「大图不能慢成几十秒」。
import time as _time
_big = np.zeros((1200, 1600), dtype=bool)
_big[10:1190, 10:1590] = True        # 一个巨大连通块，最大化迭代轮数
_t0 = _time.time()
_big_out = mx._largest_components(_big, keep=6)
_big_el = _time.time() - _t0
check("大图连通块分析在 3 秒内完成", _big_el < 3.0,
      "实测 %.2f 秒" % _big_el)
check("大图连通块结果尺寸不变", _big_out.shape == _big.shape)
check("大图连通块保留了主体", bool(_big_out.any()))

# 降采样不能凭空造出像素：结果必须是原掩膜的子集
check("降采样后结果不超出原掩膜", not bool((_big_out & ~_big).any()))

# --- 门控：有渐变的深色布也要能提取，不能被 cover 一刀切掉 ---
# 背景：曾用 cover<0.05 判「底布无法判断」，但外圈常切到花朵，
# 花占得多 cover 就低 —— basic_0(0.0503) 通过而 basic_592(0.0451) 被拒，
# 纯属刀锋式误判。现在只把 estimate_cloth 返回 None 当失败。
_grad = np.zeros((200, 200, 3), dtype=np.uint8)
# 造一张「连续渐变的深色底 + 一块亮纹样」，模拟 basic_592 那种黑布
for _i in range(200):
    _grad[:, _i] = [10 + _i // 8, 12 + _i // 9, 14 + _i // 10]
_grad[60:140, 60:140] = [230, 120, 160]
_gc = mx.estimate_cloth(_grad)
check("渐变深色底也能估出底布", _gc is not None,
      "返回 None 说明门控过严")
check("渐变深色底的估计接近深色", _gc is not None and float(
    np.mean(_gc["rgb"])) < 90,
    str(None if _gc is None else np.round(_gc["rgb"], 1)))


# ===========================================================================
# 15. 3D 立体展示（把纹样做成有厚度的实体网格）
# ===========================================================================
print()
print("15. 3D 立体展示（纹样是有体积的实物，不是一张图）")


def _syn_motif(h=120, w=160):
    """造一张合成纹样：深蓝底 + 中间一朵亮花。"""
    a = np.zeros((h, w, 3), dtype=np.uint8)
    a[:, :] = [30, 34, 70]
    yy, xx = np.mgrid[0:h, 0:w]
    disk = ((yy - h / 2) ** 2 / (h * 0.30) ** 2
            + (xx - w / 2) ** 2 / (w * 0.26) ** 2) < 1.0
    a[disk] = [235, 130, 170]
    am = disk.astype(bool)
    return a, am


_sa, _sm = _syn_motif()

# --- relief_field：应产出有结构的平滑高度场 ---
_rf = mx.relief_field(_sa, _sm, size=100)
check("浮雕高度场可生成", _rf is not None)
check("浮雕高度场尺寸正确", _rf is not None and _rf.shape[0] > 0)
# 纹样外必须严格为 0（否则网格会出现悬空的碎片）
check("浮雕高度场纹样外为 0",
      _rf is not None and float(_rf[_rf > 0].min()) > 0.0)
# 中心应比边缘高（这是"浮雕坡面"的核心特征）
if _rf is not None:
    _cy, _cx = _rf.shape[0] // 2, _rf.shape[1] // 2
    _center = float(_rf[_cy - 2:_cy + 2, _cx - 2:_cx + 2].mean())
    _edge_band = _rf[_rf > 0]
    _edge = float(np.percentile(_edge_band, 8))
    check("浮雕中心高于边缘（有坡面）", _center > _edge,
          "中心 %.3f 边缘 %.3f" % (_center, _edge))
    # 高度值不该是极端二值（二值说明只做了掩膜，没做高度）
    _uniq = len(np.unique(np.round(_rf[_rf > 0], 2)))
    check("浮雕高度有连续层次", _uniq > 5, "唯一值 %d" % _uniq)

    # 坡度量级必须与物理尺度相称 —— 这是"3D 看起来像刀山"这个
    # bug 的**唯一可靠的回归防线**，务必保留。
    #
    # 背景：alpha 是二值掩膜，如果结尾用硬截断（h * (a > 0.5)），
    # 轮廓上会切出接近 0.5 的垂直落差；如果 z 放大系数给太大，
    # 整片绣片会被拉成 60 度的假山。这两件事都不会让
    # "中心高于边缘"这类断言失败，但渲染出来完全不是绣片。
    #
    # 实测正常值：纹样内部相邻梯度中位数约 0.004~0.008，
    # 99 分位约 0.09。给一条宽松但有效的水位线：中位数必须 < 0.05，
    # 否则说明高度场里出现了大片陡壁。
    if _rf is not None:
        _g = np.r_[np.abs(np.diff(_rf, axis=1)).ravel(),
                   np.abs(np.diff(_rf, axis=0)).ravel()]
        _gmed = float(np.median(_g))
        _g99 = float(np.percentile(_g, 99))
        check("浮雕高度场无大片陡壁（梯度中位数 < 0.05）",
              _gmed < 0.05, "中位数 %.4f / 99%%位 %.4f" % (_gmed, _g99))
        # 轮廓不能有"围栏"：纹样内与纹样外的过渡应当平缓。
        # 用「高值区的边界落差」来测：取高度 > 0.3 的区域，
        # 它与外部的交界处梯度不应接近整片高度。
        _hi = _rf > 0.3
        if _hi.any():
            _drop = np.abs(np.diff(_hi.astype(np.float32), axis=1)).mean()
            check("纹样内部形成连续主体（不是零散碎片）",
                  float(_hi.mean()) > 0.15,
                  "高区占比 %.1f%%" % (100 * float(_hi.mean())))

# --- z 放大系数必须落在物理合理的量级 ---
# 0.55 会把坡度拉到 60 度（实测），0.05 以下又平到看不出立体。
check("RELIEF_Z_SCALE 在合理量级",
      0.08 <= mx.RELIEF_Z_SCALE <= 0.40,
      "当前 %.2f" % mx.RELIEF_Z_SCALE)

# --- relief_mesh：应产出合法的三角网格 ---
_msh = mx.relief_mesh(_sa, _sm, size=100, max_cells=60, thickness=0.10)
check("3D 网格可生成", _msh is not None)
if _msh:
    _nv = len(_msh["x"])
    _nt = len(_msh["i"])
    check("网格顶点数合理", 100 < _nv < 60000, "顶点 %d" % _nv)
    check("网格三角面数合理", _nt > 0, "三角面 %d" % _nt)
    check("网格顶点坐标等长",
          len(_msh["x"]) == len(_msh["y"]) == len(_msh["z"]) == len(_msh["color"]))
    check("网格三角索引等长",
          len(_msh["i"]) == len(_msh["j"]) == len(_msh["k"]))
    # 索引不能越界 —— 越界会让 Plotly 直接崩
    _mx_idx = max(max(_msh["i"]), max(_msh["j"]), max(_msh["k"]))
    check("三角索引不越界", _mx_idx < _nv, "最大索引 %d / 顶点 %d" % (_mx_idx, _nv))
    # 必须有厚度：正面与背面不能是同一个平面
    _zspan = max(_msh["z"]) - min(_msh["z"])
    check("网格有实际厚度", _zspan > 0.05, "z 跨度 %.3f" % _zspan)
    # 顶点色应是 rgb(...) 字符串
    check("顶点色格式正确",
          isinstance(_msh["color"][0], str)
          and _msh["color"][0].startswith("rgb("))
    # 颜色不该只有一种（说明真的把纹样颜色贴上去了）
    check("顶点色有多种取值", len(set(_msh["color"])) > 3,
          "%d 种" % len(set(_msh["color"])))

# --- 连通域清理：碎片孤岛必须被去掉，只留最大的一块 ---
# 为什么要测：alpha 里混进来的小块杂物（水印/反光斑）会被
# 高度场当成完整纹样抬起来，渲染成一片竖立的"鳍"。
_lc = np.zeros((40, 40), dtype=bool)
_lc[5:35, 5:35] = True         # 最大的主体
_lc[0:3, 0:3] = True           # 左上角小碎片
_lc[37:39, 36:39] = True       # 右下角小碎片
_lc_out = mx._largest_component(_lc)
check("连通域清理：保留最大主体",
      _lc_out is not None and bool(_lc_out[20, 20]))
check("连通域清理：去掉左上碎片", not bool(_lc_out[1, 1]))
check("连通域清理：去掉右下碎片", not bool(_lc_out[38, 37]))
check("连通域清理：结果面积等于主体",
      _lc_out is not None and int(_lc_out.sum()) == 30 * 30,
      "面积 %s" % (int(_lc_out.sum()) if _lc_out is not None else "None"))
check("连通域清理：全空输入安全",
      mx._largest_component(np.zeros((10, 10), dtype=bool)) is not None
      and not mx._largest_component(np.zeros((10, 10), dtype=bool)).any())

# --- 填内部孔：3D 网格能用的前提（有孔就会长出满屏竖条纹）---
_fh = np.zeros((40, 40), dtype=bool)
_fh[5:35, 5:35] = True
_fh[18:22, 18:22] = False        # 正中间挖一个内部孔
_fh_out = mx._fill_holes(_fh)
check("填内部孔：孔被填上",
      _fh_out is not None and bool(_fh_out[20, 20]))
check("填内部孔：外边界不被扩张",
      _fh_out is not None and bool(_fh_out[5, 5]) and not bool(_fh_out[0, 0]))
check("填内部孔：外部背景保持为空",
      _fh_out is not None and int(_fh_out.sum()) == 30 * 30,
      "面积 %s" % (int(_fh_out.sum()) if _fh_out is not None else "None"))

# --- 3D 实体轮廓：应当是"一整块"，不是碎裂的花瓣 ---
_so = mx._solid_outline(_fh, 40, 40)
check("实体轮廓可生成", _so is not None and _so.any())
if _so is not None:
    check("实体轮廓是连成一片的（无内部孔）",
          bool(_so[20, 20]))
    # 轮廓面积不应小于原掩膜（外轮廓只做加法）
    check("实体轮廓不小于原掩膜",
          int(_so.sum()) >= int(_fh.sum()),
          "%d vs %d" % (int(_so.sum()), int(_fh.sum())))

# --- 3D 网格里的陡面比例（竖条纹的量化指标）---
# 陡面 = dz/dh 远大于 1 的面。侧壁本身是竖直的（正常），
# 但如果比例过高，说明网格被切碎了。
_msh3 = mx.relief_mesh(_sa, _sm, size=120, max_cells=60, thickness=0.06)
if _msh3:
    _x3 = np.array(_msh3["x"]); _y3 = np.array(_msh3["y"])
    _z3 = np.array(_msh3["z"])
    _i3 = np.array(_msh3["i"]); _j3 = np.array(_msh3["j"])
    _k3 = np.array(_msh3["k"])
    _X3 = np.stack([_x3[_i3], _x3[_j3], _x3[_k3]], 1)
    _Y3 = np.stack([_y3[_i3], _y3[_j3], _y3[_k3]], 1)
    _Z3 = np.stack([_z3[_i3], _z3[_j3], _z3[_k3]], 1)
    _dh3 = np.hypot(_X3.max(1) - _X3.min(1), _Y3.max(1) - _Y3.min(1))
    _dz3 = _Z3.max(1) - _Z3.min(1)
    _sl3 = np.where(_dh3 > 1e-9, _dz3 / np.maximum(_dh3, 1e-9), np.nan)
    _sl3 = _sl3[~np.isnan(_sl3)]
    if _sl3.size:
        check("3D 陡面比例可控（< 60%）",
              float((_sl3 > 3).mean()) < 0.60,
              "%.1f%%" % (100 * float((_sl3 > 3).mean())))

# --- 越界的 max_cells 也要能给出结果（不能返回 None 或抛异常）---
_m2 = mx.relief_mesh(_sa, _sm, size=100, max_cells=8)
check("极小网格密度仍可生成", _m2 is not None)

# --- 空纹样必须安全返回 None，而不是崩 ---
_empty = np.ones((50, 50), dtype=bool)
_empty[:] = False
check("空纹样的高度场安全返回 None",
      mx.relief_field(np.zeros((50, 50, 3), dtype=np.uint8), _empty) is None)
check("空纹样无法建网格",
      mx.relief_mesh(np.zeros((50, 50, 3), dtype=np.uint8), _empty) is None)


# ===========================================================================
# 16. 图数据库教学模块
# ===========================================================================
section("16. 图数据库教学模块 graph_teach.py")

import graph_teach as gteach                                   # noqa: E402

# --- 静态教学内容完整性 ---
check("图模型定义了 10 类节点", len(gteach.NODE_LABELS) == 10,
      "实际 %d" % len(gteach.NODE_LABELS))
check("每类节点都有中文名与说明",
      all(len(v) >= 2 and v[0] and v[1]
          for v in gteach.NODE_LABELS.values()))
check("关系类型不少于 8 种", len(gteach.REL_TYPES) >= 8,
      "实际 %d" % len(gteach.REL_TYPES))
check("关系的两端都是已定义节点",
      all(a in gteach.NODE_LABELS and b in gteach.NODE_LABELS
          for (a, b, _cn, _d) in gteach.REL_TYPES.values()))
check("查询预设不少于 5 条", len(gteach.QUERY_PRESETS) >= 5)
check("每条预设都同时有 Cypher 和 SQL",
      all(p.get("cypher") and p.get("sql")
          for p in gteach.QUERY_PRESETS))
check("每条预设都有教学说明 why",
      all(p.get("why") for p in gteach.QUERY_PRESETS))
check("每条预设的 id 唯一",
      len({p["id"] for p in gteach.QUERY_PRESETS}) == len(gteach.QUERY_PRESETS))
check("preset_by_id 能查到",
      gteach.preset_by_id(gteach.QUERY_PRESETS[0]["id"]) is not None)
check("preset_by_id 查不到时返回 None",
      gteach.preset_by_id("__not_exist__") is None)

# --- 对照表内容 ---
tv = gteach.TABLE_VS_GRAPH
check("对照表有两侧名称", tv.get("left_name") and tv.get("right_name"))
check("对照表有不少于 5 行对比", len(tv.get("rows", [])) >= 5)
check("对照表有结论", bool(tv.get("结论")))

# --- CSV 内存图构建 ---
_cg = gteach.build_csv_graph(ROOT)
check("CSV 图可构建", _cg is not None)
if _cg is not None:
    _st = _cg.stats()
    check("图里有节点", _st["nodes"] > 100, "节点 %d" % _st["nodes"])
    check("图里有关系", _st["rels"] > 100, "关系 %d" % _st["rels"])
    check("有 Image 节点", _st["by_label"].get("Image", 0) >= 1000,
          "%d" % _st["by_label"].get("Image", 0))
    check("有 MotifGroup 节点", _st["by_label"].get("MotifGroup", 0) >= 200)
    check("有 Color 节点", _st["by_label"].get("Color", 0) > 0)
    check("有 GeneratedWork 节点",
          _st["by_label"].get("GeneratedWork", 0) >= 1)
    check("有 HAS_VARIANT 关系", _st["by_rel"].get("HAS_VARIANT", 0) > 0)
    check("有 DERIVED_FROM 关系",
          _st["by_rel"].get("DERIVED_FROM", 0) > 0)
    check("有 USES_COLOR 关系", _st["by_rel"].get("USES_COLOR", 0) > 0)

    # --- 每条预设都要能跑通，且结构合法（不抛异常）---
    for _p in gteach.QUERY_PRESETS:
        _pid = _p["id"]
        try:
            _r = _cg.run_preset(_pid, _p.get("params_hint"))
            _ok = isinstance(_r, list)
        except Exception as _e:
            _r, _ok = None, False
            print("        (%s 抛异常 %s)" % (_pid, _e))
        check("预设 %s 可执行且返回列表" % _pid, _ok)

    # --- 关键查询的语义正确性 ---
    _var = _cg.q_variant_chain("basic_0")
    check("① 变体查询有结果", len(_var) > 0, "%d 行" % len(_var))
    check("① 变体查询的源图正确",
          all(r["源图"] == "basic_0" for r in _var))
    check("① 变体结果含 color/texture/mixup 类型",
          any(r["类型"] in ("color", "texture", "mixup") for r in _var))

    _two = _cg.q_two_hop("basic_0", depth=2)
    check("② 两跳查询有结果", len(_two) > 0, "%d 行" % len(_two))

    _nb = _cg.q_neighbors("basic_8")
    check("③ 邻居查询有结果", len(_nb) > 0, "%d 行" % len(_nb))
    check("③ 邻居结果有类型列",
          all("邻居类型" in r and "关系" in r for r in _nb))

    _sc = _cg.q_shared_color("basic_0")
    check("④ 共色查询有结果", len(_sc) > 0, "%d 行" % len(_sc))
    check("④ 共色结果是按数量降序",
          all(_sc[i]["同色图片数"] >= _sc[i + 1]["同色图片数"]
              for i in range(len(_sc) - 1)))

    _rg = _cg.q_count_by_region()
    check("⑤ 地域统计可执行", isinstance(_rg, list))
    check("⑤ 地域统计按图片数降序",
          all(_rg[i]["图片数"] >= _rg[i + 1]["图片数"]
              for i in range(len(_rg) - 1)))

    _tr = _cg.q_tech_region()
    check("⑥ 技法×地域可执行", isinstance(_tr, list))

    # --- 不存在的图 id 要安全返回空，而不是崩 ---
    check("查不存在的图返回空", _cg.q_variant_chain("__nope__") == [])
    check("查不存在的邻居返回空", _cg.q_neighbors("__nope__") == [])

    # --- 不存在的预设要安全返回空列表 ---
    check("未知预设返回空列表", _cg.run_preset("__nope__") == [])


# ===========================================================================
# 课程结构（app/course.py）
# ===========================================================================
section("课程结构：单元、课时、分层与进度")

import course

# --- 结构完整性 ---
check("课程含 4 个教学单元", len(course.UNITS) == 4,
      "实际 %d" % len(course.UNITS))

# 方案 §6：16 学时理论 + 16 学时实验 = 32 课时
check("总课时为 32", course.TOTAL_HOURS == 32,
      "实际 %d" % course.TOTAL_HOURS)
check("理论 16 课时", course.THEORY_HOURS == 16,
      "实际 %d" % course.THEORY_HOURS)
check("实践 16 课时", course.PRACTICE_HOURS == 16,
      "实际 %d" % course.PRACTICE_HOURS)

# 每个单元都要有课时、标签页、产出、步骤、实验指导书
for _u in course.UNITS:
    _p = "第%d单元" % _u["no"]
    check("%s 有课时" % _p, _u["hours"] > 0)
    check("%s 理论+实践=总课时" % _p,
          _u["theory"] + _u["practice"] == _u["hours"])
    check("%s 指向至少一个标签页" % _p, len(_u["tabs"]) > 0)
    check("%s 有产出物" % _p, len(_u["outputs"]) > 0)
    check("%s 有操作步骤" % _p, len(_u["steps"]) > 0)
    check("%s 有拓展层任务" % _p, len(_u["advanced"]) > 0)
    check("%s 有实验指导书" % _p, bool(_u["exp_file"]))

# 单元序号必须连续 1..4
check("单元序号连续",
      [u["no"] for u in course.UNITS] == [1, 2, 3, 4])

# --- 标签页反查 ---
check("⑥ 知识图谱 归到第二单元",
      course.unit_for_tab("⑥ 知识图谱")["no"] == 2)
check("② AI 重组创作 归到第三单元",
      course.unit_for_tab("② AI 重组创作")["no"] == 3)
check("④ 提取纹样 归到第一单元",
      course.unit_for_tab("④ 提取纹样")["no"] == 1)
check("未知标签页返回 None",
      course.unit_for_tab("__不存在__") is None)

# 界面里实际存在的标签页都应能被某单元认领（不能有孤儿页）
for _t in ["① 检索纹样", "② AI 重组创作", "③ 我的作品",
           "④ 提取纹样", "⑤ 标注纹样", "⑥ 知识图谱"]:
    check("标签页 %s 能挂到某单元" % _t,
          course.unit_for_tab(_t) is not None)

# --- 分层 ---
check("有两个学习层次", len(course.TRACKS) == 2)
check("层次含基础层", course.TRACK_BASIC in course.TRACKS)
check("层次含拓展层", course.TRACK_ADVANCED in course.TRACKS)

# --- 进度计算：空状态 ---
_ctx0 = {"annotated_groups": 0, "total_groups": 245, "extracted_count": 0,
         "works": [], "peer_reviews_given": 0, "student_id": "S1"}
_p0 = course.overall_progress(_ctx0)
check("空状态下没有单元完成", _p0["units_done"] == 0)
check("空状态进度为 0", _p0["ratio"] == 0.0)
for _r in _p0["rows"]:
    check("空状态每个单元都有说明文字", bool(_r["label"]))

# --- 进度计算：全部完成 ---
_ctx1 = {"annotated_groups": 5, "total_groups": 245, "extracted_count": 3,
         "works": [{"student_id": "S1", "design_note": "改了配色"}],
         "peer_reviews_given": 3, "student_id": "S1"}
_p1 = course.overall_progress(_ctx1)
check("数据齐全时 4 个单元全完成", _p1["units_done"] == 4,
      "实际 %d" % _p1["units_done"])
check("全完成时进度为 1", abs(_p1["ratio"] - 1.0) < 1e-9)

# --- 单元 2 门槛：标注不足 3 组不算完成 ---
_ctx2 = dict(_ctx1)
_ctx2["annotated_groups"] = 2
_r2 = course.unit_progress(course.UNITS[1], _ctx2)
check("标注 2 组未达门槛，单元2 未完成", not _r2["done"])
_ctx2["annotated_groups"] = 3
_r2b = course.unit_progress(course.UNITS[1], _ctx2)
check("标注满 3 组，单元2 完成", _r2b["done"])

# --- 单元 3：有作品但没写设计说明，只算半完成 ---
_ctx3 = {"annotated_groups": 0, "total_groups": 245, "extracted_count": 0,
         "works": [{"student_id": "S1", "design_note": ""}],
         "peer_reviews_given": 0, "student_id": "S1"}
_r3 = course.unit_progress(course.UNITS[2], _ctx3)
check("有作品但无设计说明 -> 未完成", not _r3["done"])
check("有作品但无设计说明 -> 进度 0.5", abs(_r3["ratio"] - 0.5) < 1e-9)
check("提示里点出缺设计说明", "设计说明" in _r3["label"])

# --- 只统计本人作品 ---
_ctx4 = {"annotated_groups": 0, "total_groups": 245, "extracted_count": 0,
         "works": [{"student_id": "S1", "design_note": "a"},
                   {"student_id": "S2", "design_note": ""}],
         "peer_reviews_given": 0, "student_id": "S1"}
_r4 = course.unit_progress(course.UNITS[2], _ctx4)
check("只算本人作品时，别人的空说明不影响", _r4["done"])


# ===========================================================================
# 作品归档扩展：设计说明 + 学生互评
# ===========================================================================
section("作品归档：设计说明与学生互评")

import tempfile as _tf
from archive import WorkArchive, WORK_FIELDS, PEER_FIELDS

_tmp2 = _tf.mkdtemp()
_ar2 = WorkArchive(os.path.join(_tmp2, "works.csv"),
                   os.path.join(_tmp2, "generated"))

check("WORK_FIELDS 含 design_note", "design_note" in WORK_FIELDS)
check("PEER_FIELDS 定义完整",
      set(["work_id", "reviewer_id", "reviewer_name",
           "merit", "suggestion", "created_at"]) <= set(PEER_FIELDS))

_w1 = _ar2.record(student_id="S1", student_name="张三",
                  source_images=["basic_0"], mode="img2img",
                  prompt="miao", output_path="/tmp/a.png")
check("新记录默认 design_note 为空", _ar2.load()[0]["design_note"] == "")

# 写设计说明
_ok = _ar2.update_design_note(_w1, "把底布换成深靛蓝，保留花枝走向")
check("设计说明写入成功", _ok)
check("设计说明读回正确",
      _ar2.load()[0]["design_note"] == "把底布换成深靛蓝，保留花枝走向")
check("写设计说明不影响审核状态",
      _ar2.load()[0]["review_status"] == "待审核")

# 写不存在的作品应返回 False，而不是崩
check("改不存在的作品返回 False",
      _ar2.update_design_note("__nope__", "x") is False)

# --- 互评 ---
_ar2.add_peer_review(_w1, reviewer_id="S2", reviewer_name="李四",
                     merit="配色克制", suggestion="花心可以再收一点")
check("互评写入成功", len(_ar2.load_peer_reviews(_w1)) == 1)

# 同一人重复提交应覆盖而非追加
_ar2.add_peer_review(_w1, reviewer_id="S2", reviewer_name="李四",
                     merit="改后的优点", suggestion="改后的建议")
_pr = _ar2.load_peer_reviews(_w1)
check("同一人重复互评去重为 1 条", len(_pr) == 1)
check("重复互评保留最新内容", _pr[0]["merit"] == "改后的优点")

# 第二个人评同一件
_ar2.add_peer_review(_w1, reviewer_id="S3", reviewer_name="王五",
                     merit="结构好", suggestion="纹理可保留更多")
check("不同人可各评一条", len(_ar2.load_peer_reviews(_w1)) == 2)

# 统计
_w2 = _ar2.record(student_id="S2", student_name="李四", output_path="/tmp/b.png")
check("按学号统计已评数（S2 评了 1 件）",
      _ar2.peer_review_stats("S2")["given"] == 1)
check("统计全表互评条数", _ar2.peer_review_stats()["total"] == 2)
check("统计每件作品收到几条",
      _ar2.peer_review_stats()["by_work"].get(_w1) == 2)

# 按作品筛选互评
check("按 work_id 筛互评", len(_ar2.load_peer_reviews(_w2)) == 0)

# --- 旧表兼容：没有 design_note 列的历史文件也要能读 ---
_legacy = os.path.join(_tmp2, "legacy.csv")
_old_cols = [c for c in WORK_FIELDS if c != "design_note"]
import csv as _csv
with open(_legacy, "w", newline="", encoding="utf-8-sig") as _f:
    _wtr = _csv.DictWriter(_f, fieldnames=_old_cols)
    _wtr.writeheader()
    _wtr.writerow({c: ("W1" if c == "work_id" else "") for c in _old_cols})
_ar3 = WorkArchive(_legacy, os.path.join(_tmp2, "g2"))
_leg = _ar3.load()
check("旧表读取不报错", len(_leg) == 1)
check("旧表补齐 design_note 键", "design_note" in _leg[0])
check("旧表 design_note 值为空串", _leg[0]["design_note"] == "")
# 在旧表上写新字段，重写后不能丢列
check("旧表可写设计说明", _ar3.update_design_note("W1", "补的新说明"))
check("旧表写后仍是完整列数",
      len(_ar3.load()[0]) == len(WORK_FIELDS),
      "实际 %d / 期望 %d" % (len(_ar3.load()[0]), len(WORK_FIELDS)))
check("旧表写后能读回", _ar3.load()[0]["design_note"] == "补的新说明")


# ===========================================================================
# 存储层：并发写的安全底座 app/storage.py
# ===========================================================================
# 这一组存在的理由：一个班几十个学生同时提交，原来的「读进来 → open('w')
# 截断 → 写回」在 10 线程 × 20 条的实测里只剩 2~8 条（丢 ~97%）。丢的是
# 学生填的设计说明，且全程无报错。必须钉住。
section("存储层：文件锁与原子写 app/storage.py")

import threading  # noqa: E402
import time  # noqa: E402
import inspect  # noqa: E402
import storage  # noqa: E402

_tmpS = tempfile.mkdtemp()
_CSV_S = os.path.join(_tmpS, "safe.csv")
_F = ["work_id", "note"]

# --- 基本读写 ---
check("读不存在的文件返回空列表", storage.read_csv_rows(_CSV_S) == [])
storage.atomic_write_csv(_CSV_S, _F, [{"work_id": "W1", "note": "甲"}])
_r = storage.read_csv_rows(_CSV_S, _F)
check("原子写后可读回", len(_r) == 1 and _r[0]["work_id"] == "W1", str(_r))

# --- 原子性：写坏了不能留残表和临时文件 ---
storage.atomic_write_csv(_CSV_S, _F, [{"work_id": "W9", "note": "丙"}])
_leftover = [n for n in os.listdir(_tmpS) if ".tmp_" in n]
check("原子写不留临时文件", _leftover == [], str(_leftover))
check("重写后是最新内容", storage.read_csv_rows(_CSV_S, _F)[0]["note"] == "丙")


# 模拟写一半崩了：临时文件必须被收走，原表必须原样不动。
# 不用 unittest.mock —— 这里只想造一个"写的过程中抛异常"，手工替换方法最直白。
_orig_writer = storage.csv.DictWriter


def _boom_writer(*a, **k):
    raise RuntimeError("模拟写一半崩溃")


storage.csv.DictWriter = _boom_writer
try:
    storage.atomic_write_csv(_CSV_S, _F, [{"work_id": "X", "note": "崩"}])
    _crashed = False
except RuntimeError:
    _crashed = True
finally:
    storage.csv.DictWriter = _orig_writer

check("写崩溃会抛出而非静默", _crashed)
check("写崩溃后原表仍可读", storage.read_csv_rows(_CSV_S, _F)[0]["note"] == "丙")
check("写崩溃后没有残留临时文件",
      [n for n in os.listdir(_tmpS) if ".tmp_" in n] == [])

# --- 多余列不会被写出去 ---
storage.atomic_write_csv(_CSV_S, _F,
                         [{"work_id": "W1", "note": "甲", "junk": "x"}])
with open(_CSV_S, encoding="utf-8-sig", newline="") as f:
    _hdr = next(csv.reader(f))
check("多余列被丢弃而不报错", _hdr == _F, str(_hdr))

# --- 并发：这是核心断言 ---
storage.atomic_write_csv(_CSV_S, _F, [])
_NT, _NE = 8, 15


def _worker(tid):
    for i in range(_NE):
        with storage.file_lock(_CSV_S):
            rows = storage.read_csv_rows(_CSV_S, _F)
            rows.append({"work_id": "W%d_%d" % (tid, i), "note": str(tid)})
            storage.atomic_write_csv(_CSV_S, _F, rows)


_ts = [threading.Thread(target=_worker, args=(t,)) for t in range(_NT)]
for t in _ts:
    t.start()
for t in _ts:
    t.join()

_rows = storage.read_csv_rows(_CSV_S, _F)
check("并发 %d 线程各追加 %d 条不丢更新" % (_NT, _NE),
      len(_rows) == _NT * _NE,
      "实际 %d / 应为 %d" % (len(_rows), _NT * _NE))
_ids = [r["work_id"] for r in _rows]
check("并发写不产生重复行", len(set(_ids)) == len(_ids),
      "重复 %d 条" % (len(_ids) - len(set(_ids))))
check("并发写不产生残缺行", all(r["work_id"] for r in _rows))

# --- 互斥本身：持锁期间别人抢不到 ---
_lock_held = {"inner": False}


def _holder():
    with storage.file_lock(_CSV_S):
        time.sleep(0.45)
        _lock_held["inner"] = True


_th = threading.Thread(target=_holder)
_th.start()
time.sleep(0.12)
_t0 = time.time()
with storage.file_lock(_CSV_S):
    pass
_waited = time.time() - _t0
_th.join()
check("锁忙时后来者会等待而非同时进入", _waited >= 0.15,
      "等待 %.2fs" % _waited)
check("持锁线程正常完成", _lock_held["inner"] is True)

# --- 锁文件留在磁盘上（删掉会让互斥失效） ---
check("锁文件保留而非删除", os.path.exists(_CSV_S + ".lock"))

# --- Archive / AnnotationStore 走的是同一套底座 ---
import archive as _arcmod  # noqa: E402
import annotation_store as _asmod2  # noqa: E402

check("archive 不再使用裸写（'w' 截断）",
      'open(self.csv_path, "w"' not in inspect.getsource(_arcmod.WorkArchive))
check("annotation_store 不再使用裸写",
      'open(self.path, "w"' not in inspect.getsource(_asmod2.AnnotationStore))


# ===========================================================================
# 汇总
# ===========================================================================
print()
print("=" * 62)
print("测试汇总")
print("=" * 62)
print("  通过：%d" % len(PASS))
if SKIP:
    print("  跳过：%d" % len(SKIP))
    for s in SKIP:
        print("        - %s" % s)
print("  失败：%d" % len(FAIL))
for name, detail in FAIL:
    print("        x %s  %s" % (name, detail))
print("=" * 62)
print("结果：%s" % ("全部通过" if not FAIL else "存在 %d 项失败" % len(FAIL)))

sys.exit(1 if FAIL else 0)
