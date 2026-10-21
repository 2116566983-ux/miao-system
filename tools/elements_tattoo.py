# -*- coding: utf-8 -*-
"""把元素做成纹身稿：透明底 + 纯黑线条，没有底色块。

和「贴纸版」的区别
    贴纸版是深蓝底 + 白线，一块方形的底跟着图走，贴在别处会露出一个蓝方块。
    纹身稿只有线条本身，alpha 就是线条，放到任何底色上都成立。

为什么默认出黑色
    纹身稿最常用白底/浅皮底预览，白线在白底上等于看不见。
    同时出一份白色，供深色底使用。

用法：
    python tools/elements_tattoo.py
"""
import argparse, io, os, sys
import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from flatten_elements import clean_mask  # noqa: E402


def to_tattoo(path, color=(17, 17, 17)):
    mask, _ = clean_mask(path)
    if not mask.any():
        return None
    h, w = mask.shape
    rgb = np.zeros((h, w, 3), np.uint8)
    rgb[...] = color
    out = Image.fromarray(np.dstack([rgb, (mask.astype(np.uint8) * 255)]), "RGBA")
    ys, xs = np.nonzero(mask)
    pad = max(3, int(max(xs.max() - xs.min(), ys.max() - ys.min()) * 0.08))
    return out.crop((max(0, xs.min() - pad), max(0, ys.min() - pad),
                     min(w, xs.max() + pad + 1), min(h, ys.max() + pad + 1)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(ROOT, "data", "elements_clean"))
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "elements_tattoo"))
    args = ap.parse_args()
    os.makedirs(os.path.join(args.out, "白色"), exist_ok=True)
    files = sorted(f for f in os.listdir(args.src)
                   if f.endswith(".png") and not f.startswith("_"))
    ok = 0
    for f in files:
        p = os.path.join(args.src, f)
        try:
            blk = to_tattoo(p, (17, 17, 17))
            wht = to_tattoo(p, (250, 250, 250))
        except Exception:
            continue
        if blk is None:
            continue
        blk.save(os.path.join(args.out, f), optimize=True)
        wht.save(os.path.join(args.out, "白色", f), optimize=True)
        ok += 1
    print("%d 张纹身稿（黑/白各一份）→ %s" % (ok, args.out))
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    raise SystemExit(main())
