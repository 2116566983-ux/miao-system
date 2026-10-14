#!/usr/bin/env bash
# ============================================================================
# 苗绣纹样创作系统 · Linux 服务器部署脚本
# ============================================================================
#
# 用途：在 Ubuntu/CentOS 服务器上部署 Streamlit 系统，
#       对接已有的 ComfyUI（通常在 127.0.0.1:8188）。
#
# 安全设计（重要）：
#   1. 全部装在独立目录 ~/miao-system/，不碰系统任何文件
#   2. 用独立 venv，不污染系统 Python，不碰别人已有的 conda 环境
#   3. 不修改防火墙、不申请 root、不动已有的 ComfyUI
#   4. 卸载 = 删除目录，服务器恢复原样
#
# 用法：
#   bash deploy_server.sh              # 默认部署到 ~/miao-system
#   bash deploy_server.sh --port 8501  # 指定 Streamlit 端口
#   bash deploy_server.sh --dry-run    # 只检查不安装
#
# ============================================================================

set -e

# ---- 配置 ----
INSTALL_DIR="${INSTALL_DIR:-$HOME/miao-system}"
VENV_DIR="$INSTALL_DIR/venv"
PORT=8501
DRY_RUN=0

# ComfyUI 地址：同机默认走本地回环，不经过公网
export COMFYUI_URL="${COMFYUI_URL:-http://127.0.0.1:8188}"

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --dir) INSTALL_DIR="$2"; VENV_DIR="$INSTALL_DIR/venv"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) shift ;;
  esac
done

echo "============================================================"
echo "  苗绣纹样创作系统 · 服务器部署"
echo "============================================================"
echo "  安装目录：$INSTALL_DIR"
echo "  虚拟环境：$VENV_DIR"
echo "  服务端口：$PORT"
echo "  ComfyUI ：$COMFYUI_URL"
echo "  模式     ：$([ "$DRY_RUN" -eq 1 ] && echo '检查模式（不安装）' || echo '正式安装')"
echo "============================================================"
echo

# ---------------------------------------------------------------------------
# 第 1 步：环境检查
# ---------------------------------------------------------------------------
echo "[1/6] 环境检查"

# Python
if command -v python3 >/dev/null 2>&1; then
  PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  echo "  Python：$PY_VER"
  PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3,9) else 0)')
  if [ "$PY_OK" != "1" ]; then
    echo "  [错误] 需要 Python 3.9 或更高版本"
    exit 1
  fi
else
  echo "  [错误] 未找到 python3"
  echo "         Ubuntu: sudo apt install python3 python3-venv python3-pip"
  exit 1
fi

# venv 模块
if ! python3 -c 'import venv' 2>/dev/null; then
  echo "  [错误] 缺少 venv 模块"
  echo "         Ubuntu: sudo apt install python3-venv"
  exit 1
fi

# 磁盘空间（需要约 1GB）
AVAIL_KB=$(df -Pk "$HOME" | tail -1 | awk '{print $4}')
AVAIL_GB=$((AVAIL_KB / 1024 / 1024))
echo "  可用磁盘：${AVAIL_GB} GB"
if [ "$AVAIL_GB" -lt 2 ]; then
  echo "  [警告] 磁盘空间不足 2GB，建议先清理"
fi

# 端口占用
if command -v ss >/dev/null 2>&1; then
  if ss -tln 2>/dev/null | grep -q ":$PORT "; then
    echo "  [警告] 端口 $PORT 已被占用，请用 --port 换一个"
    PORT_BUSY=1
  else
    echo "  端口 $PORT：空闲"
    PORT_BUSY=0
  fi
fi

# ComfyUI 连通性（只探测，不启动）
echo -n "  ComfyUI："
if command -v curl >/dev/null 2>&1; then
  if curl -s --max-time 5 "$COMFYUI_URL/system_stats" >/dev/null 2>&1; then
    echo "可连接 ✓"
  else
    echo "连不上（部署后仍可启动，但无法生成）"
    echo "         请确认 ComfyUI 已在运行，地址是否正确"
  fi
else
  echo "未装 curl，跳过检测"
fi

echo

if [ "$DRY_RUN" -eq 1 ]; then
  echo "检查模式结束，未做任何修改。"
  exit 0
fi

# ---------------------------------------------------------------------------
# 第 2 步：复制文件
# ---------------------------------------------------------------------------
echo "[2/6] 准备安装目录"

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ "$SRC_DIR" = "$INSTALL_DIR" ]; then
  echo "  已在安装目录内，跳过复制"
