# -*- coding: utf-8 -*-
"""
生成可随仓库发布的缩略图集

为什么要做这件事：
    课堂要用的原图 155MB（1000 张绣品照片），放不进 GitHub 仓库 ——
    GitHub 单文件有 100MB 硬上限，仓库也不建议超过 1GB。
    但检索页、标注页、提取页都要看图，没图这个课就没法上。

    所以把原图压成「够看清纹样、但体积小得多」的缩略图，
    随仓库一起发布。云端部署时 streamlit 直接读 data/images/，
    学生的检索页就有图可看了。

    代价：清晰度下降，不适合下载去做印刷级设计。
    课上要高清图的，用原图目录（见 README）。

输出位置选 data/images/ 而不是别的目录：
    app/paths.py 的查找顺序是
        1. 环境变量 MIAO_IMAGE_ROOT
        2. 项目内 data/images/
        3. 项目内 images/
        4. 常见外部位置（兜底）
    放在 data/images/ 会被第 2 条命中，且**优先于**本机兜底路径 ——
    这样本地跑和云端跑看到的是同一批图，不会出现「我这能看、你那不能看」。

用法：
    python tools/build_thumbs.py                       # 自动找原图目录
    python tools/build_thumbs.py --src D:\\图片 --out data/images
    python tools/build_thumbs.py --max-edge 640 --quality 82
    python tools/build_thumbs.py --dry-run             # 只算体积，不写文件
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

from PIL import Image  # noqa: E402


# 长边默认 560：一张绣品照片压到 560px，纹样的花瓣、针脚走向还能看清，
# 足够学生判断「这张要不要选」。再小就看不出技法差异了。
#
# 为什么从 640 降到 560：实测 1000 张压到 640px 是 67MB，仍然偏大。
# 屏幕上检索网格里的缩略图一般也就 200-300px 宽，560px 仍是它的近两倍，
# 留了足够的余量给「点开看大图」。
DEFAULT_MAX_EDGE = 560

# JPEG 质量 78：肉眼几乎看不出与原图差别，体积比 82 再小约两成。
DEFAULT_QUALITY = 78

# 单张体积上限（KB）。超过这个数即使长边已经够小，也重新压一遍 ——
# 有些图长边不大但文件很肥（拍摄噪点多，JPEG 压不下去）。
# 不设这个闸门的话，这类图会原样复制进来，白白占仓库体积。
MAX_KB = 90


def human(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return "%.1f %s" % (nbytes, unit)
        nbytes /= 1024.0
    return "%.1f TB" % nbytes


def collect_sources(src: str):
    """收集源目录下的所有图片文件。"""
    exts = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
    out = []
    for name in sorted(os.listdir(src)):
        if name.lower().endswith(exts):
            out.append(name)
    return out


def build_one(src_path: str, dst_path: str, max_edge: int, quality: int,
              max_kb: int = MAX_KB):
    """压一张图。返回 (原字节, 新字节, 是否重编码过)。

    两条路，按「长边」和「体积」两个条件判断：

      长边 <= 目标 **且** 体积 <= max_kb  → 原样复制（体积不涨、画质不掉）
      否则                              → 缩放（如需）+ 重编码

    为什么要加体积这一条：
        实测 287/1000 张原图的长边本来就不到 640px。对这些图，
        重编码到 quality=82 反而会让文件**变大**（basic_100 从 57KB
        变 61KB）—— 它们本来就是被压过一轮的小图，再压只是重新量化，
        质量下降而体积上涨。所以本来就小的图原样复制。
        但有些图长边不大、文件却很肥（噪点多），原样复制会白白占体积，
        所以再用 max_kb 挡一道。
    """
    src_size = os.path.getsize(src_path)

    with Image.open(src_path) as im:
        w, h = im.size
        long_edge = max(w, h)
        need_resize = long_edge > max_edge
        need_recompress = src_size > max_kb * 1024

        # ---- 本来就够小又够轻：原样复制，不碰像素 ----
        if not need_resize and not need_recompress:
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            if os.path.abspath(src_path) != os.path.abspath(dst_path):
                with open(src_path, "rb") as fsrc:
                    data = fsrc.read()
                with open(dst_path, "wb") as fdst:
                    fdst.write(data)
            return src_size, os.path.getsize(dst_path), False

        # ---- 需要处理：缩放（如需）+ 重编码 ----
        # 统一转 RGB：原图可能有 CMYK / P 模式 / 带 alpha，
        # 直接存 JPEG 会报错或颜色错乱。
        if im.mode != "RGB":
            im = im.convert("RGB")

        if need_resize:
            scale = max_edge / float(long_edge)
            new_size = (max(1, int(round(w * scale))),
                        max(1, int(round(h * scale))))
            # LANCZOS：缩图时质量最好的一种重采样，
            # 默认的 NEAREST/BILINEAR 会让绣线的细纹路发糊。
            im = im.resize(new_size, Image.LANCZOS)

        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        im.save(dst_path, "JPEG", quality=quality, optimize=True,
                progressive=True)

    new_size_bytes = os.path.getsize(dst_path)

    # 兜底：万一压完反而更大（原图已经是极限压缩），退回原文件。
    # 不加这一步的话，仓库里会混进一批「压过还不如不压」的图。
    if new_size_bytes >= src_size and not need_resize:
        with open(src_path, "rb") as fsrc:
            data = fsrc.read()
        with open(dst_path, "wb") as fdst:
            fdst.write(data)
        return src_size, os.path.getsize(dst_path), False

    return src_size, new_size_bytes, True


def main():
    ap = argparse.ArgumentParser(description="生成可随仓库发布的缩略图集")
    ap.add_argument("--src", default="", help="原图目录（缺省则自动探测）")
    ap.add_argument("--out", default="", help="输出目录（缺省 data/images）")
    ap.add_argument("--max-edge", type=int, default=DEFAULT_MAX_EDGE,
                    help="长边像素上限，默认 %d" % DEFAULT_MAX_EDGE)
    ap.add_argument("--quality", type=int, default=DEFAULT_QUALITY,
                    help="JPEG 质量，默认 %d" % DEFAULT_QUALITY)
    ap.add_argument("--dry-run", action="store_true",
                    help="只计算体积，不写文件")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    src = args.src
    if not src:
        # 复用项目自己的探测逻辑，避免这里再写一份路径猜测
        from paths import find_image_root
        src = find_image_root() or ""
    if not src or not os.path.isdir(src):
        print("[失败] 找不到原图目录。请用 --src 指定，例如：")
        print("       python tools/build_thumbs.py --src D:\\图片目录")
        return 1

    out = args.out or os.path.join(root, "data", "images")

    names = collect_sources(src)
    if not names:
        print("[失败] %s 下没有找到图片文件" % src)
        return 1

    print("=" * 62)
    print("生成缩略图集%s" % ("（试运行，不写文件）" if args.dry_run else ""))
    print("=" * 62)
    print("原图目录：%s" % src)
    print("输出目录：%s" % out)
    print("长边上限：%d px　JPEG 质量：%d" % (args.max_edge, args.quality))
    print("图片数量：%d" % len(names))
    print()

    total_src = 0
    total_dst = 0
    failed = []
    n_resized = 0
    n_copied = 0

    for i, name in enumerate(names, 1):
        src_path = os.path.join(src, name)
        # 统一输出 .jpg：源里即使有 PNG，缩略图也存 JPEG，
        # 因为这里的用途是「屏幕上看着清楚」，不需要透明通道。
        stem = os.path.splitext(name)[0]
        dst_path = os.path.join(out, stem + ".jpg")
        try:
            if args.dry_run:
                s = os.path.getsize(src_path)
                # 试运行不真的压，按经验比例估算
                d = int(s * 0.16)
                total_src += s
                total_dst += d
            else:
                s, d, resized = build_one(src_path, dst_path,
                                          args.max_edge, args.quality)
                total_src += s
                total_dst += d
                if resized:
                    n_resized += 1
                else:
                    n_copied += 1
        except Exception as e:
            failed.append((name, str(e)))

        if i % 100 == 0 or i == len(names):
            print("  进度 %d / %d" % (i, len(names)))

    print()
    print("-" * 62)
    print("原始体积：%s" % human(total_src))
    print("处理后　：%s" % human(total_dst))
    if not args.dry_run:
        print("缩放重编码：%d 张　原样复制：%d 张" % (n_resized, n_copied))
    if total_src:
        print("压缩比：%.0f%%（缩到原来的 %.1f%%）"
              % (100 - total_dst * 100.0 / total_src,
                 total_dst * 100.0 / total_src))
    if failed:
        print()
        print("[注意] 有 %d 张失败：" % len(failed))
        for name, err in failed[:10]:
            print("  %s → %s" % (name, err))
    print("-" * 62)

    if not args.dry_run:
        print()
        print("下一步：")
        print("  1. 确认 images.csv 的 file_name 与这些文件名对得上")
        print("  2. git add data/images && git commit")
        print("  3. 推到 GitHub 后，Streamlit 云端会自动读到这批图")

    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
