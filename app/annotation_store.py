# -*- coding: utf-8 -*-
"""
标注数据层

背景：
    原本的标注方式是「导出 CSV → 人工填 → 命令行 merge 回去」。
    对公选课学生（非计算机专业）来说，编辑 245 行 CSV 基本不可能完成，
    结果就是标注数据长期为 0，检索界面的地域/技法/色彩筛选全是空的。

    所以做成网页标注：在界面里看图点选，直接落盘。

数据结构（annotation/annotations.csv）：
    group_id        分组编号，主键（一组填一次，自动覆盖全部变体）
    <11 个标注字段>  见 ANNOTATION_FIELDS
    annotator       标注人（学号/姓名）—— 多人协作时用于归因与进度统计
    updated_at      更新时间

为什么以 group_id 为主键：
    250 组 × 4 变体 = 1000 张，但变体之间纹样内容一致。
    按组标注只需填 250 次而非 1000 次 —— 这是整个标注设计的核心。
"""

from __future__ import annotations

import csv
import io
import os
from collections import OrderedDict
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 标注字段定义（英文名 -> 中文标签）
ANNOTATION_FIELDS = OrderedDict([
    ("pattern_type", "纹样类型"),
    ("region", "地域来源"),
    ("technique", "刺绣技法"),
    ("dominant_color", "主色调"),
    ("symmetry_type", "对称形式"),
    ("composition", "构图特征"),
    ("cultural_meaning", "文化寓意"),
    ("permission_note", "来源与授权"),
    ("quality_note", "质量备注"),
    ("pattern_type_detail", "纹样细类"),
    ("usage_suggestion", "适用说明"),
])

# 前 4 项决定检索界面可用性，标为必填级
REQUIRED_FIELDS = ["pattern_type", "region", "technique", "dominant_color"]

# 推荐取值：做成下拉可选项（允许自拟，所以用「选项 + 其他」的方式）
VALUE_OPTIONS = OrderedDict([
    ("pattern_type", [
        "花卉纹", "动物纹", "几何纹", "人物纹",
        "星辰纹", "文字纹", "组合纹", "未确定",
    ]),
    ("region", [
        "贵州黔东南", "贵州黔南", "贵州毕节", "贵州安顺",
        "湖南湘西", "云南文山", "广西融水", "未确定",
    ]),
    ("technique", [
        "平绣", "锁绣", "破线绣", "辫绣", "绉绣", "堆绣",
        "数纱绣", "十字绣", "马尾绣", "打籽绣", "未确定",
    ]),
    ("dominant_color", [
        "靛蓝", "藏青", "深蓝", "黑色", "白色", "朱红", "桃红", "玫红",
        "深绿", "墨绿", "翠绿", "明黄", "土黄", "紫色", "深褐",
    ]),
    ("symmetry_type", [
        "左右对称", "上下对称", "双向对称", "近似对称", "非对称",
    ]),
    ("composition", [
        "中心对称式", "二方连续", "四方连续", "散点式", "满构图", "边饰环绕",
    ]),
    ("cultural_meaning", []),      # 自由填写
    ("permission_note", []),       # 自由填写
    ("quality_note", []),          # 自由填写
    ("pattern_type_detail", []),   # 自由填写
    ("usage_suggestion", []),      # 自由填写
])

# 字段填写提示
FIELD_HINTS = {
    "pattern_type": "从常见题材中选，不确定选「未确定」",
    "region": "苗绣地域性很强；看不出就填「未确定」，不要猜",
    "technique": "技法无法从像素推断，宁填「未确定」",
    "dominant_color": "看纹样本身的颜色，不要照抄机器那列（常是背景色）",
    "symmetry_type": "以人眼判断为准，机器值只作提示",
    "composition": "参考：中心对称式 / 二方连续 / 四方连续 / 散点式",
    "cultural_meaning": "自由填写，如「蝴蝶妈妈是苗族创世神话母题」",
    "permission_note": "来源与授权说明，没有可留空",
    "quality_note": "如：清晰度不足 / 有反光 / 局部模糊",
    "pattern_type_detail": "比纹样类型更细的分类，如「花卉纹」下填「牡丹」",
    "usage_suggestion": "适合做哪类创作任务",
}

EXTRA_FIELDS = ["annotator", "updated_at"]


def default_path():
    """标注文件路径。

    可用环境变量 ANNOTATION_CSV 覆盖，便于：
        - 自动化测试不污染真实数据
        - 部署时把标注数据放到共享盘
    """
    env = os.environ.get("ANNOTATION_CSV", "").strip()
    if env:
        return env
    return os.path.join(ROOT, "annotation", "annotations.csv")


