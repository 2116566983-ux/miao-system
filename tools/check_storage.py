# -*- coding: utf-8 -*-
"""storage.py 冒烟测试：锁、原子替换、并发不丢更新。"""
import csv
import io
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import storage

FIELDS = ["work_id", "note"]


def show(name, ok, extra=""):
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))


def main():
    tmpdir = tempfile.mkdtemp(prefix="storage_smoke_")
    path = os.path.join(tmpdir, "works.csv")
    print("临时目录：", tmpdir)

    # 1. 读不存在的文件
    show("读不存在的文件返回空列表", storage.read_csv_rows(path) == [])

    # 2. 原子写后可读回
    storage.atomic_write_csv(path, FIELDS, [{"work_id": "W1", "note": "甲"}])
    rows = storage.read_csv_rows(path, FIELDS)
    show("原子写后可读回", len(rows) == 1 and rows[0]["work_id"] == "W1",
         str(rows))

    # 3. 写太多列不会被写出去（extrasaction=ignore）
    storage.atomic_write_csv(path, FIELDS,
                             [{"work_id": "W1", "note": "甲", "junk": "x"}])
    with io.open(path, encoding="utf-8-sig", newline="") as f:
        header = next(csv.reader(f))
    show("多余列被丢弃", header == FIELDS, str(header))

    # 4. 原子替换不留临时文件
    leftovers = [n for n in os.listdir(tmpdir) if ".tmp_" in n]
    show("没有遗留临时文件", leftovers == [], str(leftovers))

    # 5. 锁本身能拿到并能释放
    with storage.file_lock(path):
        pass
    show("加锁释放不报错", True)

    # 6. 并发：10 个线程各自追加 20 条 —— 不加 lose 判断需要提供统一的
    #    读改写函数，这里直接套 storage 的锁模拟业务中的 update 流程。
    storage.atomic_write_csv(path, FIELDS, [])
    N_THREADS, N_EACH = 10, 20

    def worker(tid):
        for i in range(N_EACH):
            with storage.file_lock(path):
                rows = storage.read_csv_rows(path, FIELDS)
                rows.append({"work_id": "W%s_%d" % (tid, i), "note": str(tid)})
                storage.atomic_write_csv(path, FIELDS, rows)

    ts = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    rows = storage.read_csv_rows(path, FIELDS)
    expect = N_THREADS * N_EACH
    show("并发 %d 线程各追加 %d 条，无丢更新" % (N_THREADS, N_EACH),
         len(rows) == expect, "实际 %d / 应为 %d" % (len(rows), expect))

    ids = [r["work_id"] for r in rows]
    show("无重复行", len(set(ids)) == len(ids),
         "重复 %d 条" % (len(ids) - len(set(ids))))

    # 7. 一行也不残 —— 每行都有 work_id
    show("没有残缺行", all(r["work_id"] for r in rows))

    print("\n并发结果：%d 行，线程数 %d" % (len(rows), N_THREADS))
    return 0 if len(rows) == expect else 1


if __name__ == "__main__":
    sys.exit(main())
