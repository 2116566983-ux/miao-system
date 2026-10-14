# -*- coding: utf-8 -*-
"""
生成流程编排：等待任务完成 → 取回图片 → 归档

为什么单独一个模块（而不是写在 streamlit_app.py 里）：
    streamlit_app.py 是脚本，一 import 就会执行整个界面，
    没法单独测里面的函数。而「等待 + 取回 + 归档」是纯业务逻辑，
    与界面无关，抽出来才能用普通单元测试覆盖。
"""

from __future__ import annotations

import time

# 默认最长等待时间（秒）
#
# 取值依据（服务器实测）：
#   模型已热：单图 1–4 秒
#   首次冷启动：含 4GB 模型加载约 85 秒
#   排队（多人共用）：可能数分钟
# 所以给到 240 秒，超时则转为「手动继续等待」，不直接判失败。
DEFAULT_MAX_WAIT = 240
DEFAULT_INTERVAL = 1.0   # 轮询间隔。实测出图 1–4 秒，1 秒粒度体验最好


class WaitResult(dict):
    """返回结果的薄包装，方便用属性访问。

    status 取值：
        done      成功（图片已取回并归档）
        failed    任务执行失败
        timeout   等待超时（任务可能仍在跑）
        no_output 任务完成但没返回图片
    """

    @property
    def ok(self):
        return self.get("status") == "done"

    def __getattr__(self, name):
        # 双下划线属性（__deepcopy__ 之类）直接交给默认行为，
        # 避免框架内部探测属性时绕进来
        if name.startswith("__"):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


def wait_and_archive(comfy, pend, upload_dir, archive_obj,
                     max_wait=DEFAULT_MAX_WAIT,
                     interval=DEFAULT_INTERVAL,
                     progress_cb=None):
    """等待 ComfyUI 任务完成，取回结果并归档。

    参数：
        comfy        ComfyClient 实例
        pend         任务信息 dict（需含 prompt_id / student_id /
                     sources / mode / prompt / negative / seed /
                     denoise / blend / workflow_file）
        upload_dir   图片落地目录
        archive_obj  WorkArchive 实例
        max_wait     最长等待秒数
        interval     轮询间隔秒数
        progress_cb  可选回调，签名 cb(已等待秒数, 队列信息dict)
                     界面用它显示进度；传 None 则静默等待

    返回 WaitResult
    """
    pid = pend["prompt_id"]
    t0 = time.time()
    status = None

    while time.time() - t0 < max_wait:
        status = comfy.poll(pid)
        if status.get("failed") or status.get("done"):
            break

        if progress_cb is not None:
            try:
                progress_cb(time.time() - t0, status.get("progress") or {})
            except Exception:
                pass
        time.sleep(interval)

    elapsed = time.time() - t0

    if status and status.get("failed"):
        return WaitResult(status="failed", elapsed=elapsed,
                          message="生成失败：%s" % status.get("error"))

    if not status or not status.get("done"):
        return WaitResult(status="timeout", elapsed=elapsed,
                          message="等待超过 %d 秒仍未完成。" % max_wait)

    paths = comfy.fetch_images(pid, upload_dir, prefix="gen")
    if not paths:
        return WaitResult(status="no_output", elapsed=elapsed,
                          message="任务完成但没有返回图片。")

    saved = archive_obj.save_output(
        archive_obj.new_work_id(pend["student_id"]), paths)
    wid = archive_obj.record(
        student_id=pend["student_id"],
        source_images=pend["sources"],
        mode=pend["mode"], prompt=pend["prompt"],
        negative_prompt=pend["negative"],
        seed=pend["seed"], denoise=pend["denoise"],
        blend=pend["blend"],
        model_or_workflow="ComfyUI",
        workflow_file=pend["workflow_file"],
        output_path=",".join(saved),
    )

    return WaitResult(status="done", elapsed=elapsed, work_id=wid,
                      paths=saved,
                      message="生成完成并已归档，作品编号 %s" % wid)


def describe_wait(status_dict):
    """把 poll 的返回整理成一句给用户看的话。"""
    if not status_dict:
        return "状态未知"
    if status_dict.get("failed"):
        return "生成失败：%s" % status_dict.get("error")
    if status_dict.get("done"):
        return "已完成"
    q = status_dict.get("progress") or {}
    if q.get("running") or q.get("pending"):
        return "排队中 · 运行 %s / 等待 %s" % (q.get("running", "?"),
                                              q.get("pending", "?"))
    return "已提交，等待开始"
