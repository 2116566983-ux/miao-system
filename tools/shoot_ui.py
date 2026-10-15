# -*- coding: utf-8 -*-
"""
界面截图工具（开发用）

用途：把 Streamlit 页面按标签页逐个截图，用于**视觉走查**——
判断配色、间距、层级是否达到设计预期。纯看代码看不出"AI 味"，
必须真的看一眼渲染结果。

用法：
    python tools/shoot_ui.py                 # 截全部标签页
    python tools/shoot_ui.py --only 0        # 只截第 1 个标签页
    python tools/shoot_ui.py --port 8501
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import time

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(APP_DIR, "data", "ui_shots")
APP = os.path.join(APP_DIR, "app", "streamlit_app.py")

# 标签页名称**从应用源码里读**，不要在这里再抄一份。
#
# 踩过的坑：这里原本硬编码着一份页签列表，把 ⑦ 作品展示墙 挪到
# 最后时，只改了 streamlit_app.py，忘了改这里 —— 结果这个工具继续
# 按旧顺序点页签，截图全部指鹿为马（点 ⑥ 的位置拿到的是 ⑦ 的图）。
# 同一个事实在两处各写一份，改一处必然漏另一处，所以直接读源头。

_CIRCLED = {"⓪": "0", "①": "1", "②": "2", "③": "3", "④": "4",
           "⑤": "5", "⑥": "6", "⑦": "7", "⑧": "8", "⑨": "9"}


def load_tab_names():
    """从 app/streamlit_app.py 的 st.tabs([...]) 里读出页签与顺序。"""
    with io.open(APP, encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"st\.tabs\(\s*\[(.*?)\]\s*\)", src, re.S)
    if not m:
        raise RuntimeError("在 %s 里找不到 st.tabs([...])，无法读取页签"
                           % os.path.relpath(APP, APP_DIR))
    names = re.findall(r"\"([^\"]+)\"|'([^']+)'", m.group(1))
    names = [a or b for a, b in names]
    if not names:
        raise RuntimeError("st.tabs([...]) 里没解析出任何页签名")
    return names


def safe_name(name):
    """把页签名转成文件名 —— ⑦ 也要处理，否则带圈字符进文件名会乱码。"""
    out = []
    for ch in name:
        out.append(_CIRCLED.get(ch, ch))
    return "".join(out).replace(" ", "_")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8501)
    ap.add_argument("--url", default=None)
    ap.add_argument("--only", type=int, default=None,
                    help="只截某个标签页（从 0 开始）")
    ap.add_argument("--width", type=int, default=1500)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--full", action="store_true",
                    help="整页截图（默认只截视口）")
    args = ap.parse_args()

    url = args.url or "http://localhost:%d" % args.port

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("需要 playwright：pip install playwright && playwright install chromium")
        return 2

    os.makedirs(OUT_DIR, exist_ok=True)

    try:
        tab_names = load_tab_names()
    except (RuntimeError, OSError) as e:
        print("[失败] %s" % e)
        return 2
    print("页签（读自 app/streamlit_app.py）：%s" % " ".join(tab_names))

    targets = ([args.only] if args.only is not None
               else list(range(len(tab_names))))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": args.width,
                                          "height": args.height},
                                device_scale_factor=1)
        page.goto(url, wait_until="networkidle", timeout=60000)

        # Streamlit 首屏渲染较慢，等它把标题画出来
        try:
            page.wait_for_selector("h1", timeout=30000)
        except Exception:
            print("警告：没等到 h1，可能还在加载")
        time.sleep(3)

        for idx in targets:
            name = tab_names[idx]
            print("截图 %d/%d：%s" % (idx + 1, len(tab_names), name))
            try:
                # 点击对应标签
                tab = page.get_by_role("tab", name=name)
                if tab.count() == 0:
                    # 退化：按文本找
                    tab = page.locator("button[role=tab]", has_text=name)
                tab.first.click()
                time.sleep(2.5)
            except Exception as e:
                print("  跳过（找不到标签）：%s" % str(e)[:80])
                continue

            fp = os.path.join(OUT_DIR,
                              "%02d_%s.png" % (idx + 1, safe_name(name)))
            page.screenshot(path=fp, full_page=args.full)
            print("  -> %s" % os.path.relpath(fp, APP_DIR))

        browser.close()

    print("\n完成。输出目录：%s" % os.path.relpath(OUT_DIR, APP_DIR))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
