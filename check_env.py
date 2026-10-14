# -*- coding: utf-8 -*-
"""
环境自检

启动系统前先跑这个，一次性看清哪些环节可用、哪些缺什么。

用法：
    python check_env.py
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys


ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "app"))


OK = "[可用]"
NO = "[不可用]"
WARN = "[注意]"


def line(title):
    print()
    print("-" * 58)
    print(title)
    print("-" * 58)


def check_port(host, port, timeout=1.5):
    """检测端口是否有服务监听。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def check_python():
    line("1. Python 环境")
    v = sys.version_info
    print("  版本：%d.%d.%d" % (v.major, v.minor, v.micro))
    if v < (3, 9):
        print("  %s 需要 Python 3.9 以上" % NO)
        return False
    print("  %s 版本符合要求" % OK)

    line("2. 依赖包")
    pkgs = [
        ("streamlit", "界面框架", True),
        ("pandas", "数据处理", True),
        ("PIL", "图像处理", True),
        ("numpy", "数值计算", True),
        ("neo4j", "图数据库驱动", False),
        ("requests", "ComfyUI 对接", False),
    ]
    missing_required = []
    for mod, desc, required in pkgs:
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "")
            print("  %s %-12s %-14s %s" % (OK, mod, desc, ver))
        except ImportError:
            if required:
                print("  %s %-12s %-14s 必需" % (NO, mod, desc))
                missing_required.append(mod)
            else:
                print("  %s %-12s %-14s 可选，功能会降级" % (WARN, mod, desc))

    if missing_required:
        print()
        print("  缺少必需包，请执行：")
        print("    pip install -r requirements.txt")
        return False
    return True