class AnnotationStore:
    """标注数据的读写。

    设计要点：
        - 缺失文件不报错，视为「还没人标注」
        - 写入是「按组 upsert」，多人先后标注同一组会以后来者为准
        - 每次写入都记 annotator 与时间，便于教师统计工作量
    """

    def __init__(self, path=None):
        self.path = path or default_path()
        self._data = None

    # ------------------------------------------------------------------
    def load(self, force=False):
        """读取全部标注，返回 {group_id: {字段: 值}}。"""
        if self._data is not None and not force:
            return self._data

        data = {}
        if not os.path.exists(self.path):
            self._data = data
            return data

        with io.open(self.path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                gid = (row.get("group_id") or "").strip()
                if not gid:
                    continue
                data[gid] = {k: (row.get(k) or "").strip()
                             for k in list(ANNOTATION_FIELDS) + EXTRA_FIELDS}

        self._data = data
        return data

    # ------------------------------------------------------------------
    def get(self, group_id):
        """取某一组的标注，没有则返回空 dict（不返回 None，方便直接用）。"""
        return self.load().get(group_id, {k: "" for k in ANNOTATION_FIELDS})

    # ------------------------------------------------------------------
    def save(self, group_id, values, annotator=""):
        """按组写入。已有则覆盖，没有则新增。

        返回 (is_new, filled_count)
        """
        if not group_id:
            raise ValueError("group_id 不能为空")

        data = self.load()
        is_new = group_id not in data

        row = {k: "" for k in ANNOTATION_FIELDS}
        row.update({k: (values.get(k) or "").strip() for k in ANNOTATION_FIELDS})
        row["annotator"] = annotator.strip()
        row["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        data[group_id] = row
        self._write(data)
        self._data = data

        filled = sum(1 for k in ANNOTATION_FIELDS if row.get(k))
        return is_new, filled

    def delete(self, group_id):
        data = self.load()
        if group_id in data:
            del data[group_id]
            self._write(data)
            self._data = data
            return True
        return False

    # ------------------------------------------------------------------
    def _write(self, data):
        """整表重写。行数在千级以内，重写比增量更新更不容易出错。"""
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fields = ["group_id"] + list(ANNOTATION_FIELDS) + EXTRA_FIELDS

        with io.open(self.path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for gid in sorted(data):
                row = {"group_id": gid}
                row.update({k: data[gid].get(k, "") for k in ANNOTATION_FIELDS})
                row.update({k: data[gid].get(k, "") for k in EXTRA_FIELDS})
                w.writerow(row)

    # ------------------------------------------------------------------
    def progress(self, total_groups):
        """统计填表进度。

        返回 dict：
            annotated      已标注组数（任一必填项非空即算）
            complete       必填项齐全的组数
            total          总组数
            by_field       {字段: 已填数量}
            by_annotator   {标注人: 组数}
        """
        data = self.load()
        by_field = {k: 0 for k in ANNOTATION_FIELDS}
        by_annotator = {}
        complete = 0
        annotated = 0

        for gid, row in data.items():
            filled_any = False
            for k in ANNOTATION_FIELDS:
                if row.get(k):
                    by_field[k] += 1
                    filled_any = True
            if filled_any:
                annotated += 1
            if all(row.get(k) for k in REQUIRED_FIELDS):
                complete += 1
            who = (row.get("annotator") or "").strip() or "（未署名）"
            if filled_any:
                by_annotator[who] = by_annotator.get(who, 0) + 1

        return {
            "annotated": annotated,
            "complete": complete,
            "total": total_groups,
            "by_field": by_field,
            "by_annotator": by_annotator,
        }

    # ------------------------------------------------------------------
    def merge_to_images(self, images_csv):
        """把标注回写进 metadata/images.csv（供检索与建图使用）。

        标注是对「组」的，回写时按 group_id 展开到该组全部图片。

        返回 dict：{updated_rows, groups_applied, fields_written}
        """
        data = self.load()
        if not data:
            return {"updated_rows": 0, "groups_applied": 0, "fields_written": 0}

        if not os.path.exists(images_csv):
            raise FileNotFoundError("找不到元数据文件：%s" % images_csv)

        with io.open(images_csv, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames
            rows = list(reader)

        # 只回写 images.csv 里真实存在的列
        writable = [k for k in ANNOTATION_FIELDS if k in fields]
        updated = 0
        applied = set()

        for r in rows:
            gid = (r.get("group_id") or "").strip()
            a = data.get(gid)
            if not a:
                continue
            changed = False
            for k in writable:
                v = a.get(k, "")
                if v and r.get(k) != v:
                    r[k] = v
                    changed = True
            if changed:
                updated += 1
                applied.add(gid)

        with io.open(images_csv, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

        return {
            "updated_rows": updated,
            "groups_applied": len(applied),
            "fields_written": len(writable),
        }

    def export_sheet(self, out_path):
        """导出成可发给助教的 CSV（中文表头，含进度信息）。"""
        data = self.load()
        fields = ["group_id"] + list(ANNOTATION_FIELDS) + EXTRA_FIELDS
        zh = {k: ANNOTATION_FIELDS.get(k, k) for k in fields}

        with io.open(out_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow([zh.get(k, k) for k in fields])
            for gid in sorted(data):
                row = [gid] + [data[gid].get(k, "")
                               for k in list(ANNOTATION_FIELDS) + EXTRA_FIELDS]
                w.writerow(row)

        return os.path.abspath(out_path)


if __name__ == "__main__":
    store = AnnotationStore()
    print("标注文件：%s" % store.path)
    print("存在：%s" % os.path.exists(store.path))
    d = store.load()
    print("已标注组数：%d" % len(d))
    p = store.progress(245)
    print("进度：%s" % p)
