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
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import uuid

WORK_FIELDS = [
    "work_id", "student_id", "student_name", "task_id",
    "mode",                 # img2img / multi_motif
    "source_images",        # 逗号分隔的 image_id
    "group_ids",            # 逗号分隔的 group_id
    "prompt", "negative_prompt",
    "denoise", "blend", "seed",
    "model_or_workflow", "workflow_file",
    "created_at", "output_path",
    "review_status",        # 待审核 / 通过 / 需修改
    "review_note", "permission_note",
]


class WorkArchive:
    """作品归档：写 CSV 记录 + 保存图片文件。"""

    def __init__(self, csv_path, output_dir):
        self.csv_path = csv_path
        self.output_dir = output_dir
        os.makedirs(os.path.dirname(os.path.abspath(csv_path)), exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

    # ------------------------------------------------------------------
    def _ensure_header(self):
        if not os.path.exists(self.csv_path):
            with open(self.csv_path, "w", newline="", encoding="utf-8-sig") as f:
                csv.DictWriter(f, fieldnames=WORK_FIELDS).writeheader()

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
               review_status="待审核", review_note="",
               permission_note=""):
        """写入一条作品记录，返回 work_id。"""
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
            "review_status": review_status,
            "review_note": review_note,
            "permission_note": permission_note,
        }
        with open(self.csv_path, "a", newline="", encoding="utf-8-sig") as f:
            csv.DictWriter(f, fieldnames=WORK_FIELDS).writerow(row)
        return wid

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
        rows = []
        found = False
        with open(self.csv_path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                if r.get("work_id") == work_id:
                    r["review_status"] = status
                    r["review_note"] = note
                    found = True
                rows.append(r)
        if found:
            with open(self.csv_path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=WORK_FIELDS, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
        return found

    def load(self):
        if not os.path.exists(self.csv_path):
            return []
        with open(self.csv_path, encoding="utf-8-sig", newline="") as f:
            return [dict(r) for r in csv.DictReader(f)]


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
