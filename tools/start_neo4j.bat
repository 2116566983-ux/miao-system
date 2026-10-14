@echo off
chcp 65001 >nul
REM ==========================================================================
REM 启动本地 Neo4j（苗绣纹样教学用）
REM ==========================================================================
REM 为什么不用自带的 bin\neo4j.bat？
REM   官方 .bat 会转调 neo4j.ps1，在我们的环境下 PowerShell 会把真实
REM   报错吞掉（只留一个 WriteErrorException）。所以这里直接调 Java 入口，
REM   报错能完整看到。实测可用。
REM
REM 用法：双击本文件。启动后保持窗口开着，关窗即停止数据库。
REM ==========================================================================

setlocal

REM ==== 从本地私有配置读取（该文件不进版本库）====
REM 复制 ../start.local.bat.example 为 ../start.local.bat，
REM 填入自己的 NEO4J_HOME 与 NEO4J_PASSWORD。
if exist "%~dp0..\start.local.bat" call "%~dp0..\start.local.bat"

REM ---- 若上面没提供，用下面的默认值（请按自己机器改）----
REM NEO4J_HOME 是 Neo4j 解压目录，必须指向含 lib\ 与 conf\ 的那一层
if not defined NEO4J_HOME set NEO4J_HOME=C:\neo4j\neo4j-community-5.26.0
REM 本机 Neo4j 密码；留空也能启动，但导入数据时要另设
if not defined NEO4J_PASSWORD set NEO4J_PASSWORD=

if not exist "%NEO4J_HOME%\lib" (
    echo [错误] 找不到 Neo4j：%NEO4J_HOME%
    echo        请修改本文件顶部的 NEO4J_HOME 变量。
    pause
    exit /b 1
)

cd /d "%NEO4J_HOME%"

echo ============================================================
echo   Neo4j 社区版 5.26.0 · 本地实例
echo ============================================================
echo.
echo   浏览器管理界面：http://localhost:7474
echo   Bolt 连接地址 ：bolt://localhost:7687
echo   用户名        ：neo4j
echo   （密码从 start.local.bat 读取，不在此显示）
echo.
echo   首次使用请先导入数据：
echo     python tools\import_to_neo4j.py
echo.
echo   按 Ctrl+C 或直接关闭本窗口即可停止数据库。
echo ============================================================
echo.

java -cp "lib/*;conf" ^
     -Dserver.directories.neo4j_home="%NEO4J_HOME%" ^
     org.neo4j.server.CommunityEntryPoint ^
     --home-dir "%NEO4J_HOME%" ^
     --config-dir "%NEO4J_HOME%\conf"

endlocal
pause
