# -*- coding: utf-8 -*-
"""
ComfyUI SSH 隧道

解决的问题：
    服务器上的 ComfyUI 通常只监听 127.0.0.1:8188（或虽然监听 0.0.0.0
    但云安全组不放行）。直接连公网 IP 连不上，
    而把 8188 暴露到公网又不安全。

    所以走 SSH 隧道：本地随机端口 -> SSH -> 远端的 127.0.0.1:8188。
    这样 ComfyUI 完全不用改动，也不用开公网端口。

设计要点：
    - 用 paramiko 的 direct-tcpip 通道手工转发，不依赖 ssh -L 子进程
      （Windows 上更可控，且能精确管理生命周期）
    - paramiko 是**可选依赖**：不用隧道时不装也能跑
    - 本地端口由系统分配（bind 0），避免与他人冲突

用法：
    # 推荐：按环境变量一次性建立，变量名与 README/docs 保持一致
    from comfy_tunnel import tunnel_from_env

    tun = tunnel_from_env()
    if tun:
        url = tun.base_url()      # http://127.0.0.1:54321
        # 用这个 url 构造 ComfyClient

    # 或者手动指定
    from comfy_tunnel import SSHTunnel

    tun = SSHTunnel(host="gpu.example.edu", port=22,
                    user="admire", password="xxx")
    tun.start()
    print(tun.base_url())

注：本模块实际导出的是 `tunnel_from_env` 和 `SSHTunnel`
（**不是** `get_tunnel`）。过去文档里写的 `from comfy_tunnel import
get_tunnel` 会直接 ImportError —— 已经把名字更正过来，
但保留这段说明，免得以后再被那份旧写法带偏。
"""

from __future__ import annotations

import os
import select
import socket
import threading


class TunnelError(Exception):
    pass


class SSHTunnel:
    """把远端地址映射到本地随机端口。"""

    def __init__(self, host, user, password=None, key_file=None,
                 ssh_port=22, remote_host="127.0.0.1", remote_port=8188,
                 connect_timeout=20):
        try:
            import paramiko
        except ImportError:
            raise TunnelError(
                "使用 SSH 隧道需要 paramiko：pip install paramiko\n"
                "（不用隧道的话不需要装它）")

        self.paramiko = paramiko
        self.remote = (remote_host, int(remote_port))
        self.cli = paramiko.SSHClient()
        self.cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        kwargs = dict(port=int(ssh_port), username=user,
                      timeout=connect_timeout,
                      banner_timeout=connect_timeout,
                      auth_timeout=connect_timeout)
        if key_file:
            kwargs["key_filename"] = key_file
        if password:
            kwargs["password"] = password

        try:
            self.cli.connect(host, **kwargs)
        except Exception as e:
            raise TunnelError("SSH 连接失败：%s" % e)

        self.transport = self.cli.get_transport()
        if self.transport is None:
            raise TunnelError("SSH 通道未建立")

        self.local_port = None
        self._server = None
        self._stopping = False

    # ------------------------------------------------------------------
    def start(self):
        """绑定本地随机端口并开始转发，返回本地端口号。"""
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))   # 0 = 让系统分配空闲端口
        self._server.listen(16)
        self.local_port = self._server.getsockname()[1]

        t = threading.Thread(target=self._accept_loop, daemon=True)
        t.start()
        return self.local_port

    def _accept_loop(self):
        while not self._stopping:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._pipe, args=(conn,),
                             daemon=True).start()

    def _pipe(self, conn):
        """双向转发一条连接。"""
        try:
            chan = self.transport.open_channel(
                "direct-tcpip", self.remote, conn.getpeername())
        except Exception:
            conn.close()
            return

        try:
            while True:
                r, _, _ = select.select([conn, chan], [], [], 0.5)
                if conn in r:
                    data = conn.recv(8192)
                    if not data:
                        break
                    chan.sendall(data)
                if chan in r:
                    data = chan.recv(8192)
                    if not data:
                        break
                    conn.sendall(data)
        except Exception:
            pass
        finally:
            for x in (chan, conn):
                try:
                    x.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    def base_url(self):
        if not self.local_port:
            raise TunnelError("隧道尚未启动，先调用 start()")
        return "http://127.0.0.1:%d" % self.local_port

    def stop(self):
        self._stopping = True
        try:
            if self._server:
                self._server.close()
        except Exception:
            pass
        try:
            self.cli.close()
        except Exception:
            pass


# ----------------------------------------------------------------------
def tunnel_from_env():
    """按环境变量建隧道。

    COMFYUI_SSH_HOST      服务器地址（设了才启用隧道）
    COMFYUI_SSH_PORT      SSH 端口，默认 22
    COMFYUI_SSH_USER      SSH 用户名
    COMFYUI_SSH_PASSWORD  SSH 密码（或用密钥）
    COMFYUI_SSH_KEY       SSH 私钥路径（比密码更推荐）
    COMFYUI_REMOTE       ComfyUI 在服务器上的地址，默认 127.0.0.1:8188

    返回 SSHTunnel 或 None（未配置时）。
    """
    host = os.environ.get("COMFYUI_SSH_HOST", "").strip()
    if not host:
        return None

    remote = os.environ.get("COMFYUI_REMOTE", "127.0.0.1:8188")
    rh, _, rp = remote.partition(":")

    tun = SSHTunnel(
        host=host,
        ssh_port=int(os.environ.get("COMFYUI_SSH_PORT", "22")),
        user=os.environ.get("COMFYUI_SSH_USER", ""),
        password=os.environ.get("COMFYUI_SSH_PASSWORD") or None,
        key_file=os.environ.get("COMFYUI_SSH_KEY") or None,
        remote_host=rh or "127.0.0.1",
        remote_port=int(rp or "8188"),
    )
    tun.start()
    return tun


if __name__ == "__main__":
    """自检：建隧道并用它访问 ComfyUI 的 /system_stats。"""
    import json
    import urllib.request

    tun = tunnel_from_env()
    if tun is None:
        print("未配置 COMFYUI_SSH_HOST，隧道未启用。")
        print("设置示例：")
        print("  set COMFYUI_SSH_HOST=gpu.example.edu")
        print("  set COMFYUI_SSH_PORT=22")
        print("  set COMFYUI_SSH_USER=your_user")
        print("  set COMFYUI_SSH_PASSWORD=your_password")
        raise SystemExit(1)

    try:
        url = tun.base_url()
        print("隧道已建立：%s -> %s:%d" % (url, *tun.remote))

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}))
        with opener.open(url + "/system_stats", timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        print("ComfyUI 版本：%s" % data.get("system", {})
              .get("comfyui_version", "?"))
        for d in data.get("devices", []):
            print("  GPU：%s  空闲 %.1fGB"
                  % (d.get("name", "?"),
                     (d.get("vram_free") or 0) / (1024 ** 3)))
    finally:
        tun.stop()
