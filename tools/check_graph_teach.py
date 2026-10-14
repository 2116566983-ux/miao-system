# -*- coding: utf-8 -*-
"""验证图教学模块的 CSV 引擎。"""
import io
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, ROOT)

import graph_teach as gt   # noqa: E402

t = time.time()
g = gt.build_csv_graph(ROOT)
print("构建耗时 %.2fs" % (time.time() - t))
if g is None:
    print("构建失败")
    raise SystemExit(1)

s = g.stats()
print("节点 %d, 关系 %d" % (s["nodes"], s["rels"]))
print("按标签:", s["by_label"])
print("按关系:", s["by_rel"])
print()

for p in gt.QUERY_PRESETS:
    print("--- %s ---" % p["name"])
    print("  问题:", p["question"])
    rows = g.run_preset(p["id"], p.get("params_hint"))
    print("  返回 %d 行" % len(rows))
    for r in rows[:3]:
        print("    ", r)
    print()
