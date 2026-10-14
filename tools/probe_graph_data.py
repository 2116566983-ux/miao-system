# -*- coding: utf-8 -*-
"""探查图数据库相关 CSV 的结构与规模。"""
import csv
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

FILES = [
    "graph/csv/nodes_groups.csv",
    "graph/csv/nodes_images.csv",
    "graph/csv/rels_derived.csv",
    "metadata/images.csv",
    "metadata/works.csv",
    "annotation/annotation_sheet.csv",
]

for f in FILES:
    if not os.path.exists(f):
        print("%-34s (不存在)" % f)
        print()
        continue
    try:
        with io.open(f, encoding="utf-8-sig", newline="") as fh:
            r = csv.reader(fh)
            hdr = next(r)
            rows = list(r)
        print("%-34s %5d 行" % (f, len(rows)))
        print("   列(%d): %s" % (len(hdr), ", ".join(hdr)))
        if rows:
            print("   首行: %s" % ", ".join(
                (x[:18] + ".." if len(x) > 18 else x) for x in rows[0]))
    except Exception as e:
        print("%-34s 错误 %s" % (f, e))
    print()
