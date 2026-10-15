# -*- coding: utf-8 -*-
"""安全写辅助 —— 作品归档和标注表共用的底座。

为什么要有这个文件
------------------
系统给一个班的学生同时用：30 个人在同一分钟提交作品、补填设计说明、
写互评。原来的写法是

    rows = 读整个 CSV
    改一行
    with open(path, "w") as f:    # ← 这一瞬间文件被截断成 0 字节
        写回全部行

这在一个人用时没问题，多人/多进程时就三处事故：

1. **丢更新** —— A 和 B 各自读了旧内容，各自写回，后写的把先写的盖掉。
   学生填了说明，刷新一看没了，是最容易被当成"系统不行"的那类 bug。
2. **断电留半张表** —— `open("w")` 截断之后到写完之间任何异常（异常、
   进程被杀、容器被回收），CSV 就是残的：最后一行截一半，`DictReader`
   读到半行直接抛错，整张表读不出来。
3. **没有互斥** —— 读改写不是原子操作，光靠 Python 层解决不了，必须落到
   文件锁上。

所以这里提供两件事：**文件锁** 和 **原子替换**。

两个不能省的细节
----------------
* **锁必须打在旁路文件上，不能打在数据表上。**
  原子替换会把 `xxx.csv` 换成一个全新的 inode；如果锁打在 `xxx.csv` 上，
  先来的进程锁住旧 inode、后来的进程写完后把文件替换掉，两个进程锁的
  根本不是同一个东西，互斥形同虚设。所以锁文件是 `xxx.csv.lock`，
  它从不参与替换，永远稳定。

* **锁文件用完不删。** 删掉会引入新竞争：进程 A 释放锁准备删除，
  进程 B 已经在等待同一个路径，A 删掉后 B 拿到的是一个全新 inode，
  而 A 已经释放 —— 于是 A、B 同时认为自己持有锁。留在磁盘上，
  代价只是一个空文件换来正确性。

跨平台
------
本机是 Windows（用 `msvcrt` 字节域锁），云端 Streamlit 是 Linux
（用 `fcntl.flock`）。两套都实现了，按运行环境自动选。
"""

from __future__ import annotations

import csv
import io
import os
import sys
import time
import uuid
from contextlib import contextmanager

_LOCK_TIMEOUT = 15.0
# 抢占锁失败后的重试间隔。0.02s 起步，指数退避到 0.2s 封顶 ——
# 学生提交是秒级事件，没必要让它一分钟内狂转几十万次。
_RETRY_BASE = 0.02
_RETRY_MAX = 0.2


# ---------------------------------------------------------------------------
# 平台相关的底层加锁
# ---------------------------------------------------------------------------

if sys.platform.startswith("win"):  # pragma: no cover - 平台分支
    import msvcrt

    def _lock_file(fileno, exclusive):
        """Windows 字节域锁。

        msvcrt 只提供字节域锁；这里锁第 0 字节起的 1 字节，当作整个文件
        的信号量。LK_NB* 是非阻塞版，失败抛 OSError，由上层退避重试兜住。
        """
        # 注意 LK_NBLCK(独占) 与 LK_NBRLCK(共享) 不要写混 —— 写混会让
        # "独占" 退化成共享，互斥直接失效。
        mode = msvcrt.LK_NBLCK if exclusive else msvcrt.LK_NBRLCK
        msvcrt.locking(fileno, mode, 1)

    def _unlock_file(fileno):
        try:
            msvcrt.locking(fileno, msvcrt.LK_UNLCK, 1)
        except OSError:
            # 释放失败通常意味着这段区划已不在我们手里；
            # 调用方马上就要关 fd 了，兜住不让异常盖掉业务错误。
            pass

elif os.name == "posix":  # pragma: no cover - 平台分支
    import fcntl

    def _lock_file(fileno, exclusive):
        fcntl.flock(fileno, fcntl.LOCK_EX | fcntl.LOCK_NB if exclusive
                    else fcntl.LOCK_SH | fcntl.LOCK_NB)

    def _unlock_file(fileno):
        try:
            fcntl.flock(fileno, fcntl.LOCK_UN)
        except OSError:
            pass

else:  # pragma: no cover - 兜底分支

    def _lock_file(fileno, exclusive):
        return None

    def _unlock_file(fileno):
        return None


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

