# -*- coding: utf-8 -*-
"""模拟一个班同时在线操作，压存储层，回答「能不能撑 30–50 人」。

为什么压存储层而不是压生图接口
------------------------------
生图走的是外部大模型 API，压它既要花钱又会被限流，测出来的数还不代表
我们系统的能力。真正由**我们自己**承担、且全班共享的瓶颈是存储：
`storage` 用的是「文件锁 + CSV 原子替换」，50 个人同时保存作品时
写入会被串行排队，这才是可能卡住的地方。

一个学生的一次完整操作按真实流程建模：
    读作品列表 → 读自己的记录 → 保存一次新作品 → 再读一次列表
其中"保存"是读-改-写，会独占文件锁；"读"可以并发。

生图本身的耗时（约 5 秒/张）单独用排队公式推算，不混进实测里，
报告里分开写，避免把估算当实测。

用法：
    python tools/load_test.py --users 30
    python tools/load_test.py --users 50 --rounds 3
"""

from __future__ import annotations

import argparse
import io
import os
import statistics
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import storage  # noqa: E402

FIELDS = ["work_id", "student", "round", "note"]
PRELOAD = 200          # 预置历史作品条数，模拟学期中已有数据


def _preload(path, n=PRELOAD):
    rows = [{"work_id": "W%04d" % i, "student": "S%02d" % (i % 60),
             "round": "0", "note": "历史作品"} for i in range(n)]
    storage.atomic_write_csv(path, FIELDS, rows)


def _pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def run(users, rounds=1, verbose=True, mode="rewrite"):
    tmp = tempfile.mkdtemp(prefix="loadtest_")
    path = os.path.join(tmp, "works.csv")
    _preload(path, PRELOAD)

    read_ms, write_ms = [], []
    errors = []
    lock = threading.Lock()
    barrier = threading.Barrier(users)     # 让所有人在同一刻起跑

    def one_user(uid):
        try:
            barrier.wait(timeout=30)       # 同时冲进来，模拟上课齐点"生成"
        except Exception:
            pass
        for r in range(rounds):
            try:
                t0 = time.perf_counter()
                storage.read_csv_rows(path, FIELDS)
                t1 = time.perf_counter()

                rec = {"work_id": "W%s_%d_%d" % (uid, r, time.time_ns()),
                       "student": "S%03d" % uid, "round": str(r),
                       "note": "新作品"}
                if mode == "append":
                    # 保存一件作品只是多一行，不该读全表再整表重写
                    storage.append_csv_row(path, FIELDS, rec)
                else:
                    with storage.file_lock(path):
                        rows = storage.read_csv_rows(path, FIELDS)
                        rows.append(rec)
                        storage.atomic_write_csv(path, FIELDS, rows)
                t2 = time.perf_counter()

                storage.read_csv_rows(path, FIELDS)
                t3 = time.perf_counter()

                with lock:
                    read_ms.append((t1 - t0) * 1000)
                    read_ms.append((t3 - t2) * 1000)
                    write_ms.append((t2 - t1) * 1000)
            except Exception as e:          # noqa: BLE001
                with lock:
                    errors.append("%s#%d: %s" % (uid, r, str(e)[:50]))

    t_start = time.perf_counter()
    ts = [threading.Thread(target=one_user, args=(i,)) for i in range(users)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    elapsed = time.perf_counter() - t_start

    rows = storage.read_csv_rows(path, FIELDS)
    expect = PRELOAD + users * rounds
    lost = expect - len(rows)
    ids = [x["work_id"] for x in rows]
    dup = len(ids) - len(set(ids))

    if verbose:
        print("用户数 %d，每人 %d 轮，保存方式 %s" % (users, rounds, mode))
        print("总耗时 %.2f 秒" % elapsed)
        print("读  次数 %d  P50 %6.1f ms  P95 %6.1f ms  P99 %6.1f ms"
              % (len(read_ms), _pct(read_ms, 50), _pct(read_ms, 95),
                 _pct(read_ms, 99)))
        print("写  次数 %d  P50 %6.1f ms  P95 %6.1f ms  P99 %6.1f ms  (最大 %.0f)"
              % (len(write_ms), _pct(write_ms, 50), _pct(write_ms, 95),
                 _pct(write_ms, 99), max(write_ms) if write_ms else 0))
        print("写入吞吐 %.1f 次/秒" % (len(write_ms) / elapsed if elapsed else 0))
        print("错误 %d 条%s" % (len(errors), ("：" + errors[0]) if errors else ""))
        print("数据校验：应有 %d 行，实得 %d 行，丢 %d 行，重复 %d 行"
              % (expect, len(rows), lost, dup))
        verdict = ("通过" if (lost == 0 and dup == 0 and not errors
                              and _pct(write_ms, 95) < 2000) else "未通过")
        print("结论：%s" % verdict)

    return {"users": users, "rounds": rounds, "elapsed": elapsed,
            "read_p50": _pct(read_ms, 50), "read_p95": _pct(read_ms, 95),
            "read_p99": _pct(read_ms, 99),
            "write_p50": _pct(write_ms, 50), "write_p95": _pct(write_ms, 95),
            "write_p99": _pct(write_ms, 99),
            "write_max": max(write_ms) if write_ms else 0,
            "throughput": len(write_ms) / elapsed if elapsed else 0,
            "errors": len(errors), "lost": lost, "dup": dup,
            "expect": expect, "got": len(rows)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=30)
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--mode", choices=["rewrite", "append"], default="rewrite",
                    help="rewrite=现状(整表重写)；append=追加写")
    args = ap.parse_args()
    r = run(args.users, args.rounds, mode=args.mode)
    return 0 if (r["lost"] == 0 and r["dup"] == 0 and r["errors"] == 0) else 1


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    raise SystemExit(main())
