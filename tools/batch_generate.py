# -*- coding: utf-8 -*-
"""按清单批量调用生图接口：纹样图 → 二次创作图。

用法：
    set IMAGE_API_KEY=xxx        # Windows，或 export IMAGE_API_KEY=xxx
    python tools/batch_generate.py --manifest <清单.csv> --limit 5

清单列：motif（参考图文件名）、prompt（提示词）、product、strength
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app"))

from api_image_client import ApiImageClient, ApiImageError  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", default=None, help="输出目录，默认与清单同级的 generated/")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--size", default="1024x1024")
    args = ap.parse_args()

    base = os.path.dirname(os.path.abspath(args.manifest))
    out = args.out or os.path.join(base, "generated")
    os.makedirs(out, exist_ok=True)

    client = ApiImageClient()
    if not client.available:
        print("[无法生成] %s" % client.error)
        print("           %s" % client.hint)
        print("先配置密钥再跑；清单和素材都已就绪。")
        return 2

    with io.open(args.manifest, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if args.limit:
        rows = rows[:args.limit]

    ok = fail = 0
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        motif = os.path.join(base, r["motif"])
        try:
            paths = client.generate(prompt=r["prompt"], image_path=motif,
                                    out_dir=out, size=args.size,
                                    prefix=os.path.splitext(r["motif"])[0])
            ok += 1
            print("  %d/%d %s → %s" % (i, len(rows), r["motif"][:36],
                                       os.path.basename(paths[0]) if paths else "无输出"))
        except ApiImageError as e:
            fail += 1
            print("  %d/%d %s 失败：%s" % (i, len(rows), r["motif"][:36], e))
        except Exception as e:                          # noqa: BLE001
            fail += 1
            print("  %d/%d 异常：%s" % (i, len(rows), str(e)[:60]))

    print()
    print("成功 %d，失败 %d，用时 %.1f 分钟" % (ok, fail, (time.time() - t0) / 60))
    print("输出：%s" % out)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    raise SystemExit(main())
