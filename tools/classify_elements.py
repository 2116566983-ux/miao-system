# -*- coding: utf-8 -*-
"""按老师要求的三条美学轴给元素分类：对称美 / 流线型 / 黄金分割。

判据直接复用 `classify_aesthetics`，不另写一套 —— 那套已经在 1000 张实物照片上
跑过、阈值是照着实测分布定的。这里只是把输入从「绣片照片」换成「纯色底元素图」。

输出两份：
  1. `元素美学分类.csv`：每个元素的三轴等级 + 底层数值
  2. 三个分组目录（按对称美 / 按流线型 / 按黄金分割），每个元素被拷三份，
     三个视角各看一遍

为什么要分三个目录而不是一个
    一条轴定不了类。一朵团花可能「放射对称 + 流线低 + 黄金比中等」，
    按任何单一标签归档都会丢掉另外两条信息。三个目录各放一遍，
    谁要用哪个视角都能直接翻。
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

from classify_aesthetics import (a_metrics, sym_label, band,  # noqa: E402
                                 tercile)

AXES = [
    ("按对称美", "sym_label", "对称类型"),
    ("按流线型", "stream_label", "流线强度"),
    ("按黄金分割", "golden_label", "黄金比贴合度"),
]


def classify(files, base):
    rows = []
    for i, f in enumerate(files, 1):
        try:
            r = a_metrics(os.path.join(base, f))
        except Exception as e:                        # noqa: BLE001
            print("  [跳过] %s：%s" % (f[:40], str(e)[:40]))
            continue
        r["file"] = f
        rows.append(r)
        if i % 50 == 0:
            print("  已算 %d/%d" % (i, len(files)))
    if not rows:
        return rows

    # 分级用批内三分位：每一档都有图，不会出现「80% 都叫自由式」那种排不出高低的情况
    golds = sorted(r["golden"] for r in rows)
    syms = sorted(r["sym_best"] for r in rows)
    ents = sorted(r["dir_entropy"] for r in rows)
    varss = sorted(r["angular_var"] for r in rows)

    for r in rows:
        r["sym_label"] = sym_label(r)
        r["sym_grade"] = band(tercile(r["sym_best"], syms))
        r["curl"] = (tercile(r["dir_entropy"], ents) * 0.6
                     + tercile(r["angular_var"], varss) * 0.4)
        r["stream_label"] = band(r["curl"])
        r["golden_label"] = band(tercile(r["golden"], golds))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(
        ROOT, "data", "elements_clean"))
    ap.add_argument("--out", default=os.path.join(
        ROOT, "data", "elements_classified"))
    args = ap.parse_args()

    if not os.path.isdir(args.src):
        print("[失败] 找不到 %s" % args.src)
        return 2

    files = sorted(f for f in os.listdir(args.src)
                   if f.lower().endswith(".png") and not f.startswith("_")
                   and os.path.isfile(os.path.join(args.src, f)))
    print("待分类 %d 个元素" % len(files))
    rows = classify(files, args.src)
    if not rows:
        print("[失败] 一个都没算出来")
        return 2

    os.makedirs(args.out, exist_ok=True)
    for mid, key, _ in AXES:
        for r in rows:
            d = os.path.join(args.out, mid, r[key])
            os.makedirs(d, exist_ok=True)
            shutil.copy2(os.path.join(args.src, r["file"]),
                         os.path.join(d, r["file"]))

    fields = ["file", "sym_label", "sym_grade", "sym_best", "sym_mirror",
              "sym_radial", "rot_fold", "periodic",
              "stream_label", "curl", "dir_entropy", "angular_var",
              "golden_label", "golden", "golden_fill", "fill",
              "motif_w", "motif_h"]
    with io.open(os.path.join(args.out, "元素美学分类.csv"), "w",
                 encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (round(r[k], 3) if isinstance(r.get(k), float)
                            else r.get(k)) for k in fields})

    print()
    print("=== 分布 ===")
    for mid, key, name in AXES:
        cnt = {}
        for r in rows:
            cnt[r[key]] = cnt.get(r[key], 0) + 1
        parts = "，".join("%s %d" % (k, v)
                          for k, v in sorted(cnt.items(), key=lambda kv: -kv[1]))
        print("%s（%s）：%s" % (mid, name, parts))

    print()
    print("输出：%s" % args.out)
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    raise SystemExit(main())
