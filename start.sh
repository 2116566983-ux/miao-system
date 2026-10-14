#!/usr/bin/env bash
# ============================================================================
# 苗绣纹样创作系统 · 一键启动（Linux / macOS / Git Bash）
# ============================================================================
# 用法：
#   ./start.sh              正常启动
#   ./start.sh --no-check   跳过环境自检
#   ./start.sh --port 8502  指定端口
# ============================================================================

set -e

# ---- 默认连接配置（按需修改，或改用环境变量覆盖）----
export NEO4J_URI="${NEO4J_URI:-bolt://localhost:7687}"
export NEO4J_USER="${NEO4J_USER:-neo4j}"
export NEO4J_PASSWORD="${NEO4J_PASSWORD:-}"
export COMFYUI_URL="${COMFYUI_URL:-http://127.0.0.1:8188}"

# ---- 免费云端生图 API（可选，不配则自动降级）----
# 密钥申请：https://modelscope.cn （需阿里云实名认证）
# 详见 docs/free-api-guide.md
export IMAGE_API_KEY="${IMAGE_API_KEY:-}"
export IMAGE_API_MODEL="${IMAGE_API_MODEL:-Qwen/Qwen-Image-Edit}"

# ---- 远端 ComfyUI（可选）：设了 HOST 就自动建 SSH 隧道 ----
# 详见 docs/remote-comfyui.md
export COMFYUI_SSH_HOST="${COMFYUI_SSH_HOST:-}"
export COMFYUI_SSH_PORT="${COMFYUI_SSH_PORT:-22}"
export COMFYUI_SSH_USER="${COMFYUI_SSH_USER:-}"
export COMFYUI_SSH_KEY="${COMFYUI_SSH_KEY:-}"

# ---- 解析参数 ----
PORT=8501
SKIP_CHECK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --no-check) SKIP_CHECK=1; shift ;;
    --port) PORT="$2"; shift 2 ;;
    *) shift ;;
  esac
done

cd "$(dirname "$0")"

echo "============================================================"
echo "  苗绣纹样创作系统"
echo "============================================================"
echo

# 检测 Python
if ! command -v python >/dev/null 2>&1; then
  if command -v python3 >/dev/null 2>&1; then
    PY=python3
  else
    echo "[错误] 未找到 Python，请先安装 Python 3.9 以上版本"
    exit 1
  fi
else
  PY=python
fi

if [ "$SKIP_CHECK" -eq 0 ]; then
  echo "[1/3] 环境自检..."
  "$PY" check_env.py || true
  echo
else
  echo "[1/3] 已跳过环境自检"
  echo
fi

echo "[2/3] 启动服务（端口 $PORT）..."
echo
echo "  启动后请在浏览器访问：http://localhost:$PORT"
echo "  按 Ctrl+C 可停止服务"
echo
echo "============================================================"
echo

echo "[3/3] 正在打开浏览器..."
if command -v xdg-open >/dev/null 2>&1; then
  (sleep 3 && xdg-open "http://localhost:$PORT" >/dev/null 2>&1) &
elif command -v open >/dev/null 2>&1; then
  (sleep 3 && open "http://localhost:$PORT" >/dev/null 2>&1) &
elif command -v start >/dev/null 2>&1; then
  (sleep 3 && start "http://localhost:$PORT" >/dev/null 2>&1) &
fi

exec "$PY" -m streamlit run app/streamlit_app.py \
  --server.port "$PORT" \
  --browser.gatherUsageStats false
