# Nacho 接入层改造 Roadmap

目标：抽象统一适配器层，令牌列表融合在线状态，让后续接入 Kook 等新平台时**图、节点、令牌列表全都不用动**。

## 总览

- [x] **P1** 令牌融合在线状态（接口聚合 + 前端状态列）
  - 说明：`online` 是**派生态**（内存实时态），不落库；接口层 `list_tokens` 时聚合各适配器的在线连接。
- [x] **P2** 适配器层抽象：新增 `nacho/bridge/`（`PlatformEvent` + `BotAdapter` 协议 + `Gateway` 总线），把 `OneBotServer` 包成第一个适配器
- [x] **P3** workflow 泛化：消息触发真正接通（start `trigger=message`）+ `onebot` 节点泛化为 `send` 节点（platform 参数）
- [ ] **P4** Kook 适配器：作为第二个适配器验证抽象是否通用

## P4 拆分

设计基调（与 P2 / P3 同路数：每步独立可验证、全量测试始终绿；关键差异先讲清）：

- **连接方向相反**：OneBot 是**反向 WS**（框架当服务端，等实现连进来，令牌在握手时鉴权）；
  Kook 是**正向 WebSocket**（框架当客户端，主动连 Kook 网关，用 **Bot Token** 鉴权）。所以
  `nacho/kook/` 的核心是「正向 WS 客户端 + 断线重连 + 心跳 + 事件分发」，跟 `OneBotServer`
  的服务端模型不是一个东西 —— 这恰恰是验证 `BotAdapter` 协议是否「真通用」的关键点。
- **令牌形态不同**：OneBot 的令牌是「反向 WS 握手鉴权」，落在 `onebot_tokens` 表、走
  `TokenRegistry` 的 `resolve`/`issue`；Kook 的凭证是「Bot Token」（Kook 开放平台签发，用户
  填进配置 / 库里），**不能**套 `TokenRegistry` 那套 `resolve`/`issue` 语义。Kook 的凭证形态
  在本阶段定义为「配置里带（`[kook] token=...`）」，不落库、不进接口层令牌列表 —— 接口层
  `OneBotLike` 管理面是 onebot 专属，Kook 不硬套。
- **依赖方向不破**：`nacho/bridge/kook.py` 是 bridge 里**第二个**允许 import 平台包的地方；
  `nacho/kook/` 只依赖 `nacho.core`（logger）与 `websockets`（正向客户端也要它）；`nacho/kook/`
  不 import `nacho.api` / `nacho.workflow`。
- **规范化口径复用**：Kook 事件的 `PlatformEvent` 翻译、`BotClient`、`ActionResult` 都走
  P2 定的那套形状（身份一律字符串；翻译不了的挂 `raw`）；发送走 `gateway.send("kook",
  owner_id, action, ...)`。
- **Kook 新增一组动作（不复用 OneBot 动作）**：OneBot 那套（`send_msg` / `send_group_msg` /
  `send_private_msg` / `delete_msg`，号转整数）是 OneBot 专属契约，Kook 的 channel_id 是
  **字符串**、且发消息走「频道消息 / 私聊消息」两类。Kook 独立一组动作（候选）：
  `send_channel_msg`（channel_id 字符串 + message）、`send_dm_msg`（user_id 字符串 +
  message）、`delete_msg`（message_id 字符串）。动作参数**不转整数**（Kook 的 id 是字符串）。
  `send` 节点按 `platform` 决定「用哪组动作 / 怎么转参数」——这是节点层的平台分派，不是
  改 bridge。
- **可选依赖**：`nacho[kook]` 复用 `websockets`（正向客户端也要），`pyproject.toml` 加
  `kook = ["websockets>=13"]`；`nacho/kook/__init__.py` 不强制任何额外依赖。

- [x] **P4-1** `nacho/kook/` 平台包：`options.py`（`KookOptions`：网关地址 / Bot Token /
  心跳间隔 / 动作超时）、`models.py`（Kook 事件模型：消息 / 系统事件 / 心跳等，`extra=allow`）、
  `client.py`（`KookClient`：正向 WS 连接 + `connect`/`close`/`send`/`call` 动作发送 + 事件
  分发 handler 钩子 + 断线重连 + 心跳；**不 import 业务**）。纯「连接 + 协议」层，配单测
  （用 `websockets` 本地测试服务端模拟 Kook 网关）
- [ ] **P4-2** `bridge/kook.py`：`KookAdapter` 包 `KookClient`，实现 `BotAdapter` 协议
  （`platform="kook"` / `start` / `stop` / `clients` / `send`）；handler 里把 Kook 事件翻译成
  `PlatformEvent`（Kook 的 channel/author/内容 -> chat/chat_id/user_id/text，身份转字符串）
  后调 `publish`；`send` 把 `owner_id`（Kook 里是机器人自身，暂按 owner=空串或 token 对应的
  bot id）路由到 `KookClient.call`；配单测（FakeClient 走翻译 + 透传）
- [ ] **P4-3** `send` 节点支持 Kook 动作组：`nodes/send.py` 按 `platform` 分派动作组与参数
  语义 —— onebot 走 `SEND_ACTION_ORDER`（号转整数），kook 走 `KOOK_ACTION_ORDER`
  （`send_channel_msg` / `send_dm_msg` / `delete_msg`，channel_id / user_id / message_id 都是
  **字符串不转整数**）；校验器同样按 platform 判动作枚举；配单测（platform=kook 时动作组 /
  参数语义切换，platform=onebot 行为不变）
- [ ] **P4-4** 装配 `bootstrap.py`：读 `[kook]` 配置 -> 建 `KookClient` -> 包成
  `KookAdapter` -> `gateway.register`；`serve_forever` 的退出条件把 Kook 客户端算上
  （P2 已留了「只等一个会没人守另一个」的口子）；配单测（装配切换）
- [ ] **P4-5** 收尾：`bridge/__init__.py` 文档更新（「适配器怎么写第二个」补上「正向连接
  平台」的差异）；`pyproject.toml` 加 `kook` 可选依赖；全量回归对齐基线（全绿才算完）；
  勾掉 P4-1~P4-5 与总览 P4
