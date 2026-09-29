@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  nacho 一键启动脚本（前后端同时起）
rem  后端 FastAPI : http://127.0.0.1:18080
rem  前端 Vite    : http://127.0.0.1:5273
rem  两个窗口各跑一边；关掉对应窗口即停止对应服务
rem ============================================================

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没找到 python，请先安装 Python 3.12+ 并加入 PATH。
    pause
    exit /b 1
)

where node >nul 2>nul
if errorlevel 1 (
    echo [错误] 没找到 node，请先安装 Node.js 18+ 并加入 PATH。
    pause
    exit /b 1
)

rem 首次运行（前端还没有 node_modules）时先装依赖
if not exist "frontend\node_modules" (
    echo 首次运行，正在安装前端依赖（npm install）...
    pushd frontend
    call npm install
    popd
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络或 npm 配置。
        pause
        exit /b 1
    )
)

echo 正在启动后端（新窗口）与前端（新窗口）...
echo 关闭对应窗口即可停止对应服务。
echo.

rem 分别起两个窗口：后端跑 app.py，前端跑 npm run dev
start "nacho 后端" cmd /k "cd /d ""%~dp0"" && python app.py"
start "nacho 前端" cmd /k "cd /d ""%~dp0frontend"" && npm run dev"

echo 前后端已在两个新窗口启动。
exit /b 0
