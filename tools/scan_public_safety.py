# -*- coding: utf-8 -*-
"""推送到公开仓库之前的安全扫描。

公开仓库等于世界可见，所以只扫"受版本控制的文件" —— 没被 git 跟踪的
东西不会被推上去，不用管。
"""
import os
import re
import subprocess
import sys

# 本脚本就放在仓库根目录，所以只回退一级。
# （曾经写成两级回退，结果 git ls-files 跑在了仓库外，
#   返回空列表 → "扫描了 0 个文件，全部干净"，是个假绿灯。）
ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)

TEXT_EXT = (".py", ".md", ".txt", ".csv", ".json", ".yml", ".yaml",
            ".sh", ".bat", ".example", ".gitignore")
# 排除显然无害的例子/占位符
ALLOW = ("example.com", "@example", "your@", "username@", "user@host",
         "miao-system@")

PATTERNS = {
    "邮箱/账号": re.compile(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "Windows 用户目录": re.compile(r"[Cc]:\\Users\\[A-Za-z0-9_.-]+"),
    "Linux/home 目录": re.compile(r"/home/[a-z][a-z0-9_-]*"),
    "疑似密钥/口令": re.compile(
        r"(password|passwd|secret|token|api_key|access_key)\s*[:=]\s*"
        r"['\"][^'\"\s]{8,}['\"]", re.I),
    "IP 地址": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
}


def main():
    out = subprocess.run(["git", "ls-files"], capture_output=True,
                         text=True, encoding="utf-8").stdout
    files = [p.strip() for p in out.splitlines() if p.strip()]
    scanned = 0
    findings = {k: [] for k in PATTERNS}

    for rel in files:
        if not rel.lower().endswith(TEXT_EXT):
            continue
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            continue
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        scanned += 1
        for name, pat in PATTERNS.items():
            for m in sorted(set(pat.findall(text))):
                if any(a in m.lower() for a in ALLOW):
                    continue
                # 版本号里的 0.0.0.0 / 127.0.0.1 之类放行
                if name == "IP 地址" and m.startswith(("0.", "127.", "255.")):
                    continue
                findings[name].append((rel, m))

    print("扫描了 %d 个受版本控制的文本文件\n" % scanned)
    total = 0
    for name, hits in findings.items():
        if not hits:
            print("  [干净] %s" % name)
            continue
        print("  [发现 %d 处] %s" % (len(hits), name))
        for rel, m in hits[:20]:
            print("        %s -> %s" % (rel, m))
        total += len(hits)
    print("\n合计 %d 处需要人工判断" % total)
    return total


if __name__ == "__main__":
    sys.exit(0 if main() == 0 else 1)
