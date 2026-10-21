# -*- coding: utf-8 -*-
"""按关键词从 Bing 图片搜索批量下载纹样图。

用途
----
用户要的是「苗族蜡染」这类**白纹深底、完整对称**的图案，不是从实物照片里抠。
这类图网上现成很多，直接按关键词收集比自己抠快得多。

做法
----
Bing 图片的异步接口 `/images/async` 每页返回 35 条，每条把图片信息塞在
`m="{...}"` 这个 HTML 转义过的 JSON 里，取 `murl`（原图直链）、
`t`（标题）、`purl`（来源页）。翻页靠 `first=1,36,71...`。

下载时带 Referer（部分站点防盗链），拿 PIL 验证能打开、尺寸够，再按
MD5 去重。失败的跳过并记进清单，不中断整批。

**版权提醒**：这些图来自第三方网站，只适合内部调研/临摹参考，
不能直接商用。清单里保留了每张的来源页，需要授权时能找到出处。

用法：
    python tools/crawl_bing_images.py --selftest
    python tools/crawl_bing_images.py --per-query 60
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import os
import re
import sys
import time

import requests
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 目标风格：苗族蜡染/刺绣的白纹深底图案
DEFAULT_QUERIES = [
    "苗族蜡染 图案",
    "苗族蜡染 纹样",
    "苗族纹样 黑白",
    "苗族刺绣 图案 白描",
    "苗族蝴蝶妈妈 纹样",
    "苗族蜡染 鸟 鱼 图案",
]

M_JSON = re.compile(r'm="(\{[^"]+?\})"')


def parse_items(page_html):
    """从一页 HTML 里取出 [(murl, title, purl), ...]。"""
    out = []
    for raw in M_JSON.findall(page_html):
        try:
            d = json.loads(html.unescape(raw))
        except Exception:
            continue
        u = d.get("murl")
        if not u:
            continue
        out.append((u, d.get("t") or "", d.get("purl") or ""))
    return out


def _safe_name(s, limit=40):
    s = re.sub(r"[\\/:*?\"<>|\s]+", "_", s or "")
    return s.strip("_")[:limit] or "img"


def download(url, referer, timeout=6, max_bytes=12 * 1024 * 1024):
    r = requests.get(url, headers={"User-Agent": UA,
                                   "Referer": referer or "https://cn.bing.com/"},
                     timeout=timeout, stream=True)
    if r.status_code != 200:
        raise RuntimeError("HTTP %d" % r.status_code)
    buf = io.BytesIO()
    for chunk in r.iter_content(65536):
        buf.write(chunk)
        if buf.tell() > max_bytes:
            raise RuntimeError("文件过大")
    data = buf.getvalue()
    im = Image.open(io.BytesIO(data))
    im.load()
    return data, im.size, im.format.lower()


def selftest():
    """离线自检：解析、文件名净化、去重键。"""
    print("=== 自检 ===")
    ok = True

    def show(name, cond, extra=""):
        nonlocal ok
        ok = ok and cond
        print("  [%s] %s %s" % ("PASS" if cond else "FAIL", name, extra))

    sample = ('<a class="iusc" m="{&quot;murl&quot;:&quot;http://x.com/a.jpg&quot;,'
              '&quot;t&quot;:&quot;苗族蜡染&quot;,&quot;purl&quot;:&quot;http://p.com/1&quot;}">')
    items = parse_items(sample)
    show("能解析出 1 条", len(items) == 1, str(items))
    show("murl 正确", items and items[0][0] == "http://x.com/a.jpg", "")

    bad = '<a class="iusc" m="{不是JSON}">'
    show("脏数据不崩", parse_items(bad) == [], "")

    show("文件名净化", _safe_name("苗族/蜡染:图案 01") == "苗族_蜡染_图案_01",
         _safe_name("苗族/蜡染:图案 01"))
    show("空标题有兜底", _safe_name("") == "img", _safe_name(""))

    h1 = hashlib.md5(b"abc").hexdigest()
    show("去重键稳定", h1 == hashlib.md5(b"abc").hexdigest(), h1[:8])

    print()
    print("自检：%s" % ("通过" if ok else "有失败项"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", nargs="*", default=DEFAULT_QUERIES)
    ap.add_argument("--per-query", type=int, default=60)
    ap.add_argument("--min-side", type=int, default=400,
                    help="短边小于这个像素的丢弃（缩略图不要）")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "crawl_miao"))
    ap.add_argument("--delay", type=float, default=0.2)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    os.makedirs(args.out, exist_ok=True)
    manifest = os.path.join(args.out, "抓取清单.csv")
    seen = set()
    rows = []
    n_ok = n_fail = n_dup = n_small = 0

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})

    for qi, q in enumerate(args.queries, 1):
        print("\n[%d/%d] %s" % (qi, len(args.queries), q))
        got = 0
        first = 1
        while got < args.per_query and first < 400:
            try:
                r = sess.get("https://cn.bing.com/images/async",
                             params={"q": q, "first": first, "count": 35,
                                     "mmasync": 1},
                             timeout=20)
                items = parse_items(r.text)
            except Exception as e:
                print("   翻页失败：%s" % str(e)[:60])
                break
            if not items:
                break

            for murl, title, purl in items:
                if got >= args.per_query:
                    break
                try:
                    data, size, fmt = download(murl, purl)
                except Exception:
                    n_fail += 1
                    continue
                if min(size) < args.min_side:
                    n_small += 1
                    continue
                h = hashlib.md5(data).hexdigest()
                if h in seen:
                    n_dup += 1
                    continue
                seen.add(h)
                ext = "jpg" if fmt in ("jpeg", "jpg") else fmt
                name = "%02d_%s_%03d_%s.%s" % (qi, _safe_name(q, 12), got + 1,
                                               h[:8], ext)
                with open(os.path.join(args.out, name), "wb") as f:
                    f.write(data)
                rows.append({"query": q, "file": name, "w": size[0],
                             "h": size[1], "bytes": len(data),
                             "title": title[:80], "url": murl,
                             "page": purl})
                got += 1
                n_ok += 1
                time.sleep(args.delay)
            first += 35

        print("   收下 %d 张" % got)

    with io.open(manifest, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["query", "file", "w", "h", "bytes",
                                          "title", "url", "page"])
        w.writeheader()
        w.writerows(rows)

    print()
    print("成功 %d 张；重复跳过 %d；太小跳过 %d；下载失败 %d"
          % (n_ok, n_dup, n_small, n_fail))
    print("输出：%s" % args.out)
    print("清单：%s" % manifest)
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    raise SystemExit(main())
