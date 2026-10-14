# -*- coding: utf-8 -*-
"""
清理元数据中的个人路径，便于分发

为什么需要：
    metadata/images.csv 里的 file_path 列记录的是生成时那台机器的绝对路径
    （例如 C:/Users/某某/Desktop/...）。把项目交给别人后，这些路径全都无效，
    留着只会让人困惑，还可能泄露你的目录结构。

处理方式：
    把 file_path 列清空。这对系统没有影响 ——
    界面会用「图片根目录 + file_name」实时拼出路径，
    file_path 只是生成时的记录。

用法：
    python tools/clean_paths.py            # 处理 metadata/images.csv（原地备份）
    python tools/clean_paths.py --check    # 只检查，不修改
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CSV = os.path.join(ROOT, "metadata", "images.csv")


def check(path):
    """检查是否存在机器相关的绝对路径。"""
    if not os.path.exists(path):
        print("[错误] 文件不存在：%s" % path)
        return 1

    with io.open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print("[警告] 文件为空")
        return 1

    field = "file_path"
    if field not in rows[0]:
        print("[提示] 没有 file_path 列，无需处理")
        return 0

    samples = [r.get(field, "") for r in rows]
    non_empty = [s for s in samples if s.strip()]

    print("文件：%s" % path)
    print("总行数：%d" % len(rows))
    print("file_path 非空：%d 行" % len(non_empty))

    if non_empty:
        print()
        print("样例（前 3 条）：")
        for s in non_empty[:3]:
            print("   %s" % s)
        print()
        print("[建议] 分发前应清空该列：python tools/clean_paths.py")
        return 1

    print()
    print("[OK] file_path 已为空，可以直接分发")
    return 0


def clean(path):
    """清空 file_path 列，并保留备份。"""
    if not os.path.exists(path):
        print("[错误] 文件不存在：%s" % path)
        return 1

    with io.open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    if "file_path" not in fieldnames:
        print("[提示] 没有 file_path 列，无需处理")
        return 0

    # 备份
    bak = path + ".bak"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
        print("已备份原文件：%s" % bak)
    else:
        print("备份已存在，跳过备份：%s" % bak)

    # 清空
    changed = 0
    for r in rows:
        if r.get("file_path", "").strip():
            r["file_path"] = ""
            changed += 1

    with io.open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    print("已清空 %d 行的 file_path" % changed)
    print()
    print("说明：系统运行不依赖这一列 ——")
    print("      界面用「图片根目录 + file_name」实时拼路径。")
    print("      别人拿到后需重新生成元数据：")
    print("      python build_metadata.py --src \"图片目录\" --out metadata/images.csv")
    return 0


def main():
    ap = argparse.ArgumentParser(description="清理元数据中的个人路径")
    ap.add_argument("--csv", default=DEFAULT_CSV, help="元数据 CSV 路径")
    ap.add_argument("--check", action="store_true", help="只检查不修改")
    ap.add_argument("--all", action="store_true",
                    help="同时处理 annotation/ 下的 CSV")
    args = ap.parse_args()

    targets = [args.csv]
    if args.all:
        ann = os.path.join(ROOT, "annotation")
        for name in ("annotation_sheet.csv", "annotation_sheet_slim.csv"):
            p = os.path.join(ann, name)
            if os.path.exists(p):
                targets.append(p)

    rc = 0
    for t in targets:
        if args.check:
            rc |= check(t)
        else:
            rc |= clean(t)
        print()
        print("-" * 60)

    return rc


if __name__ == "__main__":
    sys.exit(main())