else
  mkdir -p "$INSTALL_DIR"
  # 用 rsync 优先（可增量），没有则用 cp
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --exclude 'venv' --exclude '__pycache__' \
          --exclude '*.pyc' --exclude 'tools/probe_server.py' \
          "$SRC_DIR/" "$INSTALL_DIR/"
  else
    cp -r "$SRC_DIR"/. "$INSTALL_DIR"/
    rm -rf "$INSTALL_DIR/venv" "$INSTALL_DIR/__pycache__"
    find "$INSTALL_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
    # 探测脚本含服务器信息，不进部署环境
    rm -f "$INSTALL_DIR/tools/probe_server.py"
  fi
  echo "  文件已复制到 $INSTALL_DIR"
fi

echo

# ---------------------------------------------------------------------------
# 第 3 步：创建独立虚拟环境
# ---------------------------------------------------------------------------
echo "[3/6] 创建独立虚拟环境"

if [ -d "$VENV_DIR" ]; then
  echo "  虚拟环境已存在，复用"
else
  python3 -m venv "$VENV_DIR"
  echo "  已创建 $VENV_DIR"
fi

echo

# ---------------------------------------------------------------------------
# 第 4 步：安装依赖
# ---------------------------------------------------------------------------
echo "[4/6] 安装依赖（首次约需 1-3 分钟）"

"$VENV_DIR/bin/pip" install --upgrade pip -q

# 用国内镜像加速（服务器在国内）
PIP_ARGS="-q"
if [ "${USE_MIRROR:-1}" = "1" ]; then
  PIP_ARGS="$PIP_ARGS -i https://pypi.tuna.tsinghua.edu.cn/simple"
  echo "  使用清华镜像加速"
fi

"$VENV_DIR/bin/pip" install $PIP_ARGS -r "$INSTALL_DIR/requirements.txt"

echo "  依赖安装完成"

echo

# ---------------------------------------------------------------------------
# 第 5 步：环境自检
# ---------------------------------------------------------------------------
echo "[5/6] 运行环境自检"

cd "$INSTALL_DIR"
"$VENV_DIR/bin/python" check_env.py || true

echo

# ---------------------------------------------------------------------------
# 第 6 步：生成启动脚本
# ---------------------------------------------------------------------------
echo "[6/6] 生成启动脚本"

cat > "$INSTALL_DIR/run.sh" <<RUNEOF
#!/usr/bin/env bash
# 启动苗绣纹样创作系统
export COMFYUI_URL="\${COMFYUI_URL:-$COMFYUI_URL}"
export IMAGE_API_KEY="\${IMAGE_API_KEY:-}"

cd "\$(dirname "\$0")"
exec "$VENV_DIR/bin/python" -m streamlit run app/streamlit_app.py \\
  --server.port $PORT \\
  --server.address 0.0.0.0 \\
  --server.headless true \\
  --browser.gatherUsageStats false
RUNEOF

chmod +x "$INSTALL_DIR/run.sh"

cat > "$INSTALL_DIR/stop.sh" <<STOPEOF
#!/usr/bin/env bash
# 停止系统
PID=\$(pgrep -f "streamlit run app/streamlit_app.py" | head -1)
if [ -n "\$PID" ]; then
  kill "\$PID" && echo "已停止（PID \$PID）"
else
  echo "未在运行"
fi
STOPEOF

chmod +x "$INSTALL_DIR/stop.sh"

echo "  已生成 run.sh / stop.sh"
echo

# ---------------------------------------------------------------------------
# 完成
# ---------------------------------------------------------------------------
echo "============================================================"
echo "  部署完成"
echo "============================================================"
echo
echo "  启动服务："
echo "     bash $INSTALL_DIR/run.sh"
echo
echo "  后台运行（关掉终端也不断）："
echo "     nohup bash $INSTALL_DIR/run.sh > $INSTALL_DIR/app.log 2>&1 &"
echo
echo "  停止服务："
echo "     bash $INSTALL_DIR/stop.sh"
echo
echo "  学生访问地址："
echo "     http://<服务器公网IP>:$PORT"
echo
echo "  ⚠️ 若外网访问不了，需要在云服务商控制台的安全组里"
echo "     放行 TCP $PORT 端口（本脚本不会自动改防火墙）"
echo
echo "  完全卸载："
echo "     bash $INSTALL_DIR/stop.sh && rm -rf $INSTALL_DIR"
echo "     （服务器即恢复原样，不影响其他任何东西）"
echo
echo "============================================================"
