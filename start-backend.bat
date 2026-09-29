@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  nacho 后端启动脚本
rem  启动 FastAPI 接口层（默认 http://127.0.0.1:18080）
rem  依赖：python 3.12+，以及 nacho[api] / nacho[onebot] 等依赖
rem ============================================================

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没找到 python，请先安装 Python 3.12+ 并加入 PATH。
    pause
    exit /b 1
)

echo 正在启动 nacho 后端（http://127.0.0.1:18080）...
echo 按 Ctrl+C 停止。
echo.

python app.py

echo.
echo 后端已退出。
pause
