@echo off
chcp 65001 >nul
REM ==========================================================================
REM 苗绣纹样创作系统 · 一键启动（Windows）
REM ==========================================================================
REM 用法：双击本文件，或在命令行运行 start.bat
REM 如需连接 Neo4j / ComfyUI，先修改下面两行
REM ==========================================================================

setlocal

REM ---- 从本地私有配置读取凭据（该文件不进版本库）----
REM 首次使用：把 start.local.bat.example 复制为 start.local.bat，
REM 在里面填自己的 Neo4j 密码 / 云端 API 密钥。
REM 谁的本机谁维护，仓库里永远不含真实凭据。
if exist "%~dp0start.local.bat" (
    call "%~dp0start.local.bat"
) else (
    echo [提示] 未找到 start.local.bat，将使用默认配置。
    echo        连不上 Neo4j 会自动降级为 CSV 模式，功能不受影响。
    echo        需要连数据库/云端 API：复制 start.local.bat.example
    echo        为 start.local.bat 并填入自己的凭据。
    echo.
)

REM ---- 以下为默认值：若 start.local.bat 已设置则不覆盖 ----
if not defined NEO4J_URI set NEO4J_URI=bolt://localhost:7687
if not defined NEO4J_USER set NEO4J_USER=neo4j
REM 留空则自动降级为 CSV 模式，功能不受影响。
REM 启动本地数据库：双击 tools\start_neo4j.bat
if not defined NEO4J_PASSWORD set NEO4J_PASSWORD=
if not defined COMFYUI_URL set COMFYUI_URL=http://127.0.0.1:8188

REM ---- 免费云端生图 API（可选，不填则自动降级为「仅检索」）----
REM 密钥申请：https://modelscope.cn （需阿里云实名认证）
REM 详见 docs/free-api-guide.md
REM 建议填在 start.local.bat 里，不要写在本文件（本文件会进版本库）
if not defined IMAGE_API_KEY set IMAGE_API_KEY=
if not defined IMAGE_API_MODEL set IMAGE_API_MODEL=Qwen/Qwen-Image-Edit

REM ---- 远端 ComfyUI（可选）：设了 HOST 就自动建 SSH 隧道 ----
REM 详见 docs/remote-comfyui.md
if not defined COMFYUI_SSH_HOST set COMFYUI_SSH_HOST=
if not defined COMFYUI_SSH_PORT set COMFYUI_SSH_PORT=22
if not defined COMFYUI_SSH_USER set COMFYUI_SSH_USER=
if not defined COMFYUI_SSH_KEY set COMFYUI_SSH_KEY=

REM ---- 图片目录（默认从元数据读取，此处可覆盖）----
REM set MIAO_IMAGE_ROOT=C:\path\to\images

REM ---- 界面端口 ----
set PORT=8501

cd /d "%~dp0"

echo ============================================================
echo   苗绣纹样创作系统
echo ============================================================
echo.

REM 检测 Python
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.9 以上版本
    echo        下载地址：https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/3] 环境自检...
python check_env.py
echo.

echo [2/3] 启动服务（端口 %PORT%）...
echo.
echo   启动后请在浏览器访问：http://localhost:%PORT%
echo   按 Ctrl+C 可停止服务
echo.
echo ============================================================
echo.

echo [3/3] 打开浏览器...
start "" "http://localhost:%PORT%"

python -m streamlit run app\streamlit_app.py --server.port %PORT% --browser.gatherUsageStats false

endlocal
