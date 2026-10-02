#!/bin/sh
# 单镜像的启动脚本：先起 nginx（80，静态 + 反代），再起后端（前台主进程）。
#
# 为什么要中转信号：`docker stop` 发的是 SIGTERM，而 app.py 的优雅停机（冲刷日志余量、
# 关调度器、关库连接）走的是 Ctrl+C 那条路（SIGINT）。这里把 SIGTERM 翻译成 SIGINT，
# 让容器停下来时日志不丢尾巴。
set -e

# 配置跟数据一起住在 /app/data（宿主上是 ./data，见 docker-compose.yml）：第一次启动时那份还
# 不存在，就照镜像里的模板抄一份过去 —— 「跑起来就有配置文件可改」，不必先手工 cp 再启动。
# 目录挂成只读时抄不进去：降级成按代码默认值跑，不因此起不来。
CONFIG=/app/data/config.toml
if [ ! -f "$CONFIG" ]; then
    cp /app/config.toml.example "$CONFIG" 2>/dev/null \
        && echo "[entrypoint] 已按模板生成 $CONFIG（宿主上是 ./data/config.toml）：改完重启容器即生效" \
        || echo "[entrypoint] 没有 $CONFIG 也生成不了（data 目录只读？）：按代码默认值启动"
fi

nginx -g 'daemon off;' &
NGINX_PID=$!

python app.py &
APP_PID=$!

# 容器收到 TERM/INT -> 转成 SIGINT 交给后端（它自己会收尾），nginx 交给容器退出时清理
term() {
    kill -INT "$APP_PID" 2>/dev/null || true
}
trap term TERM INT

# 后端退出即容器退出；trap 打断第一次 wait，所以要再等一次让它收尾
wait "$APP_PID" || true
wait "$APP_PID" || true

kill "$NGINX_PID" 2>/dev/null || true
