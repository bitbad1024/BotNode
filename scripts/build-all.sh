#!/usr/bin/env bash
# ============================================================================
#  BotNode 一键打包（Linux / macOS，与 build-all.bat 同款）
#
#    ./scripts/build-all.sh               前端 + 后端产物 + 单镜像 botnode:latest
#    ./scripts/build-all.sh v0.1.0        镜像标签换成 botnode:v0.1.0
#    ./scripts/build-all.sh --no-docker   只打包前后端产物，不碰 Docker
#    ./scripts/build-all.sh --clean       先清空 node_modules 再装（可复现，但更慢）
#
#  产物：
#    frontend/dist      控制台静态产物
#    build/backend      后端源码包（cd 进去 pip install -r requirements.txt，再 python app.py）
#    botnode:<标签>     单镜像（nginx 托管前端 + 反代 /api 给后端）
# ============================================================================
set -euo pipefail

# 脚本住在 scripts/ 下、项目根是它的上一级：先切过去，从哪儿调用都跑得对
cd "$(dirname "$0")/.."

TAG=latest
SKIP_DOCKER=0
CLEAN=0
for arg in "$@"; do
    case "$arg" in
        --no-docker) SKIP_DOCKER=1 ;;
        --clean) CLEAN=1 ;;
        *) TAG="$arg" ;;
    esac
done

echo "============================================================"
echo " BotNode 一键打包   （镜像标签：$TAG）"
echo "============================================================"

if ! command -v npm >/dev/null 2>&1; then
    echo "找不到 npm：请先装 Node.js 20+ 并加入 PATH"
    exit 1
fi

echo
echo "[1/3] 构建前端 ..."
if [ "$CLEAN" = "1" ]; then
    # 干净重建：npm ci 严格按 package-lock.json 装
    (cd frontend && rm -rf node_modules && npm ci && npm run build)
else
    # 默认增量安装：不动已有 node_modules，快且不容易踩文件占用
    (cd frontend && npm install && npm run build)
fi
echo "  前端产物：frontend/dist"

echo
echo "[2/3] 收集后端源码 ..."
rm -rf build
mkdir -p build/backend/botnode
# tar 过一遍是为了不带 __pycache__（rsync 不一定装）
(cd botnode && tar cf - --exclude='__pycache__' .) | (cd build/backend/botnode && tar xf -)
cp app.py config.py requirements.txt config.toml.example build/backend/
echo "  后端产物：build/backend"

if [ "$SKIP_DOCKER" = "1" ]; then
    echo
    echo "[3/3] 跳过镜像构建（--no-docker）"
elif command -v docker >/dev/null 2>&1; then
    echo
    echo "[3/3] 构建单镜像 botnode:$TAG ..."
    docker build -t "botnode:$TAG" .
else
    echo
    echo "[3/3] 找不到 docker，跳过镜像构建"
fi

echo
echo "打包完成："
echo "  前端产物  frontend/dist"
echo "  后端产物  build/backend"
if [ "$SKIP_DOCKER" != "1" ]; then
    echo "  镜像      botnode:$TAG"
    echo
    echo "镜像怎么跑："
    echo "  docker run -d --name botnode -p 8080:80 -p 16700:16700 \\"
    echo "    -v \"\$PWD/data:/app/data\" \\"
    echo "    -v botnode-logs:/app/logs botnode:$TAG"
    echo "  配置不用先准备：第一次启动会照模板生成 data/config.toml，改完重启容器即生效"
    echo "  或者：docker compose up -d --build"
fi
