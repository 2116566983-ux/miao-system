# -*- coding: utf-8 -*-
"""
项目路径配置

为什么要单独一个文件：
    原先把图片目录写死成某台机器的绝对路径（C:/Users/xxx/Desktop/...），
    换一台机器、换一个用户就跑不起来。这里统一成"自动探测 + 环境变量覆盖"。

查找顺序（先找到先用）：
    1. 环境变量 MIAO_IMAGE_ROOT
    2. 项目目录下的 data/images/
    3. 项目目录下的 images/
    4. 常见位置（仅作兜底）
    5. 都没有 → 返回 None，由调用方提示用户手动指定

这样别人拿到项目，只要把图片放进 data/images/ 就能直接用。
"""

from __future__ import annotations

import os

# 项目根目录（本文件在 app/ 下，向上一级）
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 项目内约定的图片目录
CANDIDATES_IN_PROJECT = [
    os.path.join(PROJECT_DIR, "data", "images"),
    os.path.join(PROJECT_DIR, "images"),
    os.path.join(PROJECT_DIR, "archive"),
]

# 兜底：常见的外部位置（不再硬编码具体用户名）
def _common_locations():
    home = os.path.expanduser("~")
    return [
        os.path.join(home, "Desktop", "archive", "byx"),
        os.path.join(home, "Desktop", "archive"),
        os.path.join(home, "archive", "byx"),
        os.path.join(home, "data", "miao"),
    ]


def find_image_root(verbose=False):
    """按优先级查找图片根目录，返回路径或 None。"""

    # 1. 环境变量最优先
    env = os.environ.get("MIAO_IMAGE_ROOT", "").strip()
    if env:
        if os.path.isdir(env):
            return env
        if verbose:
            print("[警告] MIAO_IMAGE_ROOT 指向的目录不存在：%s" % env)

    # 2. 项目内约定目录
    for p in CANDIDATES_IN_PROJECT:
        if os.path.isdir(p):
            # 确认里面真有图片，避免空目录误判
            try:
                files = [f for f in os.listdir(p)
                         if f.lower().endswith((".jpg", ".jpeg", ".png"))]
                if files:
                    return p
            except Exception:
                continue

    # 3. 常见位置兜底
    for p in _common_locations():
        if os.path.isdir(p):
            try:
                files = [f for f in os.listdir(p)
                         if f.lower().endswith((".jpg", ".jpeg", ".png"))]
                if files:
                    return p
            except Exception:
                continue

    return None


def image_root_or_hint():
    """找不到时给一句可操作的提示。"""
    return (
        "未找到纹样图片目录。请三选一：\n"
        "  1. 把图片放进项目下的 data/images/ 目录\n"
        "  2. 设置环境变量 MIAO_IMAGE_ROOT 指向图片目录\n"
        "     Windows:  set MIAO_IMAGE_ROOT=D:\\你的\\图片目录\n"
        "     Linux:    export MIAO_IMAGE_ROOT=/path/to/images\n"
        "  3. 在界面左侧栏「图片所在文件夹」里手动填写路径"
    )


if __name__ == "__main__":
    root = find_image_root(verbose=True)
    print("项目目录：", PROJECT_DIR)
    if root:
        n = len([f for f in os.listdir(root)
                 if f.lower().endswith((".jpg", ".jpeg", ".png"))])
        print("图片目录：", root)
        print("图片数量：", n)
    else:
        print("图片目录：未找到")
        print()
        print(image_root_or_hint())
