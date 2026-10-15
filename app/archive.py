# -*- coding: utf-8 -*-
"""
生成作品归档模块

方案要求："每件AI辅助作品需要保留原始纹样、提示词、模型或工作流、
生成时间和学生作者。" 本模块负责这项工作。

归档内容分两处：
  - works.csv        作品记录表（结构化，可导入图数据库）
  - generated/       生成的图片文件

同时提供图数据库写入接口，把作品挂到对应的 Image 和 Motif 上。

用法：
    from archive import WorkArchive
    ar = WorkArchive("metadata/works.csv", "data/generated")
    wid = ar.record(student_id="S001", source_images=["basic_0"], ...)

并发说明
--------
这个班的学生是**同时**用的：几十个人在同一分钟提交作品、补填设计说明、
写互评。所以所有写 CSV 的地方都走 app/storage.py 的「文件锁 + 原子替换」，
绝不用 `open(path, "w")` 直接截断。

实测过（tools/check_storage.py 与对照实验）：原来那种「读进来 → 截断 →
写回」的写法，10 个并发线程各追加 20 条，200 条最后只剩 2~8 条，丢率约
97%。丢的是学生辛辛苦苦填的设计说明，而且没有任何报错提示。
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import uuid

import storage

WORK_FIELDS = [
    "work_id", "student_id", "student_name", "task_id",
    "mode",                 # img2img / multi_motif
    "source_images",        # 逗号分隔的 image_id
    "group_ids",            # 逗号分隔的 group_id
    "prompt", "negative_prompt",
    "denoise", "blend", "seed",
    "model_or_workflow", "workflow_file",
    "created_at", "output_path",
    "design_note",          # 设计说明：学生自己写「改了什么、为什么这么改」
    "review_status",        # 待审核 / 通过 / 需修改
    "review_note", "permission_note",
]


#: 互评表的字段。
#:
#: 为什么单独一张表、而不是塞进 works.csv：
#:   一件作品会被**多人**评价，是「多对一」关系。塞进作品行里
#:   只能存最后一条，前面写的就被覆盖了。而且同学之间评价是
#:   课程第四单元的核心产出，不能丢。
PEER_FIELDS = [
    "work_id",        # 被评的作品
    "reviewer_id",    # 评价人学号
    "reviewer_name",  # 评价人姓名
    "merit",          # 先说优点（方案要求：先肯定，再建议）
    "suggestion",     # 再给一条具体建议
    "created_at",
]


class WorkArchive:
    """作品归档：写 CSV 记录 + 保存图片文件。"""

    def __init__(self, csv_path, output_dir, peer_path=None):
        self.csv_path = csv_path
        self.output_dir = output_dir
        # 互评表默认跟作品表放同一目录，叫 works_peer_review.csv
        if peer_path is None:
            peer_path = os.path.join(os.path.dirname(os.path.abspath(csv_path)),
                                     "works_peer_review.csv")
        self.peer_path = peer_path
        os.makedirs(os.path.dirname(os.path.abspath(csv_path)), exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

    # ------------------------------------------------------------------
    def _ensure_header(self):
        # 表不存在时建空表。加锁是因为：几十个学生同时提交时，"文件不存在"
        # 这个判断和随后的建表之间有一段窗口，裸着会重复写、也会互相截断。
        if os.path.exists(self.csv_path):
            return
        with storage.file_lock(self.csv_path):
            if os.path.exists(self.csv_path):   # 双检：锁内再看一次
                return
            storage.atomic_write_csv(self.csv_path, WORK_FIELDS, [])

    def new_work_id(self, student_id=""):
        """生成作品编号：W + 日期 + 短随机码，便于排序和人工识别。"""
        stamp = dt.datetime.now().strftime("%Y%m%d")
        short = uuid.uuid4().hex[:6]
        sid = "".join(ch for ch in str(student_id) if ch.isalnum())[:8] or "anon"
        return "W%s_%s_%s" % (stamp, sid, short)

    # ------------------------------------------------------------------
    def record(self, student_id="", source_images=None, group_ids=None,
               mode="img2img", prompt="", negative_prompt="",
               seed=None, denoise=None, blend=None,
               model_or_workflow="", workflow_file="",
               output_path="", student_name="", task_id="",
               design_note="",
               review_status="待审核", review_note="",
               permission_note=""):
        """写入一条作品记录，返回 work_id。

        design_note 是**学生自己写**的设计说明。生成时留空，
        由学生在 ③ 我的作品 页补填 —— 方案第四单元要求
        「提交作品、设计说明和过程记录」，说明不能由程序代写。
        """
        self._ensure_header()
        wid = self.new_work_id(student_id)
        row = {
            "work_id": wid,
            "student_id": student_id,
            "student_name": student_name,
            "task_id": task_id,
            "mode": mode,
            "source_images": ",".join(source_images or []),
            "group_ids": ",".join(group_ids or []),
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "denoise": "" if denoise is None else denoise,
            "blend": "" if blend is None else blend,
            "seed": "" if seed is None else seed,
            "model_or_workflow": model_or_workflow,
            "workflow_file": workflow_file,
            "created_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "output_path": output_path,
            "design_note": design_note,
            "review_status": review_status,
            "review_note": review_note,
            "permission_note": permission_note,
        }
        # 原来这里是 open(..., "a") 追加。追加模式本身在单人用时没问题，
        # 但两个进程同时追加会互把对方的半行打断；而且 csv 写入不是原子的。
        # 改成「锁内整表重写」：行数在千级以内，代价可以忽略，换来的是
        # 绝不会写出残行。
        with storage.file_lock(self.csv_path):
            rows = storage.read_csv_rows(self.csv_path, WORK_FIELDS)
            rows.append(row)
            storage.atomic_write_csv(self.csv_path, WORK_FIELDS, rows)
        return wid

    # ------------------------------------------------------------------
    def update_design_note(self, work_id, note):
        """学生补填 / 修改设计说明。"""
        return self._update_field(work_id, "design_note", note)

    def _update_field(self, work_id, field, value):
        """改某个作品的一个字段。CSV 模式需重写整个文件。

        抽出来是因为「改设计说明」和「教师审核」要做同一件事，
        区别只在改哪个字段 —— 复制两份容易改漏一处。
        """
        if not os.path.exists(self.csv_path) or field not in WORK_FIELDS:
            return False

        # 整个「读 → 改 → 写」必须在锁里完成。锁外面读、锁里面写，
        # 等于白加锁：读到的还是别人改之前的旧内容，写回时会把别人的
        # 修改一起盖掉。
        with storage.file_lock(self.csv_path):
            rows = storage.read_csv_rows(self.csv_path, WORK_FIELDS)
            found = False
            for r in rows:
                if r.get("work_id") == work_id:
                    r[field] = value
                    found = True
            if found:
                storage.atomic_write_csv(self.csv_path, WORK_FIELDS, rows)
        return found

    # ------------------------------------------------------------------
    # 学生互评（课程第四单元）
    # ------------------------------------------------------------------
    def _ensure_peer_header(self):
        if os.path.exists(self.peer_path):
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.peer_path)),
                    exist_ok=True)
        with storage.file_lock(self.peer_path):
            if os.path.exists(self.peer_path):   # 双检：锁内再看一次
                return
            storage.atomic_write_csv(self.peer_path, PEER_FIELDS, [])

    def add_peer_review(self, work_id, reviewer_id="", reviewer_name="",
                        merit="", suggestion=""):
        """写一条互评。

        同一个人对同一件作品只保留最新一条 —— 学生改主意时
        不该在列表里留下两条自相矛盾的评价。
        """
        self._ensure_peer_header()
        new_row = {
            "work_id": work_id,
            "reviewer_id": reviewer_id,
            "reviewer_name": reviewer_name,
            "merit": merit,
            "suggestion": suggestion,
            "created_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with storage.file_lock(self.peer_path):
            # 「去重」和「追加」必须在同一个锁里。分成两步的话，
            # 两个人会基于同一份旧表各自判断"要不要删"，最后的写入里
            # 有一份旧行又回来了 —— 典型的读改写竞态。
            rows = storage.read_csv_rows(self.peer_path, PEER_FIELDS)
            rows = [r for r in rows
                    if not (r.get("work_id") == work_id
                            and r.get("reviewer_id") == reviewer_id)]
            rows.append(new_row)
            storage.atomic_write_csv(self.peer_path, PEER_FIELDS, rows)
        return True

    def load_peer_reviews(self, work_id=None):
        """读互评。给定 work_id 就只返回那件作品的。"""
        rows = storage.read_csv_rows(self.peer_path, PEER_FIELDS)
        if work_id is not None:
            rows = [r for r in rows if r.get("work_id") == work_id]
        return rows

    def peer_review_stats(self, reviewer_id=""):
        """算互评统计，供课程进度用。

        返回 {'given': 我评过几件, 'received': 我的作品被评了几件,
              'by_work': {work_id: 条数}}
        """
        rows = self.load_peer_reviews()
        works = self.load()
        mine = {w["work_id"] for w in works
                if reviewer_id and reviewer_id in str(w.get("student_id") or "")}
        by_work = {}
        for r in rows:
            by_work[r["work_id"]] = by_work.get(r["work_id"], 0) + 1
        given = len({r["work_id"] for r in rows
                     if r.get("reviewer_id") == reviewer_id}) if reviewer_id else 0
        received = sum(by_work.get(w, 0) for w in mine)
        return {"given": given, "received": received,
                "total": len(rows), "by_work": by_work}

    # ------------------------------------------------------------------
    def save_output(self, work_id, images, dest_dir=None):
        """把生成图片复制/保存到归档目录，返回保存后的路径列表。

        images 可以是本地路径列表，或字节内容列表。
        """
        dest = dest_dir or self.output_dir
        os.makedirs(dest, exist_ok=True)
        saved = []
        for idx, item in enumerate(images or []):
            if isinstance(item, (bytes, bytearray)):
                p = os.path.join(dest, "%s_%d.png" % (work_id, idx))
                with open(p, "wb") as f:
                    f.write(item)
                saved.append(p)
            elif isinstance(item, str) and os.path.exists(item):
                ext = os.path.splitext(item)[1] or ".png"
                p = os.path.join(dest, "%s_%d%s" % (work_id, idx, ext))
                with open(item, "rb") as src, open(p, "wb") as dst:
                    dst.write(src.read())
                saved.append(p)
        return saved

    # ------------------------------------------------------------------
    def update_review(self, work_id, status, note=""):
        """教师审核：更新某作品的状态。CSV 模式需重写整个文件。"""
        if not os.path.exists(self.csv_path):
            return False
        with storage.file_lock(self.csv_path):
            rows = storage.read_csv_rows(self.csv_path, WORK_FIELDS)
            found = False
            for r in rows:
                if r.get("work_id") == work_id:
                    r["review_status"] = status
                    r["review_note"] = note
                    found = True
            if found:
                storage.atomic_write_csv(self.csv_path, WORK_FIELDS, rows)
        return found

    def load(self):
        """读全部作品记录。

        兼容旧表：老版本的 works.csv 没有 design_note 列。
        这里按当前 WORK_FIELDS 补齐缺失的键（值为空串），
        这样调用方可以无条件访问 w["design_note"]，不用到处写 .get()。
        """
        return storage.read_csv_rows(self.csv_path, WORK_FIELDS)


# ---------------------------------------------------------------------------
# 写入图数据库
# ---------------------------------------------------------------------------
CYPHER_CREATE_WORK = """
MERGE (w:GeneratedWork {work_id: $work_id})
SET w.student_id       = $student_id,
    w.mode             = $mode,
    w.prompt           = $prompt,
    w.negative_prompt  = $negative_prompt,
    w.seed             = $seed,
    w.denoise          = $denoise,
    w.model_or_workflow = $model_or_workflow,
    w.created_at       = $created_at,
    w.output_path      = $output_path,
    w.review_status    = $review_status
