# -*- coding: utf-8 -*-
"""整幅纹样换白底：白线深底 → 白底黑线，保持原分辨率。

为什么不切小块：一切块分辨率就掉到一百多像素（实测元素长边中位 105px，
纹样块 712px）。用户要的是"换背景"，不是"拆零件"，所以整幅处理。

做法：亮度大津分割取出白线 → 白线画黑、其余铺白。
不缩放、不裁剪（块本身已经裁好了），保证像素不损失。

用法：
    python tools/motifs_white.py --src data/motifs_split
"""
import argparse, io, os, sys
import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from patterns_to_bw import _otsu  # noqa: E402
from scipy import ndimage  # noqa: E402
from scipy import ndimage  # noqa: E402

INK, PAPER = 17, 255


def to_white(path):
    im = Image.open(path).convert("RGB")
    a = np.asarray(im, dtype=np.float32)
    L = 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]
    thr = max(_otsu(L.ravel()), 60.0)
    lines = L > thr

    # 线条占比过高的不是"深底白线"，是照片或反相图。硬转会得到一大片黑块，
    # 直接跳过——宁可少交几张，也不交废图。
    # 线条太稀疏的也不是纹样：海报、logo、文字块都长这样
    if lines.mean() < 0.08:
        raise ValueError("线条占比仅 %.0f%%，判为文字/海报" % (lines.mean() * 100))
    # 线条太稀疏的也不是纹样：海报、logo、文字块都长这样
    if lines.mean() < 0.08:
        raise ValueError("线条占比仅 %.0f%%，判为文字/海报" % (lines.mean() * 100))
    if lines.mean() > 0.45:
        raise ValueError("线条占比 %.0f%%，判为照片/反相图" % (lines.mean() * 100))

    out = np.full(L.shape, PAPER, np.uint8)
    out[lines] = INK
    return Image.fromarray(out, "L")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(ROOT, "data", "motifs_split"))
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "motifs_white"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    files = sorted(f for f in os.listdir(args.src)
                   if f.endswith(".png") and not f.startswith("_"))
    ok = 0
    for f in files:
        try:
            to_white(os.path.join(args.src, f)).save(
                os.path.join(args.out, f), optimize=True)
            ok += 1
        except Exception as e:
            print("  跳过 %s：%s" % (f[:30], str(e)[:30]))
    print("%d 张整幅白底稿 → %s" % (ok, args.out))
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    raise SystemExit(main())
