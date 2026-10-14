# -*- coding: utf-8 -*-
"""
ComfyUI 对接层

ComfyUI 提供 HTTP API，核心流程是异步的：
    上传图片 -> 提交工作流 -> 轮询队列/历史 -> 下载结果

因此本模块把"提交"和"取回"拆成两步，界面才能正确显示进度，
而不是卡在一个请求里等几十秒。

主要接口：
    POST /upload/image          上传输入图片
    POST /prompt                提交工作流，返回 prompt_id
    GET  /history/{prompt_id}   查询任务结果
    GET  /view                  下载输出图片
    GET  /queue                 查询队列状态

用法：
    from comfy_client import ComfyClient
    c = ComfyClient("http://127.0.0.1:8188")
    if c.available:
        pid = c.submit(workflow, client_id="student_01")
        status = c.poll(pid)          # 界面里定时调用
        if status["done"]:
            paths = c.fetch_images(pid, out_dir)
"""

from __future__ import annotations

import io
import json
import os
import time
import uuid
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    requests = None


def no_proxy_session(base_url):
    """构造一个对本地/局域网地址绕过系统代理的 requests.Session。

    为什么必须这么做：
        很多机器（公司网络、装了代理软件）会设置 HTTP_PROXY 环境变量。
        requests 默认会读取它，把连 127.0.0.1:8188 的请求也发给代理，
        代理连不上就返回 502 —— 看起来像"ComfyUI 挂了"，其实压根没启动，
        或者反过来：ComfyUI 明明在跑，却被代理拦住了。

        本地回环地址和局域网地址永远不该走代理，这里显式写进 NO_PROXY。
    """
    s = requests.Session()
    host = (urlparse(base_url).hostname or "").lower()
    bypass = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
    # 私有网段也一并绕过（192.168.x / 10.x / 172.16-31.x）
    is_private = (
        host.startswith("192.168.")
        or host.startswith("10.")
        or any(host.startswith("172.%d." % i) for i in range(16, 32))
    )
    if host in bypass or is_private:
        s.trust_env = False        # 彻底不读代理环境变量
    else:
        s.proxies = {
            "http": os.environ.get("NO_PROXY", ""),
            "https": os.environ.get("NO_PROXY", ""),
        }
    return s


class ComfyError(Exception):
    pass


