# -*- coding: utf-8 -*-
"""只读：按应用完全相同的方式建隧道，去问 ComfyUI「你能用什么模型」。

不提交任何生成任务，只 GET /system_stats 和 /object_info/<加载器>。
这一条探的是决定性的东西：能不能出图，取决于加载器里有没有可选模型，
而不是只看目录里躺着几个文件。
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app"))

os.environ.setdefault("COMFYUI_SSH_HOST", os.environ.get("PROBE_HOST", ""))
os.environ.setdefault("COMFYUI_SSH_PORT", os.environ.get("PROBE_PORT", "22"))
os.environ.setdefault("COMFYUI_SSH_USER", os.environ.get("PROBE_USER", ""))
os.environ.setdefault("COMFYUI_SSH_PASSWORD", os.environ.get("PROBE_PASS", ""))

# 注意：comfy_tunnel 的 module docstring 里写的是 `get_tunnel`，
# 但那个名字**不存在**（真实导出是 `tunnel_from_env` / `SSHTunnel`）。
# 按文档抄会直接 ImportError —— 这是文档漂移，已同步修。
from comfy_tunnel import tunnel_from_env  # noqa: E402

LOADERS = ["CheckpointLoaderSimple", "UNETLoader", "CLIPLoader", "VAELoader"]


def get(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def main():
    tun = tunnel_from_env()
    if not tun:
        print("隧道建立失败")
        return 2

    base = tun.base_url()
    print("隧道已建立：%s -> 127.0.0.1:8188\n" % os.environ["COMFYUI_SSH_HOST"])
    try:
        info = get(base + "/system_stats")
        sysinfo = info.get("system", {})
        print("系统：")
        for d in info.get("devices", [])[:4]:
            print("   %s  %s  显存 %.1fGB" %
                  (d.get("type", "?"), d.get("name", "?"),
                   d.get("vram_total", 0) / 1e9))
        print("   ComfyUI 版本：%s"
              % sysinfo.get("comfyui_version", "?"))
        print("   Python：%s"
              % str(sysinfo.get("python_version", "?"))[:30])

        print("\n各加载器实际可选项：")
        have = {}
        for node in LOADERS:
            try:
                data = get(base + "/object_info/" + node)
                req = data.get(node, {}).get("input", {}).get("required", {})
                vals = None
                for key in ("ckpt_name", "unet_name", "clip_name",
                            "vae_name"):
                    if key in req:
                        vals = req[key][0]
                        kname = key
                        break
                vals = vals or []
                # 过滤掉 ComfyUI 自带的占位说明项
                real = [v for v in vals
                        if v and not str(v).startswith("put_")]
                have[node] = real
                print("   %-22s %d 项 %s"
                      % (node, len(real), real if len(real) <= 4
                         else real[:4] + ["..."]))
            except Exception as e:
                have[node] = []
                print("   %-22s 查询失败：%s" % (node, e))

        cp = have.get("CheckpointLoaderSimple", [])
        sepa = bool(have.get("UNETLoader")) and bool(have.get("CLIPLoader")) \
            and bool(have.get("VAELoader"))
        print("\n结论：")
        if cp:
            print("   整合检查点可用 → img2img_template.json 可直接出图")
        elif sepa:
            print("   分离式齐全 → img2img_qwen.json 可直接出图")
        else:
            print("   ⚠ 两套模型都不全，连上也无法出图。缺失：")
            if not cp:
                print("      - 整合检查点（models/checkpoints/ 是空的）")
            if not have.get("UNETLoader"):
                print("      - UNET")
            if not have.get("CLIPLoader"):
                print("      - CLIP")
            if not have.get("VAELoader"):
                print("      - VAE（models/vae/ 现在是空目录）")
        return 0
    finally:
        tun.stop()


if __name__ == "__main__":
    sys.exit(main())