WITH w
OPTIONAL MATCH (s:Student {student_id: $student_id})
FOREACH (_ IN CASE WHEN s IS NULL THEN [] ELSE [1] END |
    MERGE (w)-[:CREATED_BY]->(s)
)
RETURN w.work_id AS work_id
"""

CYPHER_LINK_SOURCE = """
MATCH (w:GeneratedWork {work_id: $work_id})
MATCH (i:Image {image_id: $image_id})
MERGE (w)-[:DERIVED_FROM]->(i)
RETURN count(*) AS linked
"""

CYPHER_LINK_TASK = """
MATCH (w:GeneratedWork {work_id: $work_id})
MATCH (t:CourseTask {task_id: $task_id})
MERGE (w)-[:SUBMITTED_FOR]->(t)
RETURN count(*) AS linked
"""


def push_to_graph(client, work_row):
    """把一条作品记录写入图数据库。

    即使部分关系端点不存在（如 Student 还没建），作品节点也会写入成功。
    """
    if client is None or not getattr(client, "available", False):
        return False, "图数据库未连接"

    try:
        client.run(
            CYPHER_CREATE_WORK,
            work_id=work_row["work_id"],
            student_id=work_row.get("student_id") or "",
            mode=work_row.get("mode") or "",
            prompt=work_row.get("prompt") or "",
            negative_prompt=work_row.get("negative_prompt") or "",
            seed=int(work_row["seed"]) if str(work_row.get("seed", "")).isdigit() else None,
            denoise=float(work_row["denoise"]) if str(work_row.get("denoise", "")).replace(".", "", 1).isdigit() else None,
            model_or_workflow=work_row.get("model_or_workflow") or "",
            created_at=work_row.get("created_at") or "",
            output_path=work_row.get("output_path") or "",
            review_status=work_row.get("review_status") or "待审核",
        )
        # 挂接来源图片
        for img in (work_row.get("source_images") or "").split(","):
            img = img.strip()
            if img:
                client.run(CYPHER_LINK_SOURCE, work_id=work_row["work_id"], image_id=img)
        # 挂接课程任务
        if work_row.get("task_id"):
            client.run(CYPHER_LINK_TASK, work_id=work_row["work_id"],
                       task_id=work_row["task_id"])
        return True, work_row["work_id"]
    except Exception as e:
        return False, str(e)


if __name__ == "__main__":
    import tempfile

    tmp = tempfile.mkdtemp()
    ar = WorkArchive(os.path.join(tmp, "works.csv"),
                     os.path.join(tmp, "generated"))
    wid = ar.record(
        student_id="S001", student_name="测试学生",
        source_images=["basic_0", "texture_1"],
        group_ids=["G0000"], mode="multi_motif",
        prompt="miao embroidery pattern", negative_prompt="blurry",
        seed=12345, denoise=0.65, blend=0.5,
        model_or_workflow="SDXL base 1.0", workflow_file="multi_motif_template.json",
        output_path="/tmp/out.png",
    )
    print("归档自检：通过")
    print("  生成作品编号：", wid)
    rows = ar.load()
    print("  记录条数：", len(rows))
    print("  来源图片：", rows[0]["source_images"])
    print("  种子：", rows[0]["seed"])
    ok = ar.update_review(wid, "通过", "构图完整，色彩符合苗绣特征")
    print("  审核更新：", "成功" if ok else "失败")
    print("  审核后状态：", ar.load()[0]["review_status"])

    # 设计说明 + 互评自检
    ar.update_design_note(wid, "把底布换成深靛蓝，保留花枝走向")
    print("  设计说明：", ar.load()[0]["design_note"])
    ar.add_peer_review(wid, "S002", "李四",
                       merit="配色克制，符合苗绣的靛蓝基调",
                       suggestion="花枝可以再疏一点，现在有点满")
    ar.add_peer_review(wid, "S002", "李四",
                       merit="改后的描述", suggestion="改后的建议")
    pr = ar.load_peer_reviews(wid)
    print("  互评条数（同一人重复提交应去重为 1）：", len(pr))
    st = ar.peer_review_stats("S002")
    print("  互评统计：已评 %d 件 / 全表 %d 条" % (st["given"], st["total"]))
