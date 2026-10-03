# 参与贡献

感谢愿意给 TickNeko 添砖加瓦。这份指南只讲「怎么动手」，架构与设计取舍看 [`docs/README.md`](../docs/README.md) 与各模块文档。

一句话原则：**改动要小而完整** —— 代码、测试、文档一起到位，别人 review 时不用猜。

---

## 1. 环境准备

| 需要的 | 版本 | 说明 |
|---|---|---|
| Python | 3.12+ | 后端 |
| Node.js | 20+ | 控制台（`frontend/`） |
| MariaDB | 可选 | 不装就用 sqlite（`data/config.toml` 里 `driver = "sqlite"`） |
| Redis | 可选 | 不装就用进程内缓存（默认） |

```bash
git clone https://github.com/haloneko/TickNeko.git
cd TickNeko

python -m venv .venv && .venv\Scripts\activate     # Windows（Linux: source .venv/bin/activate）
pip install -r requirements.txt -r requirements-dev.txt
copy config.toml.example data\config.toml        # Linux: cp config.toml.example data/config.toml

cd frontend && npm install && cd ..
```

跑起来（Windows 也可以直接双击 `scripts\start-all.bat`）：

```bash
python app.py                                  # 后端 http://127.0.0.1:18080
cd frontend && npm run dev                     # 控制台 http://127.0.0.1:15173
```

本地演示账号见根 `README.md` —— **它们是给本地试用的，别把默认密码带到公网**。

## 2. 目录速览

```
tickneko/
  core/        地基：logger / cache / scheduler（只依赖自身，不 import 上层）
  platforms/   onebot（反向 WS）、kook（正向 WS）、bridge（适配器 + Gateway 总线）
  workflow/    画布图：模型 / 校验 / 落库 / 执行引擎 / 运行时（不 import FastAPI）
  api/         接口层：api/（路由）→ services/（业务）
  db/ bots/    数据层与机器人凭证
  bootstrap.py 组合根（装配顺序都在这儿）、wiring.py（日志派发）
frontend/      控制台（React + TypeScript + Vite）
docs/          文档（一份模块一份，索引见 docs/README.md）
tests/         pytest（外部依赖一律打桩）
examples/      可运行的最小示例
scripts/       启动脚本（start-all / start-backend / start-frontend，Windows 双击即用）
.github/       社区文档（CONTRIBUTING / SECURITY / CODE_OF_CONDUCT）与 CI workflow
```

## 3. 代码约定

* **分层与依赖方向**：下层不 import 上层 —— `core` 只依赖自身；`platforms/bridge` 只有适配器才 import 平台包；`workflow` 不 import FastAPI；`api` 不 import `platforms`（靠协议接收）。加功能前先想「它属于哪一层」，别把平台细节漏进业务层。
* **类型注解写全**，文件头 `from __future__ import annotations`；公开函数写清参数与返回。
* **文档就近**：模块 docstring 只留一句话定位 + 指回文档；设计取舍（为什么这么做）写在 `docs/<模块>/`。新增 / 重命名 / 删除文件时同步更新对应的索引文档。
* **日志**：用各模块的接入点（`workflow_logger()` / `cache_logger()` …），**不要**直接 `default_core()`；日志实例**用到才取**，不要在模块级取（会把进程默认核心按默认参数定死）。
* **异常口径**：业务失败（算不出、取不到、对方回错）抛 `NodeFailure`，只停当前分支；环境问题（没接线、缺依赖）抛普通异常并留堆栈；连不上 / 超时这类**可预期的环境问题**抛 `EnvironmentFailure`（日志只记一行）。细节见 [`docs/workflow/workflow.md`](../docs/workflow/workflow.md) 第 5.5 节。
* **配置**：只有根目录 `config.py` / `app.py` 读 `data/config.toml`（模板 `config.toml.example`），包内一律由上层把选项传进来（`from_mapping` / 构造参数）—— 包内不 import `config`。
* 文案、注释、文档用中文，术语与现有文档保持一致（「归属」「运行开关」「发布 ≠ 运行」这类词已经有确定含义）。

## 4. 测试

```bash
pytest  # 全量：串行约 25 秒
pytest -n auto  # 并行：约 9 秒，快 2~3 倍
pytest -n 0  # 强制串行：排错时输出不交错
pytest tests/test_workflow.py -q  # 单个文件
pytest tests/test_workflow.py -k condition -q  # 挑用例
```

* **为什么能并行**：用例之间是隔离的 —— 临时目录各用各的（`tmp_path`）、日志核心与进程级单例在各自 worker 进程里独立、Redis 用例带自己的 namespace、需要端口的用 `free_port()` 动态取。
* 新增 / 修改行为**必须带用例**：正常路径 + 边界（空值、失败分支）。
* 外部依赖一律**打桩**：不依赖真 Redis、MariaDB、网络（`httpx.AsyncClient` 有现成的替身写法，见 `tests/test_workflow.py`；需要真端口的用例用回环地址 + `free_port()`）。本机若真起了 Redis，`tests/test_cache.py` 里那条真连用例会自动跑起来（没服务则跳过）。
* 提交前本地跑一遍全量；CI 会跑同样的命令（见 `.github/workflows/ci.yml`）。

## 5. 提交与分支

分支从 `master` 拉，名字说明意图：`feat/xxx`、`fix/xxx`、`docs/xxx`、`refactor/xxx`。

提交信息用 Conventional Commits，描述写中文（与现有历史一致）：

```
feat(kook): 心跳间隔按官方口径加 ±5 秒抖动
fix(workflow): 被跳过的分流节点级联跳过下游
docs(cache): 新增 docs/cache/cache.md，模块 docstring 精简指路
```

* **一笔提交一件事**：夹带无关改动会让 review 和回滚都变难；
* 破坏性改动在标题里点出来（如 `（breaking）`），并说明迁移方式；
* 改行为记得同步文档：`docs/<模块>/` 里对应的那份，以及模块 docstring 的指路。

## 6. 提 PR

PR 描述写清三件事：

1. **改了什么** —— 用户能感知的行为变化；
2. **为什么** —— 触发这次改动的场景 / 问题（有日志或复现步骤更好）；
3. **怎么验证** —— 跑了哪些测试、手动怎么试的。

CI 绿了再请人看。review 里被要求改的话，直接在原分支追加提交即可（不用重开 PR）。

## 7. 加平台、加节点

* **加平台**：写一个适配器（实现 `BotAdapter`），在 `bootstrap.py` 装配一行 —— 接口层与工作流一行都不用改。步骤见 [`docs/bridge/bridge.md`](../docs/bridge/bridge.md)。
* **加节点**：新建一个文件、注册一下，框架不用动 —— 完整指南（契约、注册即校验、可选依赖、测试写法）见 [`docs/workflow/workflow.md`](../docs/workflow/workflow.md) 第 5 节。

## 8. 许可

本项目以 [Apache License 2.0](../LICENSE) 发布。**你提交的代码默认按同一许可证授权**（inbound = outbound），不需要签 CLA；请确保你有权提交这部分代码（别把来路不明的代码贴进来）。Apache-2.0 是宽松许可：别人可以商用、也能闭源分发，但分发时必须带上 [`LICENSE`](../LICENSE) 与 [`NOTICE`](../NOTICE)（保留版权与署名），且不得用 TickNeko 的名字为派生作品背书。

第三方依赖的许可清单见 [`THIRD_PARTY_NOTICES`](../docs/THIRD_PARTY_NOTICES)。
