# 打包与部署

三种部署方式，按省心程度排：

| 方式 | 适合 | 需要装什么 |
|---|---|---|
| **Docker 单镜像**（推荐） | 有 Docker 的机器，一条命令起 | Docker |
| **docker compose** | 同上，还想把配置 / 卷写进文件管理 | Docker + compose |
| **不用 Docker** | 已有反向代理 / 静态服务器，或想直接跑源码 | Python 3.12+、Node 20+（构建前端时） |

镜像里**一个容器同时有前端与后端**：nginx 在 80 端口托管控制台并把 `/api`、`/docs`
反代给同容器内的后端（`127.0.0.1:18080`），所以前端不需要跨域配置，也不用开 CORS。

---

## 1. 一键打包

```bat
scripts\build-all.bat              :: Windows：前端 + 后端产物 + 单镜像 tickneko:latest
scripts\build-all.bat v0.1.0       :: 换个镜像标签
scripts\build-all.bat --no-docker  :: 只打包前后端产物，不碰 Docker
```

```bash
./scripts/build-all.sh         # Linux / macOS，参数同上
```

产物：

| 产物 | 位置 | 用途 |
|---|---|---|
| 控制台静态文件 | `frontend/dist` | 交给任意静态服务器 / CDN；刷新子路由要回落到 `index.html`（单页应用） |
| 后端源码包 | `build/backend` | 不用 Docker 时：`cd build/backend && pip install -r requirements.txt && python app.py` |
| 单镜像 | `tickneko:<标签>` | 下面两种跑法都用它 |

只想自己敲命令也可以：

```bash
cd frontend && npm ci && npm run build && cd ..
docker build -t tickneko:latest .
```

国内网络慢就换源（两者都可选）：

```bash
docker build \
  --build-arg NPM_REGISTRY=https://registry.npmmirror.com \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t tickneko:latest .
```

## 2. 跑起来

配置**不用提前准备**：它跟数据一起住在 `data/` 下（`data/config.toml`），容器第一次启动会照镜像里
的模板生成一份。想自己写就先复制（这样文件属主是你，Linux 上不必 sudo 才能改）：

```bash
mkdir -p data && cp config.toml.example data/config.toml   # Windows: mkdir data & copy config.toml.example data\config.toml
```

**配置为什么住 `data/`**：它是运行时数据，跟 sqlite、Kook 密钥、头像同处一地 —— 备份 / 搬迁 / 挂载
都只搬 `data/` 一个目录；而且挂的是**目录**，宿主上文件不存在时不会被 Docker 建成同名目录，容器还
写得进去，配置才能由容器自己生成。配置**不进镜像**（`data/` 不在镜像里），有口令与密钥。

### 2.1 docker run

```bash
docker run -d --name tickneko \
  -p 8080:80 \
  -p 16700:16700 \
  -v "$PWD/data:/app/data" \
  -v tickneko-logs:/app/logs \
  --restart unless-stopped \
  tickneko:latest
```

### 2.2 docker compose

```bash
docker compose up -d --build
```

`docker-compose.yml` 里已经把端口、卷、时区写好了，改标签 / 换源都在那个文件里；配置挂的是宿主
`./data`，首次启动自动生成 `data/config.toml`，改完 `docker compose restart` 即生效。

### 2.3 端口分别是什么

| 端口 | 谁在听 | 说明 |
|---|---|---|
| `80` → 宿主 `8080` | nginx | 控制台 + 接口（`/api`、`/docs` 反代到后端） |
| `18080` | 后端（FastAPI） | **容器内**，不对外映射；由 nginx 转 |
| `16700` | OneBot 反向 WS | 要对外映射 —— OneBot 实现端（go-cqhttp / NapCat / LLOneBot…）从这里连进来 |

### 2.4 挂载的卷

| 挂到哪 | 装什么 | 丢了会怎样 |
|---|---|---|
| 宿主 `./data` → `/app/data` | `config.toml`（配置）、`sqlite` 数据库（`[database].path` 默认 `data/tickneko.db`）、`secret_key`（Kook 凭证加密密钥）、头像上传 | **配置与用户数据都没了**；Bot Token 也得重填（密钥丢了解不开旧密文） |
| 卷 `tickneko-logs` → `/app/logs` | 日志分片（`[logging.file].dir` 默认 `logs/`） | 只是日志没了，数据无碍 |

## 3. 容器里要改的配置

打开 `data/config.toml`（就在宿主上，直接编辑），下面两处**必须**确认：

```toml
[onebot]
# 反向 WS 的监听地址：容器里要听 0.0.0.0，否则宿主映射了端口也连不进来
host = "0.0.0.0"
port = 16700

[api]
# 代码默认是 "0.0.0.0"（谁都能连）；容器里 nginx 和它同处一个容器，走回环就够，建议改回：
host = "127.0.0.1"
port = 18080
```

其余按需：

* `[database]`：默认 `sqlite`（文件落在 `/app/data/tickneko.db`，随宿主 `./data` 持久化）；要用 MariaDB 就把 `driver` 改成
  `mariadb` 并填连接项 —— 注意容器里的 `host` 不是 `127.0.0.1`，而是数据库服务的地址
  （compose 里加一个 `mariadb` 服务，host 就写服务名）。
