# -*- coding: utf-8 -*-
"""
离线渲染纹样 3D 网格，用于**肉眼验证立体感**。

为什么需要这个脚本：
    3D 效果最终只在浏览器 Plotly 里呈现，但"肉眼看不到就没法判断
    到底有没有立体感"。这里用一个最小的软件光栅化器（z-buffer +
    简单朗伯光照）把同一份网格画成 PNG，用来快速自检：

        - 有没有厚度（侧壁能不能看见）
        - 有没有明暗（光照方向对不对）
        - 轮廓是不是更像"实体"而不是"一张纸"

    它不参与产品功能，纯粹是开发期验证工具。

用法：
    python tools/render_mesh_preview.py [图片文件名] [输出PNG]
"""

from __future__ import annotations

import io
import os
import sys

import numpy as np

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                             errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, ROOT)

from motif_extract import extract, relief_field, relief_mesh   # noqa: E402
from paths import find_image_root                              # noqa: E402

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("需要 Pillow")
    raise SystemExit(1)

W, H = 760, 560


def render(mesh, out_path, yaw_deg=-28.0, pitch_deg=32.0, zoom=1.0):
    """把网格投影到屏幕，z-buffer 消隐 + 朗伯着色。"""
    x = np.asarray(mesh["x"], dtype=np.float64)
    y = np.asarray(mesh["y"], dtype=np.float64)
    z = np.asarray(mesh["z"], dtype=np.float64)
    col = mesh["color"]
    tri = np.stack([mesh["i"], mesh["j"], mesh["k"]], axis=1).astype(np.int64)

    # ---- 相机：先绕 x 轴俯仰，再绕 z 轴偏航 ----
    ya = np.radians(yaw_deg)
    pa = np.radians(pitch_deg)

    def rot(px, py, pz):
        # yaw（绕 z 轴）
        cx, sx = np.cos(ya), np.sin(ya)
        rx = px * cx - py * sx
        ry = px * sx + py * cx
        # pitch（绕 x 轴）
        cp, sp = np.cos(pa), np.sin(pa)
        ry2 = ry * cp - pz * sp
        rz2 = ry * sp + pz * cp
        return rx, ry2, rz2

    rx, ry, rz = rot(x, y, z)

    # 正交投影 + 屏幕映射
    scale = min(W, H) * 0.72 * zoom
    sx = rx * scale + W / 2.0
    sy = -ry * scale + H / 2.0
    sz = rz

    # ---- 光照 ----
    L = np.array([-0.45, -0.55, 0.70])
    L = L / np.linalg.norm(L)

    img = np.full((H, W, 3), 248, dtype=np.float64)
    zbuf = np.full((H, W), -1e9, dtype=np.float64)

    # 顶点色
    vcolor = np.array(
        [[int(c[c.find("(") + 1:c.find(",")]),
          int(c[c.find(",") + 1:c.rfind(",")]),
          int(c[c.rfind(",") + 1:c.find(")")])] for c in col],
        dtype=np.float64) / 255.0

    # 逐三角面光栅化
    for t in tri:
        p0 = np.array([sx[t[0]], sy[t[0]]])
        p1 = np.array([sx[t[1]], sy[t[1]]])
        p2 = np.array([sx[t[2]], sy[t[2]]])
        z0, z1, z2 = sz[t[0]], sz[t[1]], sz[t[2]]

        minx = int(max(0, np.floor(min(p0[0], p1[0], p2[0]))))
        maxx = int(min(W - 1, np.ceil(max(p0[0], p1[0], p2[0]))))
        miny = int(max(0, np.floor(min(p0[1], p1[1], p2[1]))))
        maxy = int(min(H - 1, np.ceil(max(p0[1], p1[1], p2[1]))))
        if minx > maxx or miny > maxy:
            continue

        # 面法线（用世界坐标算光照）
        w0 = np.array([x[t[0]], y[t[0]], z[t[0]]])
        w1 = np.array([x[t[1]], y[t[1]], z[t[1]]])
        w2 = np.array([x[t[2]], y[t[2]], z[t[2]]])
        n = np.cross(w1 - w0, w2 - w0)
        nn = np.linalg.norm(n)
        if nn < 1e-12:
            continue
        n = n / nn
        ndl = abs(float(np.dot(n, L)))
        shade = 0.42 + 0.72 * ndl          # 环境光 + 定向光
        shade = min(shade, 1.35)

        fc = (vcolor[t[0]] + vcolor[t[1]] + vcolor[t[2]]) / 3.0 * shade
        fc = np.clip(fc, 0, 1) * 255

        # 重心坐标扫描
        det = ((p1[1] - p2[1]) * (p0[0] - p2[0])
               + (p2[0] - p1[0]) * (p0[1] - p2[1]))
        if abs(det) < 1e-9:
            continue
        ys = np.arange(miny, maxy + 1)
        xs = np.arange(minx, maxx + 1)
        PX, PY = np.meshgrid(xs, ys)
        l0 = ((p1[1] - p2[1]) * (PX - p2[0])
              + (p2[0] - p1[0]) * (PY - p2[1])) / det
        l1 = ((p2[1] - p0[1]) * (PX - p2[0])
              + (p0[0] - p2[0]) * (PY - p2[1])) / det
        l2 = 1.0 - l0 - l1
        inside = (l0 >= -1e-6) & (l1 >= -1e-6) & (l2 >= -1e-6)
        if not inside.any():
            continue
        zz = l0 * z0 + l1 * z1 + l2 * z2
        sub_z = zbuf[miny:maxy + 1, minx:maxx + 1]
        better = inside & (zz > sub_z)
        if not better.any():
            continue
        sub_c = img[miny:maxy + 1, minx:maxx + 1]
        sub_z[better] = zz[better]
        sub_c[better] = fc

    out = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")
    out.save(out_path)
    return out


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "basic_432.jpg"
    outp = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        ROOT, "data", "motifs", "_mesh_preview.png")

    root = find_image_root()
    if not root:
        print("未找到图片目录")
        return 1
    p = os.path.join(root, name)
    if not os.path.exists(p):
        print("图片不存在：%s" % p)
        return 1

    r = extract(p, out_dir=os.path.join(ROOT, "data", "motifs"))
    if not r.get("ok"):
        print("提取失败：%s" % r.get("message"))
        return 1

    arr, alpha = r["array"], r["alpha"]
    # 传 cloth_rgb：让高度场能区分「绣花」与「底布」，
    # 这样离线预览和线上 Plotly 看到的是同一套高度场。
    hf = relief_field(arr, alpha, size=200, cloth_rgb=r.get("cloth_rgb"))
    mesh = relief_mesh(arr, alpha, height=hf, max_cells=110,
                       cloth_rgb=r.get("cloth_rgb"))
    print("顶点 %d，三角面 %d" % (len(mesh["x"]), len(mesh["i"])))

    # 三个角度各渲染一张，方便判断厚度是否可读
    for tag, yaw, pitch in (("front", -20, 28),
                            ("side", 62, 20),
                            ("top", -30, 58)):
        f = outp.replace(".png", "_%s.png" % tag)
        render(mesh, f, yaw_deg=yaw, pitch_deg=pitch)
        print("  已渲染 %s" % os.path.basename(f))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
