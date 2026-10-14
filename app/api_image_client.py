# -*- coding: utf-8 -*-
"""
云端生图 API 对接层（免费方案）

为什么需要这一层：
    本地 ComfyUI 需要下载 2GB 本体 + 数个 GB 模型，且需要 GPU 常开。
    对于公选课教学场景，很多时候更希望"填个 key 就能出图"。
    因此把生成后端抽象成可插拔的一层，与 ComfyUI 并列：

        ComfyUI(本地 GPU)  →  免费云端 API  →  不可用

    界面只需要调用统一的接口，不关心底下是谁在出图。
    以后想换成本地 ComfyUI，改环境变量即可，业务代码不动。

支持的接口形态：
    1. ModelScope API-Inference（默认，国内直连，有每日免费额度）
       Base: https://api-inference.modelscope.cn/v1
       图生图模型: Qwen/Qwen-Image-Edit
    2. 任意 OpenAI 兼容的 /v1/images/generations 端点

环境变量：
    IMAGE_API_KEY    必填，API 密钥
    IMAGE_API_BASE   可选，默认 ModelScope 地址
    IMAGE_API_MODEL  可选，默认 Qwen/Qwen-Image-Edit
    IMAGE_API_MODE   可选，img2img / txt2img，默认 img2img

用法：
    from api_image_client import ApiImageClient
    c = ApiImageClient.from_env()
    if c.available:
        paths = c.generate(prompt, image_path="素材.jpg", out_dir="data/generated")
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid

try:
    import requests
except ImportError:
    requests = None


# ---------------------------------------------------------------------------
# 预设：常见免费/低价云端生图服务
# ---------------------------------------------------------------------------
PROVIDERS = {
    "modelscope": {
        "name": "魔搭 ModelScope",
        "base": "https://api-inference.modelscope.cn/v1",
        "model": "Qwen/Qwen-Image-Edit",
        "model_txt2img": "Qwen/Qwen-Image",
        "note": "国内直连，需实名认证，每日有免费额度，仅限非商业用途。",
    },
    "openai_compat": {
        "name": "OpenAI 兼容接口",
        "base": "",
        "model": "",
        "note": "任何符合 /v1/images/generations 规范的服务都可以接入。",
    },
}


class ApiImageError(Exception):
    pass


def _no_proxy_session(base_url):
    """本地/局域网地址绕过系统代理；公网地址走正常网络。

    与 comfy_client 同样的处理：有些机器设了 HTTP_PROXY，
    requests 会连本地服务也走代理，导致莫名其妙的 502。
    """
    s = requests.Session()
    if "127.0.0.1" in base_url or "localhost" in base_url:
        s.trust_env = False
    return s


class ApiImageClient:
    """云端生图 API 客户端。

    设计要点：
        - 连不上或没配 key 时 available=False，界面降级，不抛异常
        - 图生图时把参考图以 base64 data URI 传过去（兼容性最好）
        - 失败时给出可操作的中文原因，而不是裸的 HTTP 状态码
    """

    def __init__(self, api_key=None, base_url=None, model=None,
                 timeout=180.0, provider="modelscope"):
        self.provider = provider
        p = PROVIDERS.get(provider, PROVIDERS["modelscope"])

        self.api_key = api_key or os.environ.get("IMAGE_API_KEY", "").strip()
        self.base_url = (base_url
                         or os.environ.get("IMAGE_API_BASE", "").strip()
                         or p["base"]).rstrip("/")
        self.model = (model
                      or os.environ.get("IMAGE_API_MODEL", "").strip()
                      or p["model"])
        self.timeout = timeout
        self.available = False
        self.error = None
        self.hint = None
        self.last_raw = None

        if requests is None:
            self.error = "未安装 requests 依赖"
            self.hint = "pip install requests"
            return

        self.session = _no_proxy_session(self.base_url)

        if not self.api_key:
            self.error = "未配置 API 密钥"
            self.hint = ("设置环境变量 IMAGE_API_KEY 后重启系统。"
                         "免费密钥获取：https://modelscope.cn （需阿里云实名）")
            return

        if not self.base_url:
            self.error = "未配置 API 地址"
            self.hint = "设置环境变量 IMAGE_API_BASE"
            return

        self.available = True

    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls):
        """从环境变量构造，供界面直接调用。"""
        return cls(provider=os.environ.get("IMAGE_API_PROVIDER", "modelscope"))

    # ------------------------------------------------------------------
    def probe(self):
        """轻量探活：只验证密钥与端点是否可用，不真的出图。

        返回 (ok: bool, message: str)
        """
        if not self.available:
            return False, self.error
        try:
            r = self.session.get(self.base_url + "/models",
                                 headers=self._headers(), timeout=15)
            if r.status_code == 200:
                return True, "密钥有效，接口可用"
            if r.status_code in (401, 403):
                self.error = "API 密钥无效或无权限（%d）" % r.status_code
                self.hint = "请确认 IMAGE_API_KEY 填写正确，且账号已完成实名认证。"
                return False, self.error
            return False, "探活返回状态码 %d" % r.status_code
        except Exception as e:
            return False, "探活失败：%s" % e

    # ------------------------------------------------------------------
    def _headers(self):
        h = {"Authorization": "Bearer %s" % self.api_key}
        # ModelScope 的异步生图需要这个头
        h["X-ModelScope-Async-Mode"] = "true"
        return h

    @staticmethod
    def _to_data_uri(image_path):
        """把本地图片转成 data URI，用于图生图输入。

        用 base64 内联而不是先上传，是因为各家 API 的图片上传接口
        格式不统一，而 data URI 基本都被支持，兼容性最好。
        """
        ext = os.path.splitext(image_path)[1].lower().lstrip(".") or "jpeg"
        if ext == "jpg":
            ext = "jpeg"
        mime = "image/%s" % ext
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return "data:%s;base64,%s" % (mime, b64)

    # ------------------------------------------------------------------
    def generate(self, prompt, image_path=None, out_dir="data/generated",
                 negative=None, size="1024x1024", prefix="api", steps=None):
        """生成图片，返回本地文件路径列表。

        参数：
            prompt      提示词（中英文均可，ModelScope 中文支持好）
            image_path  参考图路径。给了就是图生图，不给就是文生图
            out_dir     输出目录
            negative    反向提示词（部分服务支持）
            size        尺寸，格式 "宽x高"
        """
        if not self.available:
            raise ApiImageError(self.error or "接口不可用")

        payload = {
            "model": self.model,
            "prompt": prompt,
            "n": 1,
            "size": size,
        }
        if negative:
            payload["negative_prompt"] = negative
        if steps:
            payload["steps"] = steps

        if image_path:
            if not os.path.exists(image_path):
                raise ApiImageError("参考图不存在：%s" % image_path)
            payload["image"] = self._to_data_uri(image_path)

        try:
            r = self.session.post(self.base_url + "/images/generations",
                                  headers=self._headers(),
                                  json=payload,
                                  timeout=self.timeout)
        except requests.exceptions.Timeout:
            raise ApiImageError("请求超时（%d 秒）。生图较慢，可稍后重试。" % self.timeout)
        except Exception as e:
            raise ApiImageError("请求失败：%s" % e)

        if r.status_code in (401, 403):
            raise ApiImageError("密钥无效或无权限（%d）。请检查 IMAGE_API_KEY。" % r.status_code)
        if r.status_code == 429:
            raise ApiImageError("请求过于频繁或免费额度已用完（429）。"
                                "额度每日重置，请明天再试。")
        if r.status_code != 200:
            raise ApiImageError("接口返回 %d：%s" % (r.status_code, r.text[:300]))

        try:
            j = r.json()
        except Exception:
            raise ApiImageError("返回内容不是合法 JSON：%s" % r.text[:200])

        self.last_raw = j

        # ModelScope 是异步任务：先拿 task_id，再轮询
        if "task_id" in j:
            return self._wait_task(j["task_id"], out_dir, prefix)

        return self._extract_and_save(j, out_dir, prefix)

    # ------------------------------------------------------------------
    def _wait_task(self, task_id, out_dir, prefix, timeout=300, interval=3.0):
        """ModelScope 异步任务的轮询与下载。"""
        url = "%s/tasks/%s" % (self.base_url, task_id)
        headers = {"Authorization": "Bearer %s" % self.api_key,
                   "X-ModelScope-Task-Type": "image_generation"}
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = self.session.get(url, headers=headers, timeout=30)
            except Exception as e:
                raise ApiImageError("查询任务状态失败：%s" % e)

            if r.status_code != 200:
                raise ApiImageError("查询任务失败（%d）" % r.status_code)

            j = r.json()
            status = (j.get("task_status") or "").upper()
            if status == "SUCCEED":
                return self._extract_and_save(j, out_dir, prefix)
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                msg = j.get("message") or j.get("errors") or status
                raise ApiImageError("生成失败：%s" % str(msg)[:300])
            time.sleep(interval)

        raise ApiImageError("生成超时（%d 秒）" % timeout)

    # ------------------------------------------------------------------
    def _extract_and_save(self, payload, out_dir, prefix):
        """从返回体里取出图片并落盘。

        各家返回结构不统一，这里做兼容：
            - ModelScope 异步: {"output_images": ["https://..."]}
            - OpenAI 兼容:     {"data": [{"b64_json": "..."}]}
            - OpenAI 兼容 URL: {"data": [{"url": "https://..."}]}
        """
        os.makedirs(out_dir, exist_ok=True)
        urls = []
        b64s = []

        def _take(value):
            """把返回体里的一项归入"链接"或"已编码图片"。

            ModelScope 有时给 https 链接，有时直接给 data URI，
            两种都要接住，否则会误报"没有图片数据"。
            """
            if not isinstance(value, str) or not value:
                return
            if value.startswith("data:"):
                _, _, b64 = value.partition(",")
                if b64:
                    b64s.append(b64)
            elif value.startswith("http"):
                urls.append(value)

        # 形态一：ModelScope 的 output_images
        for u in (payload.get("output_images") or []):
            _take(u)

        # 形态二：OpenAI 风格的 data 数组
        for item in (payload.get("data") or []):
            if isinstance(item, dict):
                if item.get("b64_json"):
                    b64s.append(item["b64_json"])
                elif item.get("url"):
                    _take(item["url"])
            else:
                _take(item)

        if not urls and not b64s:
            raise ApiImageError(
                "返回体里没有图片数据。原始响应：%s"
                % json.dumps(payload, ensure_ascii=False)[:300])

        paths = []
        stamp = "%s_%s" % (time.strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:6])

        for i, b64 in enumerate(b64s):
            out = os.path.join(out_dir, "%s_%s_%d.png" % (prefix, stamp, i))
            with open(out, "wb") as f:
                f.write(base64.b64decode(b64))
            paths.append(out)

        for i, u in enumerate(urls):
            out = os.path.join(out_dir, "%s_%s_%d.png" % (prefix, stamp, len(b64s) + i))
            try:
                rr = self.session.get(u, timeout=120)
                if rr.status_code == 200:
                    with open(out, "wb") as f:
                        f.write(rr.content)
                    paths.append(out)
            except Exception:
                continue

        if not paths:
            raise ApiImageError("图片下载失败，请检查网络或稍后重试。")
        return paths


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    c = ApiImageClient.from_env()
    print("服务商：", PROVIDERS.get(c.provider, {}).get("name", c.provider))
    print("接口地址：", c.base_url)
    print("使用模型：", c.model)
    print("密钥状态：", "已配置" if c.api_key else "未配置")
    if not c.available:
        print("不可用原因：", c.error)
        if c.hint:
            print("处理建议：", c.hint)
    else:
        ok, msg = c.probe()
        print("探活结果：", msg)
