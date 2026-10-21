# -*- coding: utf-8 -*-
"""元素出白底黑线稿：不透明白底 + 纯黑线条。

用户要的是这种——白纸黑线，直接能看、能打印、能当参考图。
透明底那份仍然保留，用于贴到别的底色上。

用法：
    python tools/elements_white.py
"""
import io, os, sys
import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from flatten_elements import clean_mask  # noqa: E402

SRC = os.path.join(ROOT, "data", "elements_clean")
OUT = os.path.join(ROOT, "data", "elements_white")
INK = (17, 17, 17)
PAPER = (255, 255, 255)


def to_white(path):
    mask, _ = clean_mask(path)
    if not mask.any():
        return None
    h, w = mask.shape
    canvas = np.zeros((h, w, 3), np.uint8)
    canvas[...] = PAPER
    canvas[mask] = INK
    out = Image.fromarray(canvas, "RGB")
    ys, xs = np.nonzero(mask)
    pad = max(4, int(max(xs.max() - xs.min(), ys.max() - ys.min()) * 0.10))
    return out.crop((max(0, xs.min() - pad), max(0, ys.min() - pad),
                     min(w, xs.max() + pad + 1), min(h, ys.max() + pad + 1)))


def main():
    os.makedirs(OUT, exist_ok=True)
    files = sorted(f for f in os.listdir(SRC)
                   if f.endswith(".png") and not f.startswith("_"))
    ok = 0
    for f in files:
        try:
            im = to_white(os.path.join(SRC, f))
        except Exception:
            continue
        if im is None:
            continue
        im.save(os.path.join(OUT, f), optimize=True)
        ok += 1
    print("%d 张白底稿 → %s" % (ok, OUT))
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    raise SystemExit(main())
