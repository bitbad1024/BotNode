#!/bin/sh
# 单镜像的启动脚本：先起 nginx（80，静态 + 反代），再起后端（前台主进程）。
#
# 为什么要中转信号：`docker stop` 发的是 SIGTERM，而 app.py 的优雅停机（冲刷日志余量、
# 关调度器、关库连接）走的是 Ctrl+C 那条路（SIGINT）。这里把 SIGTERM 翻译成 SIGINT，
# 让容器停下来时日志不丢尾巴。
set -e

if [ ! -f /app/config.toml ]; then
    echo "[entrypoint] 没找到 /app/config.toml：按默认值启动（建议挂一份，见 docs/deploy.md）"
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