class ComfyClient:
    """ComfyUI HTTP API 客户端。

    连不上服务时 available 为 False，界面应降级为"仅检索"模式。
    """

    def __init__(self, base_url="http://127.0.0.1:8188", timeout=5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.available = False
        self.error = None
        self.hint = None
        self.client_id = str(uuid.uuid4())
        self.tunnel = None
        self.tunnel_info = None

        if requests is None:
            self.error = "未安装 requests 依赖"
            self.hint = "pip install requests"
            return

        # 如果配了远端服务器，先建 SSH 隧道，把 base_url 指向本地转发端口
        self._maybe_open_tunnel()

        self.session = no_proxy_session(self.base_url)

        # 隧道就绪才探测；隧道失败的原因要保留，不能被 _probe 覆盖
        if not self.error:
            self._probe()

    # ------------------------------------------------------------------
    def _maybe_open_tunnel(self):
        """按环境变量决定是否走 SSH 隧道。

        为什么需要：
            服务器上的 ComfyUI 通常只监听 127.0.0.1，或者虽然监听
            0.0.0.0 但云安全组不放行 8188。把 8188 暴露到公网不安全，
            所以走 SSH 隧道 —— ComfyUI 不用改任何配置。

        失败时不抛异常，只记录原因，让界面显示出来。
        """
        host = os.environ.get("COMFYUI_SSH_HOST", "").strip()
        if not host:
            return

        try:
            from comfy_tunnel import tunnel_from_env
            self.tunnel = tunnel_from_env()
            if self.tunnel:
                self.base_url = self.tunnel.base_url()
                self.tunnel_info = "%s:%s -> %s:%d" % (
                    host, os.environ.get("COMFYUI_SSH_PORT", "22"),
                    *self.tunnel.remote)
        except ImportError:
            self.error = "配置了 SSH 隧道但未安装 paramiko"
            self.hint = "pip install paramiko（或改用直连地址）"
        except Exception as e:
            self.error = "SSH 隧道建立失败：%s" % str(e)[:150]
            self.hint = ("请检查 COMFYUI_SSH_HOST / COMFYUI_SSH_USER / "
                         "COMFYUI_SSH_PASSWORD 是否正确，"
                         "以及服务器 SSH 端口是否可达。")

    def close(self):
        """关闭隧道（若已建立）。"""
        if self.tunnel:
            try:
                self.tunnel.stop()
            except Exception:
                pass
            self.tunnel = None

    # ------------------------------------------------------------------
    # 连通性
    # ------------------------------------------------------------------
    def _probe(self):
        """探测服务是否可用，并读取系统信息。

        error 只写"发生了什么"，hint 写"怎么解决"，界面分开显示，
        避免把「没启动」误报成「服务返回 502」这种误导性描述。
        """
        try:
            r = self.session.get(self.base_url + "/system_stats",
                                 timeout=self.timeout)
            if r.status_code == 200:
                self.available = True
                self.stats = r.json()
            elif r.status_code in (502, 503, 504):
                # 典型情况：请求被系统代理拦截，而非 ComfyUI 本身报错
                self.error = "连接被代理拦截（返回 %d），未真正到达 ComfyUI" % r.status_code
                self.hint = ("本机设置了 HTTP_PROXY，但 ComfyUI 地址是 "
                             "127.0.0.1，请求不应走代理。系统已尝试绕过；"
                             "若仍失败，请确认 ComfyUI 是否已启动。")
            else:
                self.error = "服务返回状态码 %d" % r.status_code
                self.hint = "请确认该地址上运行的是 ComfyUI"
        except requests.exceptions.ConnectionError:
            self.error = "无法连接：目标端口无服务（连接被拒绝）"
            self.hint = ("ComfyUI 尚未启动。启动方式见 docs/comfyui-guide.md，"
                         "启动后默认监听 127.0.0.1:8188。")
        except requests.exceptions.Timeout:
            self.error = "连接超时（%s 秒无响应）" % self.timeout
            self.hint = "服务可能在启动中或卡死，请查看 ComfyUI 控制台日志。"
        except Exception as e:
            self.error = "无法连接 ComfyUI：%s" % e
            self.hint = None

    def device_summary(self):
        """返回运行设备摘要，供界面显示。"""
        if not self.available:
            return None
        try:
            devs = (self.stats or {}).get("devices") or []
            if devs:
                d = devs[0]
                return "%s · %.1fGB" % (
                    d.get("name", "未知设备"),
                    (d.get("vram_total", 0) or 0) / (1024 ** 3),
                )
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # 能力探测（决定用哪套工作流模板）
    # ------------------------------------------------------------------
    def node_options(self, node_type, field, timeout=20):
        """查某个加载节点有哪些可选值（模型清单）。

        ComfyUI 的 /object_info/<NodeType> 会返回该节点 required 输入的
        枚举值，这是枚举模型清单最可靠的方式。

        返回列表；查不到返回空列表。
        """
        if not self.available:
            return []
        try:
            r = self.session.get(
                "%s/object_info/%s" % (self.base_url, node_type),
                timeout=timeout)
            if r.status_code != 200:
                return []
            data = r.json()
            node = data.get(node_type) or {}
            vals = (node.get("input", {}).get("required", {})
                    .get(field, [[]])[0])
            return list(vals) if isinstance(vals, list) else []
        except Exception:
            return []

    def model_inventory(self):
        """盘点可用模型，用于挑选工作流模板。

        返回 dict：
            checkpoints      整合检查点（CheckpointLoaderSimple）
            unets            扩散模型（UNETLoader）
            clips            文本编码器（CLIPLoader）
            vaes             VAE（VAELoader）
        """
        return {
            "checkpoints": self.node_options(
                "CheckpointLoaderSimple", "ckpt_name"),
            "unets": self.node_options("UNETLoader", "unet_name"),
            "clips": self.node_options("CLIPLoader", "clip_name"),
            "vaes": self.node_options("VAELoader", "vae_name"),
        }

    def missing_models(self, requirements):
        """检查工作流需要的模型是否都在服务器上，返回缺失清单。

        requirements 形如 template_requirements() 的返回：
            {"ckpt": "xxx.safetensors", "unet": ..., "clip": ..., "vae": ...}

        为什么要单独做这件事：
            模板与服务器模型不匹配时，ComfyUI 的报错是
            「value_not_in_list: ckpt_name not in []」——
            它只说「这个模型不在列表里」，不会说「是你选错了模板」。
            学生和教师看到这句只会以为模型坏了。
            在**提交之前**自己核对一遍，就能给出「换哪个模板」这种可执行的结论。

        返回字符串列表（空列表 = 全部就位）。
        查询失败时返回空列表（不阻塞提交，让 ComfyUI 自己去报）。
        """
        if not requirements:
            return []

        try:
            inv = self.model_inventory()
        except Exception:
            return []

        pool = {
            "ckpt": inv.get("checkpoints") or [],
            "unet": inv.get("unets") or [],
            "clip": inv.get("clips") or [],
            "vae": inv.get("vaes") or [],
        }
        label = {"ckpt": "整合检查点", "unet": "扩散模型",
                 "clip": "文本编码器", "vae": "VAE"}

        missing = []
        for key, want in requirements.items():
            if not want or key not in pool:
                continue
            if want not in pool[key]:
                have = pool[key]
                missing.append(
                    "%s `%s` 不存在（服务器上该类别共 %d 个%s）"
                    % (label.get(key, key), want, len(have),
                       ("：" + "、".join(have[:3])) if have else "，一个都没有"))
        return missing

    # ------------------------------------------------------------------
    # 释放显存（共享机器上的礼貌操作）
    # ------------------------------------------------------------------
    def free_memory(self, unload_models=True):
        """让 ComfyUI 卸载缓存的模型、释放显存。

        为什么需要：
            ComfyUI 默认会把模型常驻显存，方便下次快速出图
            （所以首图 86 秒、后续 7 秒）。
            但在**共享的服务器**上，跑完就走却把几 GB 显存占着不放，
            会挤压别人的任务。

            用完调一次，下次自己再用时重新加载即可（慢一点但公平）。

        注意：这只影响缓存，不删任何文件、不改任何配置。
        下一次提交任务时 ComfyUI 会自动重新加载模型。

        返回 True 表示请求已被接受。
        """
        if not self.available:
            return False
        try:
            r = self.session.post(self.base_url + "/free",
                                  json={"unload_models": bool(unload_models),
                                        "free_memory": True},
                                  timeout=30)
            return r.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 上传输入图片
    # ------------------------------------------------------------------
    def upload_image(self, image_path, subfolder=""):
        """上传一张图片作为工作流输入，返回 ComfyUI 侧的文件名。"""
        if not self.available:
            raise ComfyError("ComfyUI 未连接")
        if not os.path.exists(image_path):
            raise ComfyError("图片不存在：%s" % image_path)

        name = os.path.basename(image_path)
        with open(image_path, "rb") as f:
            files = {"image": (name, f, "image/jpeg")}
            data = {"overwrite": "true"}
            if subfolder:
                data["subfolder"] = subfolder
            r = self.session.post(self.base_url + "/upload/image",
                                  files=files, data=data, timeout=60)
        if r.status_code != 200:
            raise ComfyError("上传失败：%s" % r.text[:200])
        j = r.json()
        return j.get("name", name), j.get("subfolder", "")

    # ------------------------------------------------------------------
    # 提交工作流
    # ------------------------------------------------------------------
    def submit(self, workflow, client_id=None):
        """提交工作流，返回 prompt_id。

        workflow 必须是 API 格式（节点ID -> {class_type, inputs}），
        在 ComfyUI 界面用 "Save (API Format)" 导出。

        提交前会剔除注释类键（`_readme` 等）。ComfyUI 会把顶层任意键
        当节点解析，遇到说明性键会抛未捕获异常并返回 **500**，
        而不是可读的校验错误 —— 先清干净，免得调用方踩这个坑。
        """
        if not self.available:
            raise ComfyError("ComfyUI 未连接")

        payload = {
            "prompt": self._sanitize(workflow),
            "client_id": client_id or self.client_id,
        }

        try:
            r = self.session.post(self.base_url + "/prompt",
                                  json=payload, timeout=30)
        except Exception as e:
            raise ComfyError("提交请求失败：%s" % e)

        if r.status_code >= 500:
            raise ComfyError(
                "ComfyUI 服务端错误（%d）。常见原因：\n"
                "  1. 工作流里含非节点键（以 _ 开头的说明性字段）\n"
                "  2. 节点参数与模型不匹配\n"
                "  3. 显存不足\n"
                "原始响应：%s" % (r.status_code, r.text[:200]))
        if r.status_code != 200:
            raise ComfyError("提交失败（%d）：%s" % (r.status_code, r.text[:300]))

        try:
            j = r.json()
        except Exception:
            raise ComfyError("返回内容不是合法 JSON：%s" % r.text[:200])

        if "prompt_id" not in j:
            # ComfyUI 在节点校验失败时返回 error 字段
            err = j.get("error") or j
            detail = j.get("node_errors")
            msg = json.dumps(err, ensure_ascii=False)[:300]
            if detail:
                msg += " | 节点错误：%s" % json.dumps(
                    detail, ensure_ascii=False)[:300]
            raise ComfyError("工作流被拒绝：%s" % msg)
        return j["prompt_id"]

    @staticmethod
    def _sanitize(workflow):
        """去掉非节点键，避免 ComfyUI 解析崩溃。

        与 workflow.strip_meta 同义，这里独立实现是为了让
        comfy_client 不依赖 workflow 模块（分层清晰）。
        """
        if not isinstance(workflow, dict):
            return workflow
        out = {}
        for key, node in workflow.items():
            if str(key).startswith("_") or not isinstance(node, dict):
                continue
            out[key] = {k: v for k, v in node.items()
                        if not str(k).startswith("_")}
        return out

    # ------------------------------------------------------------------
    # 轮询状态
    # ------------------------------------------------------------------
    def poll(self, prompt_id):
        """查询任务状态。

        返回 dict：
            done     是否完成
            failed   是否失败
            progress 进度文字（若在队列中）
            images   输出图片信息列表
        """
        if not self.available:
            return {"done": False, "failed": True, "error": "未连接"}

        try:
            r = self.session.get(self.base_url + "/history/" + prompt_id,
                             timeout=15)
            if r.status_code != 200:
                return {"done": False, "failed": True,
                        "error": "查询失败 %d" % r.status_code}
            hist = r.json()
        except Exception as e:
            return {"done": False, "failed": True, "error": str(e)}

        if prompt_id not in hist:
            # 还没进历史，说明在队列里
            return {"done": False, "failed": False,
                    "progress": self.queue_position(prompt_id)}

        entry = hist[prompt_id]
        status = entry.get("status", {})
        if status.get("status_str") == "error":
            return {"done": False, "failed": True,
                    "error": "生成出错", "raw": status}

        images = []
        for node_id, out in (entry.get("outputs") or {}).items():
            for img in (out.get("images") or []):
                images.append({
                    "filename": img.get("filename"),
                    "subfolder": img.get("subfolder", ""),
                    "type": img.get("type", "output"),
                    "node_id": node_id,
                })

        return {"done": True, "failed": False, "images": images,
                "status": status.get("status_str")}

    def queue_position(self, prompt_id=None):
        """返回队列中还有多少任务在等待，供界面显示进度。"""
        if not self.available:
            return None
        try:
            r = self.session.get(self.base_url + "/queue", timeout=10)
            if r.status_code != 200:
                return None
            q = r.json()
            running = len(q.get("queue_running") or [])
            pending = len(q.get("queue_pending") or [])
            return {"running": running, "pending": pending}
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 取回结果
    # ------------------------------------------------------------------
    def fetch_image(self, image_info, out_path):
        """下载单张输出图片到本地。"""
        if not self.available:
            raise ComfyError("ComfyUI 未连接")
        params = {
            "filename": image_info["filename"],
            "subfolder": image_info.get("subfolder", ""),
            "type": image_info.get("type", "output"),
        }
        r = self.session.get(self.base_url + "/view", params=params, timeout=120)
        if r.status_code != 200:
            raise ComfyError("下载失败：%s" % r.text[:200])
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(r.content)
        return out_path

    def fetch_images(self, prompt_id, out_dir, prefix="work"):
        """取回某任务的全部输出图片，返回本地路径列表。"""
        st = self.poll(prompt_id)
        if not st.get("done"):
            return []
        paths = []
        os.makedirs(out_dir, exist_ok=True)
        for idx, info in enumerate(st.get("images") or []):
            ext = os.path.splitext(info["filename"])[1] or ".png"
            out = os.path.join(out_dir, "%s_%s_%d%s" % (prefix, prompt_id[:8], idx, ext))
            try:
                paths.append(self.fetch_image(info, out))
            except Exception:
                continue
        return paths

    # ------------------------------------------------------------------
    # 等待式接口（脚本或教师端批量任务用）
    # ------------------------------------------------------------------
    def run_and_wait(self, workflow, out_dir, prefix="work",
                     timeout=300, interval=2.0, on_progress=None):
        """提交并阻塞等待完成，返回输出图片路径列表。

        界面不建议用（会阻塞），教师端批量生成可以用。
        """
        pid = self.submit(workflow)
        start = time.time()
        while time.time() - start < timeout:
            st = self.poll(pid)
            if st.get("failed"):
                raise ComfyError(st.get("error", "生成失败"))
            if st.get("done"):
                return self.fetch_images(pid, out_dir, prefix)
            if on_progress:
                on_progress(st)
            time.sleep(interval)
        raise ComfyError("生成超时（%d 秒）" % timeout)


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------
def client_from_env():
    """从环境变量创建客户端。

    COMFYUI_URL  默认 http://127.0.0.1:8188
    """
    return ComfyClient(os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188"))


if __name__ == "__main__":
    c = client_from_env()
    print("ComfyUI 地址：", c.base_url)
    print("连接状态：", "已连接" if c.available else "未连接")
    if c.available:
        print("运行设备：", c.device_summary())
    else:
        print("原因：", c.error)
        if c.hint:
            print("提示：", c.hint)
        print()
        print("这是预期结果 —— 本机尚未安装 ComfyUI。")
        print("界面会自动降级为『仅检索』模式，功能检索不受影响。")
