@echo off
rem ============================================================================
rem  BotNode 一键打包（Windows）
rem
rem    build-all.bat                  前端 + 后端产物 + 单镜像 botnode:latest
rem    build-all.bat v0.1.0           镜像标签换成 botnode:v0.1.0
rem    build-all.bat --no-docker      只打包前后端产物，不碰 Docker
rem    build-all.bat --clean          先清空 node_modules 再装（可复现，但更慢）
rem
rem  产物：
rem    frontend\dist      控制台静态产物（交给任意静态服务器即可）
rem    build\backend      后端源码包（cd 进去 pip install -r requirements.txt，再 python app.py）
rem    botnode:<标签>     单镜像（nginx 托管前端 + 反代 /api 给后端）
rem ============================================================================
setlocal enabledelayedexpansion
chcp 65001 >nul

set "TAG=latest"
set "SKIP_DOCKER=0"
set "CLEAN=0"
for %%A in (%*) do (
    set "ARG=%%~A"
    if /I "!ARG!"=="--no-docker" (
        set "SKIP_DOCKER=1"
    ) else if /I "!ARG!"=="--clean" (
        set "CLEAN=1"
    ) else (
        set "TAG=!ARG!"
    )
)

echo ============================================================
echo  BotNode 一键打包   （镜像标签：%TAG%）
echo ============================================================

echo.
echo [1/3] 构建前端 ...
where npm >nul 2>nul
if errorlevel 1 (
    echo   找不到 npm：请先装 Node.js 20+ 并加入 PATH
    goto :fail
)
pushd frontend
rem 默认走 npm install（增量）：npm ci 会先清空 node_modules，在 Windows 上
rem 撞上「文件被占用」或 npm 的批量删除保护时会直接失败；要干净重建就传 --clean
if "%CLEAN%"=="1" (
    if exist node_modules rmdir /s /q node_modules
    call npm ci
) else (
    call npm install
)
if errorlevel 1 (popd & goto :fail)
call npm run build
if errorlevel 1 (popd & goto :fail)
popd
echo   前端产物：frontend\dist

echo.
echo [2/3] 收集后端源码 ...
if exist build rmdir /s /q build
mkdir build\backend
rem robocopy：只拷源码，不带 __pycache__；退出码 0-7 都算成功
robocopy botnode build\backend\botnode /E /XD __pycache__ /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto :fail
copy /Y app.py build\backend\ >nul
copy /Y config.py build\backend\ >nul
copy /Y requirements.txt build\backend\ >nul
copy /Y config.toml.example build\backend\ >nul
echo   后端产物：build\backend

if "%SKIP_DOCKER%"=="1" (
    echo.
    echo [3/3] 跳过镜像构建（--no-docker）
    goto :done
)

echo.
echo [3/3] 构建单镜像 botnode:%TAG% ...
where docker >nul 2>nul
if errorlevel 1 (
    echo   找不到 docker，跳过镜像构建
    goto :done
)
docker build -t botnode:%TAG% .
if errorlevel 1 goto :fail

:done
echo.
echo 打包完成：
echo   前端产物  frontend\dist
echo   后端产物  build\backend
if not "%SKIP_DOCKER%"=="1" echo   镜像      botnode:%TAG%
echo.
echo 镜像怎么跑（把下面四行拼成一条命令，或直接用 docker compose）：
echo   docker run -d --name botnode -p 8080:80 -p 16700:16700
echo     -v "%CD%\config.toml:/app/config.toml:ro"
echo     -v botnode-logs:/app/logs -v botnode-data:/app/data
echo     botnode:%TAG%
echo 或者：docker compose up -d --build
exit /b 0

:fail
echo.
echo 打包失败，请看上面的报错。
exit /b 1
