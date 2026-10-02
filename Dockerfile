# BotNode 单镜像：一份镜像里同时有控制台（前端产物）与后端。
#
#   控制台 + 接口  :80    （nginx：托管前端静态文件，把 /api、/docs 反代给本机后端）
#   后端本体       :18080 （容器内，不直接对外，由 nginx 转）
#   OneBot 反向 WS :16700 （要对外暴露，OneBot 实现端连进来的口）
#
# 构建：
#   docker build -t botnode:latest .
#   国内网络可换源：--build-arg NPM_REGISTRY=https://registry.npmmirror.com \
#                   --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
#
# 运行（config.toml 不进镜像，要挂进去）：
#   docker run -d --name botnode -p 8080:80 -p 16700:16700 \
#     -v "$PWD/config.toml:/app/config.toml:ro" \
#     -v botnode-logs:/app/logs -v botnode-data:/app/data botnode:latest
#
# 一键打包（前端 + 后端产物 + 这个镜像）：build-all.bat / build-all.sh

# ------------------------------------------------------------------ ① 前端构建
FROM node:20-alpine AS web
WORKDIR /web

# 先只复制清单文件：依赖没变时这层能复用缓存
COPY frontend/package.json frontend/package-lock.json ./
ARG NPM_REGISTRY=https://registry.npmjs.org
RUN npm config set registry "$NPM_REGISTRY" && npm ci

COPY frontend/ ./
# 留空 = 同源（浏览器打 80 端口，nginx 反代 /api 到后端）—— 与 vite.config.ts 的 dev 代理同口径
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

# ------------------------------------------------------------------ ② 运行镜像
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai

# nginx 管静态与反代，curl 给健康检查用，tzdata 让日志时间戳是本地时区
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx curl tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖单独一层：代码改动不会让 pip 重装
COPY requirements.txt ./
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.txt

COPY app.py config.py config.toml.example ./
COPY botnode/ ./botnode/

# nginx：去掉发行版自带的默认站点，换成我们这份（静态 + 反代）
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
RUN rm -f /etc/nginx/sites-enabled/default

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && mkdir -p /app/logs /app/data

# 前端产物：控制台就在 /usr/share/nginx/html
COPY --from=web /web/dist /usr/share/nginx/html

# logs/（日志 + sqlite）、data/（头像等）要落在卷上，容器换了数据还在
VOLUME ["/app/logs", "/app/data"]
EXPOSE 80 16700

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:18080/openapi.json >/dev/null || exit 1

CMD ["/entrypoint.sh"]