* `[cache]`：默认进程内内存缓存；要用 Redis 同理，`host` 写 Redis 服务名而不是本机。
* `[logging]`：控制台出口在容器里就是 `docker logs tickneko`，文件出口落在 `/app/logs`。
* `[kook]`：Kook 是正向连接，出网即可，不需要额外映射端口；`secret_key` 留空会自动生成到
  `/app/data/secret_key`（就在宿主 `./data` 里 —— 丢了 Bot Token 要重填）。

## 4. 不用 Docker 的部署

1. **前端**：把 `frontend/dist` 交给 Nginx / Caddy / 对象存储，规则只有两条 ——
   静态文件直接发；找不到的路径回落到 `index.html`（单页应用）。并加一条反向代理：

   ```nginx
   location ~ ^/(api|docs|redoc|openapi\.json) {
       proxy_pass http://127.0.0.1:18080;   # 后端
       proxy_set_header Host $host;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto $scheme;
   }
   ```

2. **后端**：`build/backend` 拷到服务器，然后

   ```bash
   pip install -r requirements.txt
   python app.py            # 默认读 data/config.toml（没有就按代码默认值跑）
   ```

   端口、OneBot 监听地址都在 `data/config.toml` 里；要常驻就用 systemd / supervisor / 计划任务。

3. 前端想**直连**另一个域名的后端（不走同源反代）：构建时传 `VITE_API_BASE_URL=https://api.example.com`
   （`npm run build` 前设这个环境变量，或 Docker 的 `--build-arg`），并在后端放行 CORS。

## 5. 上线前必须做的事

* **改掉演示账号 `admin` 的默认密码**（`tickneko-admin`，由开发用代码写入）—— 公网部署前务必改密；
  要别的账号不用改代码：在登录页注册即可（新账号是普通用户）；
* **`data/config.toml` 别提交、别打进镜像**（里面有数据库口令与 `secret_key`）—— 整个 `data/` 已在
  `.gitignore` 里；
* 对外只暴露 `80` 与 OneBot 的 `16700`；OneBot 端口尽量限制来源 IP；
* 走 HTTPS 就在前面再放一层反向代理（或在 nginx 里加证书），Cookie 是登录凭据，别裸奔；
* 备份宿主 `data/`（配置、sqlite 数据库、Kook 密钥、头像都在里面）。

## 6. 升级与回滚

```bash
git pull
docker compose up -d --build          # 重新构建并滚动替换
```

`./data` 与日志卷都不动，数据都还在。回滚就是把代码切回旧提交再 `up -d --build`（镜像标签也可以
带版本号，用 `scripts\build-all.bat v0.1.0` 打出 `tickneko:v0.1.0` 之类长期留着）。

## 7. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 控制台能开，接口全 502 | 后端没起来。`docker logs tickneko` 看是不是连不上数据库（`[database]` 填错） |
| OneBot 实现端连不上 `16700` | 容器里 `[onebot] host` 还是 `127.0.0.1` —— 改成 `0.0.0.0` 并确认宿主端口已映射 |
| 刷新工作流页面 404 | 前端的静态服务器没配「回落到 `index.html`」；用镜像里的 nginx 没这个问题 |
| 数据重启后没了 | 没挂 `/app/data`（配置与 sqlite 都在里面），或用了 `docker run --rm` |
| 宿主上改不了 `data/config.toml`（Linux） | 容器以 root 跑，自动生成的那份属主是 root：`sudo chown -R $USER data`，或先自己 `cp config.toml.example data/config.toml` 再启动 |
| 登录后一会儿就掉 | 前面还套了一层代理时，确认 `X-Forwarded-Proto` 传对了（HTTPS 下 Cookie 要 `Secure`） |
| 构建时拉依赖很慢 | 用 `--build-arg` 换 NPM / PyPI 镜像源（见第 1 节） |
| 构建时报 `failed to resolve source metadata ... EOF` | Docker Hub 连不上，配镜像加速器（见第 8 节） |

部署相关的安全细节见 [`.github/SECURITY.md`](../.github/SECURITY.md)。

## 8. 拉不到基础镜像？

`docker build` 要拉两个基础镜像：`node:20-alpine`（构建前端）与 `python:3.12-slim`（运行后端）。
国内直连 Docker Hub 经常失败，报的就是：

```
ERROR: failed to build: failed to solve: python:3.12-slim: failed to resolve source metadata
for docker.io/library/python:3.12-slim: failed to do request: ... EOF
```

给 Docker 配个镜像加速器（Docker Desktop → Settings → Docker Engine，写进 JSON 后 Apply & Restart）：

```json
{
  "registry-mirrors": ["https://<你用的加速器地址>"]
}
```

企业环境也可以先把两个基础镜像 `docker pull` 到本机（或推到自己私有 registry），再把 Dockerfile 顶部的
`FROM node:20-alpine` / `FROM python:3.12-slim` 换成对应地址 —— 其余不用动。