@contextmanager
def _retry_open(lock_path, timeout):
    """反复尝试打开锁文件，直到成功或超时。见下方 file_lock 里的说明。"""
    waited, delay = 0.0, _RETRY_BASE
    while True:
        try:
            fd = os.open(lock_path,
                         os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0))
            yield fd
            return
        except OSError:
            if waited >= timeout:
                raise TimeoutError(
                    "反复打开锁文件失败 %.1fs：%s" % (timeout, lock_path))
            time.sleep(delay)
            waited += delay
            delay = min(delay * 1.6, _RETRY_MAX)


@contextmanager
def file_lock(path, exclusive=True, timeout=_LOCK_TIMEOUT):
    """对一个文件加互斥锁，保护「读进来改写再写回」这段临界区。

    用法：

        with file_lock(CSV_PATH):
            rows = read_csv_rows(CSV_PATH)
            ... 改 ...
            atomic_write_csv(CSV_PATH, FIELDS, rows)

    timeout 用尽还没抢到锁时抛 TimeoutError。宁可报错也不要带着
    "以为写成功了"的状态继续 —— 假成功比失败更难查。
    """
    lock_path = str(path) + ".lock"
    directory = os.path.dirname(os.path.abspath(lock_path))
    if directory:
        os.makedirs(directory, exist_ok=True)

    # os.open 也要重试：Windows 上创建一个新文件后，杀软/MS Defender 会立刻
    # 扫它，这一瞬间再开就是 PermissionError(Errno 13)。这个错跟"锁被别人
    # 占着"无关，所以只重试加锁、不重试打开是不够的 —— 本轮 -prove 对照实验
    # 里 10 线程并发抢同一把锁时就复现了：200 条丢到 120 条才发现这层。
    with _retry_open(lock_path, timeout) as fd:
        got = False
        try:
            waited, delay = 0.0, _RETRY_BASE
            while True:
                try:
                    _lock_file(fd, exclusive)
                    got = True
                    break
                except OSError:
                    if waited >= timeout:
                        raise TimeoutError(
                            "等待文件锁超时 %.1fs：%s" % (timeout, path))
                    time.sleep(delay)
                    waited += delay
                    delay = min(delay * 1.6, _RETRY_MAX)
            yield
        finally:
            if got:
                _unlock_file(fd)
            os.close(fd)
            # 有意不删除 lock_path —— 理由见模块文档注释。


def read_csv_rows(path, fieldnames=None):
    """读 CSV 成 [{列: 值}]，不存在时返回 []。

    用 utf-8-sig：不少 CSV 是从 Excel 另存出来的，带 UTF-8 BOM。漏了这一步，
    首列名会被读成 "\ufeffwork_id"，然后所有按 'work_id' 取值的代码全线
    拿到 None —— 症状是"数据莫名其妙全空"，很难往编码上想。
    """
    if not os.path.exists(path):
        return []
    with io.open(path, encoding="utf-8-sig", newline="") as f:
        rows = []
        for raw in csv.DictReader(f):
            row = dict(raw)
            # DictReader 会把多出来的列塞到 row[None]（值是列表）。这里直接丢掉，
            # 后续 atomic_write_csv 用 extrasaction="ignore" 也不会写出去。
            row.pop(None, None)
            # 补齐旧表缺的列。不补的话，重写时这些列会被整体漏掉。
            if fieldnames:
                for k in fieldnames:
                    row.setdefault(k, "")
            rows.append(row)
        return rows


def atomic_write_csv(path, fieldnames, rows):
    """整表重写，但对读者而言永不见"半张表"。

    做法是写到同目录的临时文件，刷盘，再用 os.replace 原子地把临时文件
    换成正主。os.replace 在同目录是原子操作：任何一个读者要么看到旧表，
    要么看到新表，不存在中间态。这也是这里**不需要**先 truncate 的原因
    —— 原来的写法（open('w') 先清空）正是把窗口开到最大的那个动作。

    返回写入的行数。
    """
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)

    tmp = "%s.tmp_%s" % (path, uuid.uuid4().hex[:8])
    try:
        with io.open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames,
                               extrasaction="ignore")
            w.writeheader()
            for r in rows:
                if fieldnames:
                    clean = {k: (r.get(k, "") if r.get(k) is not None else "")
                             for k in fieldnames}
                else:
                    clean = r
                w.writerow(clean)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return len(rows)
    except BaseException:
        # 写失败了务必把临时文件收走，否则下次 os.replace 会盖掉残片、
        # 也会在目录里留一堆 tmp 垃圾。
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