def check_data():
    line("3. 数据文件")
    files = [
        ("metadata/images.csv", "纹样元数据", True),
        ("annotation/annotation_sheet.csv", "标注任务表", False),
        ("graph/csv/nodes_groups.csv", "图导入-MotifGroup", False),
        ("graph/csv/nodes_images.csv", "图导入-Image", False),
        ("graph/csv/rels_derived.csv", "图导入-关系", False),
        ("graph/schema.cypher", "建库脚本", False),
        ("graph/import.cypher", "导入脚本", False),
        ("workflows/img2img_template.json", "单图重组模板", False),
        ("workflows/multi_motif_template.json", "多纹样组合模板", False),
    ]
    all_ok = True
    for rel, desc, required in files:
        p = os.path.join(ROOT, rel)
        if os.path.exists(p):
            size = os.path.getsize(p)
            print("  %s %-36s %s" % (OK, rel, "%.0fKB" % (size / 1024)))
        else:
            mark = NO if required else WARN
            print("  %s %-36s 缺失（%s）" % (mark, rel, desc))
            if required:
                all_ok = False

    # 标注进度
    sheet = os.path.join(ROOT, "annotation", "annotation_sheet.csv")
    if os.path.exists(sheet):
        import csv
        with open(sheet, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        if rows:
            key = "【待填】纹样类型"
            if key in rows[0]:
                filled = sum(1 for r in rows if (r.get(key) or "").strip())
                print()
                print("  标注进度：%d / %d 组已填（%.0f%%）"
                      % (filled, len(rows), filled / len(rows) * 100))
                if filled == 0:
                    print("  %s 尚未开始标注，检索界面的地域/技法/色彩维度暂不可用"
                          % WARN)
                    print("      操作参见 annotation/标注操作指南.md")

    # 图片目录
    line("4. 图片目录")

    # 优先用自动探测（分发包里的 file_path 已按规范清空）
    root = ""
    try:
        sys.path.insert(0, os.path.join(ROOT, "app"))
        from paths import find_image_root
        root = find_image_root() or ""
    except Exception:
        pass

    if root:
        n = len([x for x in os.listdir(root) if x.lower().endswith(
            (".jpg", ".jpeg", ".png", ".bmp", ".webp"))])
        print("  图片目录：%s" % root)
        print("  %s 目录存在，含 %d 张图片" % (OK, n))
    else:
        print("  %s 未找到图片目录" % NO)
        print("      三选一：")
        print("        1. 把图片放进项目下的 data/images/ 目录")
        print("        2. 设置环境变量 MIAO_IMAGE_ROOT 指向图片目录")
        print("        3. 在界面左侧栏「图片所在文件夹」里手动填写")

    # 提示元数据是否需要按本机情况重新生成
    img_csv = os.path.join(ROOT, "metadata", "images.csv")
    if os.path.exists(img_csv):
        import csv as _csv
        with open(img_csv, encoding="utf-8-sig", newline="") as f:
            rows = list(_csv.DictReader(f))
        if rows:
            blank = sum(1 for r in rows if not (r.get("file_path") or "").strip())
            if blank == len(rows):
                print("  元数据：%d 行，file_path 为空（分发包正常状态）" % len(rows))
                print("          换机器后建议重新生成：")
                print("          python build_metadata.py --src \"图片目录\" --out metadata/images.csv")
            else:
                print("  元数据：%d 行，含本机路径记录" % len(rows))
    return all_ok


def check_neo4j():
    line("5. 图数据库 Neo4j")
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    port_ok = check_port("127.0.0.1", 7687)
    print("  连接地址：%s" % uri)
    print("  端口 7687 %s" % ("有服务监听" if port_ok else "无服务监听"))

    try:
        from neo4j_client import client_from_env
        c = client_from_env()
        if c.available:
            print("  %s 连接成功" % OK)
            prog = c.annotation_progress()
            if prog:
                print("     图库中分组数：%s" % prog.get("total_groups", 0))
            c.close()
            return
        print("  %s %s" % (NO, str(c.error)[:100]))
    except Exception as e:
        print("  %s %s" % (NO, str(e)[:100]))

    print()
    print("  说明：不装 Neo4j 系统也能用，检索会降级为读 CSV，功能不受影响。")
    print("  要启用图查询：")
    print("    1. 安装 Neo4j Desktop：https://neo4j.com/download/")
    print("    2. 执行 graph/schema.cypher 建库")
    print("    3. 把 graph/csv/*.csv 放入 import 目录后执行 graph/import.cypher")
    print("    4. 设置环境变量 NEO4J_PASSWORD 后重启系统")


def check_comfy():
    line("6. AI 生成后端")
    print("  生成后端优先级：ComfyUI(本地/远端) → 免费云端 API → 不可用")
    print()

    # ---- 后端一：ComfyUI（本地或远端）----
    ssh_host = os.environ.get("COMFYUI_SSH_HOST", "").strip()
    if ssh_host:
        print("  [后端 A] 远端 ComfyUI（走 SSH 隧道）")
        print("    服务器：%s:%s"
              % (ssh_host, os.environ.get("COMFYUI_SSH_PORT", "22")))
        print("    用户：%s" % os.environ.get("COMFYUI_SSH_USER", "（未填）"))
        print("    远端地址：%s"
              % os.environ.get("COMFYUI_REMOTE", "127.0.0.1:8188"))
        print("    说明：隧道由系统自动建立，不需要手工开 ssh -L")
    else:
        print("  [后端 A] 本地 ComfyUI")
        url = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")
        port_ok = check_port("127.0.0.1", 8188)
        print("    连接地址：%s" % url)
        print("    端口 8188 %s" % ("有服务监听" if port_ok else "无服务监听"))

    comfy_ok = False
    try:
        from comfy_client import client_from_env
        c = client_from_env()
        if c.available:
            print("    %s 连接成功" % OK)
            dev = c.device_summary()
            if dev:
                print("       运行设备：%s" % dev)
            if getattr(c, "tunnel_info", None):
                print("       隧道：%s" % c.tunnel_info)

            # 盘点模型：决定能不能真出图
            inv = c.model_inventory()
            n_ckpt = len(inv.get("checkpoints") or [])
            n_unet = len(inv.get("unets") or [])
            n_clip = len(inv.get("clips") or [])
            n_vae = len(inv.get("vaes") or [])
            print("       模型盘点：整合检查点 %d · 扩散模型 %d · "
                  "文本编码器 %d · VAE %d"
                  % (n_ckpt, n_unet, n_clip, n_vae))

            if n_ckpt:
                print("       %s 可以直接出图（整合检查点）" % OK)
            elif n_unet and n_clip and n_vae:
                print("       %s 可以直接出图（分离式模型）" % OK)
            else:
                print("       %s 连接正常但缺少生成模型，无法出图" % WARN)
                print("           需要放入扩散模型 + 文本编码器 + VAE")
            comfy_ok = True
        else:
            print("    %s %s" % (NO, str(c.error)[:100]))
    except Exception as e:
        print("    %s %s" % (NO, str(e)[:100]))

    if not comfy_ok:
        print("    提示：不装 ComfyUI 也可以，用下面的云端 API 即可。")
        print("    安装步骤见 docs/comfyui-guide.md")

    print()

    # ---- 后端二：免费云端 API ----
    print("  [后端 B] 免费云端生图 API")
    api_ok = False
    try:
        from api_image_client import ApiImageClient
        a = ApiImageClient.from_env()
        print("    接口地址：%s" % a.base_url)
        print("    使用模型：%s" % a.model)
        if a.available:
            print("    密钥状态：已配置")
            ok, msg = a.probe()
            if ok:
                print("    %s %s" % (OK, msg))
                api_ok = True
            else:
                print("    %s %s" % (NO, msg))
        else:
            print("    密钥状态：未配置")
            print("    %s %s" % (NO, a.error))
            if a.hint:
                print("    提示：%s" % a.hint)
    except Exception as e:
        print("    %s %s" % (NO, str(e)[:100]))

    print()

    # ---- 综合结论 ----
    if comfy_ok:
        _where = "远端 ComfyUI · SSH 隧道" if ssh_host else "本地 ComfyUI"
        print("  结论：%s 生成功能可用（%s）" % (OK, _where))
    elif api_ok:
        print("  结论：%s 生成功能可用（免费云端 API）" % OK)
    else:
        print("  结论：%s 生成功能不可用，但检索与归档照常可用。" % NO)
        print("        三种开启方式任选其一：")
        print("        1. 远端 ComfyUI：设置 COMFYUI_SSH_HOST 等变量，系统自动建 SSH 隧道")
        print("        2. 本地 ComfyUI：见 docs/comfyui-guide.md")
        print("        3. 云端 API：到 modelscope.cn 注册实名后设置 IMAGE_API_KEY")
        print("        未配置生成后端时，界面会自动降级为『仅检索』，不报错。")

    print()
    print("  显卡检测：")
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5)
        out = (r.stdout or "").strip()
        if out:
            for l in out.splitlines():
                print("     %s" % l)
        else:
            print("     未检测到 NVIDIA 显卡")
    except Exception:
        print("     未检测到 nvidia-smi（可能无 N 卡或未装驱动）")


def main():
    print("=" * 58)
    print("苗绣纹样创作系统 · 环境自检")
    print("=" * 58)

    cond = check_python()
    check_data()
    check_neo4j()
    check_comfy()

    line("总结")
    print("  检索功能：%s（不依赖外部服务）" % OK)
    print("  该功能始终可用，即使 Neo4j、ComfyUI 和云端 API 都没配。")
    print()
    if cond:
        print("  启动系统：streamlit run app/streamlit_app.py")
    else:
        print("  请先安装依赖：pip install -r requirements.txt")
    print()


if __name__ == "__main__":
    main()
