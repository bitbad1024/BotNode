@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  BotNode 前端启动脚本
rem  启动 Vite 开发服务器（默认 http://127.0.0.1:15173）
rem  依赖：node 18+ / npm；首次运行会自动安装依赖
rem ============================================================

cd /d "%~dp0"

where node >nul 2>nul
if errorlevel 1 (
    echo [错误] 没找到 node，请先安装 Node.js 18+ 并加入 PATH。
    pause
    exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
    echo [错误] 没找到 npm，请先安装 Node.js（自带 npm）并加入 PATH。
    pause
    exit /b 1
)

cd frontend

rem 首次运行（还没有 node_modules）时自动装依赖
if not exist "node_modules" (
    echo 首次运行，正在安装前端依赖（npm install）...
    call npm install
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络或 npm 配置。
        pause
        exit /b 1
    )
)

echo 正在启动 BotNode 前端（http://127.0.0.1:15173）...
echo 按 Ctrl+C 停止。
echo.

call npm run dev

echo.
echo 前端已退出。
pause
