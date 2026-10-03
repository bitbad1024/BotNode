# 桥接层（`tickneko.platforms.bridge`）

## 解决什么

每多接一个平台（OneBot / Kook / …），不该让图、节点、令牌列表跟着改。做法是老一套
「**结构化协议、互不 import**」（同 `tickneko.api.api.onebot.protocols`）：本包只声明形状，
由各平台适配器结构化满足。

依赖方向：本包只依赖 `tickneko.core`（logger），**不** import `tickneko.platforms.onebot` /
`tickneko.api` / `tickneko.workflow` —— 平台的类型只在适配器实现里出现，协议这边一律
`object` / 结构化形状（PEP 563 惰性注解兜底）。

## 模块职责

| 模块 | 职责 |
| --- | --- |
| `models` | 规范化数据形状：`PlatformEvent` / `BotClient` / `ActionResult` / `ChatTarget` |
| `protocols.BotAdapter` | 一个平台适配器长什么样（结构化协议） |
| `gateway.Gateway` | 总线：注册适配器、订阅事件、按平台路由发送 |
| `onebot.OneBotAdapter` | 第一个适配器，包住 `tickneko.platforms.onebot.OneBotServer`（**可选依赖**） |
| `kook.KookAdapter` | 第二个适配器，包住 `tickneko.platforms.kook.KookClient`（正向 WS，多客户端） |
| `manager.BotManager` | 管理面：凭证落库 + 适配器生命周期，统一成接口层认的 `BotsService` |
| `logging` | 本层的日志接入点 |

平台适配器是**可选**的：只有 `onebot.py` import `tickneko.platforms.onebot`（且只有
`pip install "tickneko[onebot]"` 才装得上），`kook.py` 同理；包 `__init__` 刻意不
re-export 它们 —— import 本包不应连带要求 websockets。

## 规范化模型

各平台的事件模型差异很大（OneBot 的 `user_id` 是整数、Kook 的是字符串），下游
（workflow 触发、日志钩子）不该逐平台认字段，所以适配器把平台事件**翻译**成
`PlatformEvent`，下游只认这一份。口径：

- 身份一律**字符串**：`owner_id`（谁的）、`self_id`（机器人自身账号）、`user_id` /
  `chat_id`（对方 / 会话）。数字平台的适配器负责转成字符串；
- 「归属」跨平台不再唯一：同一套 `owner_id` 在不同平台各有一条连接，所以事件带着
  `platform`，Gateway 内部按 `(platform, owner_id)` 复合键路由；管理面的凭证统一落在
  `tickneko.bots` 的 `bot_credentials` 凭证行（`platform` + `owner_id` + `bot_id` 复合键，
  一个用户可多个机器人）；
- 翻译不了的字段不去硬翻：平台原始事件整条挂在 `raw` 上（OneBot 来的就是
  `OneBotEvent`），下游要用细节就下探 `raw`，但下探就意味着绑平台 —— 能用规范化字段
  就别用 `raw`；
- 三个模型都是**冻结**的：事件是已发生的事，不改写。

`PlatformEvent` 的字段分三组：身份（`platform` / `owner_id` / `self_id`）、指向
（`kind` / `chat` / `user_id`）、内容（`text` / `message_id` / `raw`）。`text` 只在消息
事件里有意义，且是**纯文本正文**。

## 适配器协议（`BotAdapter`）

为什么是 Protocol 而不是基类：桥接层只声明「Gateway 需要哪些能力」，由各平台适配器
结构化满足。Gateway 依赖的就这四样：

- `platform` —— 平台标识（事件 / 发送的路由键），同一 Gateway 里不得重复；
- `start` / `stop` —— 生命周期（幂等）；
- `clients()` —— 在线列表快照（「已连接设备」那张表）；
- `send()` —— 给某个归属的在线连接发一个动作并等回执。

适配器自己的事件怎么进 Gateway 不在协议里：那是**构造约定** —— 构造时 Gateway 把投递口
`publish` 交给适配器，适配器翻译完调它。

## Gateway 总线

它自己**不懂任何平台**：事件从适配器来（翻译后调 `publish`），发送按 `platform` 路由给
对应适配器。

- **订阅口径**：handler 抛出的异常**只记日志**，不淹总线、不影响其它订阅者 —— 事件分发是
  「尽力而为」，一条业务出错不该连累整条接入层；
- **发送口径**：**环境问题当场抛** —— 没注册这个平台是装配问题，`ConnectionError` 带上
  当前已注册的平台列表；「发出去、对方答了不成功」才走 `ActionResult` 的 `ok=False`。

装配形态（两个平台并存，凭证统一是 `bot_credentials` 凭证行）：

```python
gateway = Gateway()
gateway.register(OneBotAdapter(options, publish=gateway.publish, tokens=store))
kook = KookAdapter(kook_options, publish=gateway.publish)
for cred in await store.list_platform("kook", enabled_only=True):   # 读 Kook 凭证行
    kook.add_bot(cred.bot_id, await store.decrypt_token(cred.bot_id, secret_key), owner_id=cred.owner_id)
gateway.register(kook)
manager = BotManager(store, onebot=onebot, kook=kook, secret_key=secret_key)  # /api/bots/* 的 BotsService
gateway.subscribe(on_platform_event)   # 业务只认规范化事件，不认平台
await gateway.start()                  # -> 各 adapter.start()
```

发送走总线（按平台路由，环境问题当场抛）：

```python
await gateway.send("onebot", owner_id, "send_private_msg", user_id=..., message=...)
await gateway.send("kook", owner_id, "send_channel_msg", target_id=..., content=...)
```

## 两个自带适配器

**OneBot**（反向 WS，框架当服务端）：包一层、不改一层 —— `tickneko/platforms/onebot/` 一行
不动，适配器做三件事：事件翻译（`OneBotEvent` -> `PlatformEvent`，心跳不用滤，服务端
`_emit` 调 handler 前已经滤掉）、能力转述（`clients()` / `send()`）、兼容面（roster /
kick / revoke_by_id / set_token_enabled / tokens / connections 原样透传给被包的服务端，
接口层的 `OneBotLike` 协议由它**结构化满足**，装配时注到原来的注入点上即可，下游零改动）。

**Kook**（正向 WS，框架当客户端）：与 OneBot 方向相反，用来验证协议对「方向相反」的平台
同样通用。差异有三：

- **归属语义**：OneBot 一个端口接很多客户端、靠令牌分归属；Kook 一个 Bot Token 就是一个
  机器人。一个适配器管**多个**机器人（`dict[bot_id, KookClient]`），这样 Gateway 里 Kook
  仍只占一个 platform 槽位，运行时增删机器人也不会撞上「同平台重复注册」；
- **事件翻译**：`channel_type`（GROUP / PERSON）归一成 `chat`，`target_id` -> `chat_id`、
  `author_id` -> `user_id`、`content` -> `text`；
- **能力转述**：`clients()` 一行一个在线机器人，`send` 走 REST（`KookClient.call`）。

回程地址（回复同一会话）由各自的 `OneBotTarget` / `KookTarget` 承担：号的类型各按平台
协议（OneBot 整数、Kook 字符串），生产与消费同平台，reply 时原样传回。

## 怎么写第二个适配器

照 `onebot.py` 的样子（Kook 已经照此写了一个），四步：

1. **平台接入**（如果还没有独立包）：像 `tickneko/platforms/onebot/` / `kook/` 那样只管
   「连接 + 协议」—— 鉴权、连接管理、事件分发（handler 钩子）、动作发送；**业务不进去**。
   **方向先想清**：底层「监听」还是「连出去」完全不同，但上层适配器长得一样；
2. **适配器** `tickneko/platforms/bridge/<平台>.py`：包住平台服务端 / 客户端，实现
   `BotAdapter`；平台事件在 handler 里翻译成 `PlatformEvent` 后调 `publish` 投给 Gateway。
   这个模块是 bridge 里**唯一**允许 import 该平台包的地方；
3. **装配** `bootstrap.py`：凭证统一是凭证行（OneBot 行是随机令牌，反向 WS 握手按 token
   认归属；Kook 行是用户自填的 Bot Token，可逆加密落库、连接时解密）；装配读行建适配器
   （`publish=gateway.publish`）-> `gateway.register`，管理面经 `BotManager` 统一成接口层
   认的「增 / 启停 / 删」；
4. **下游不动**：接口层与工作流认的是 Gateway / 适配器的形状，只要兼容面还在，新平台上
   线**不碰** `router.py` / 节点 / 前端 —— 这是本层存在的全部意义。

## 管理面（`BotManager`）

接口层只认 `tickneko.api.api.bots.protocols` 的 `BotsService` 协议，由本模块**结构化满足**，
「Kook 的启停要 start / stop 正向 WS 客户端」这类细节封在这里，接口层不用 import 平台包。

坐在装配层注入的几样之上：`store`（`tickneko.bots.SqlBotStore`，凭证 CRUD）、`onebot` /
`kook`（两个适配器）、`secret_key`（Kook Bot Token 落库加密的密钥）。

平台分歧靠**每个平台一个 `BotLifecycle`** 收敛：OneBot 的「增」= 签发随机令牌，「停用 / 删」
要走 `set_token_enabled` / `revoke_by_id` 以便断开正连着的客户端；Kook 的「增」= 存 Bot
Token + 起正向 WS 客户端，「停用 / 删」= 停 / 删那个客户端。`BotManager` 按 `platform` 查表
派发，加第三个平台 = 写一个 lifecycle + 注册一行，本类四个方法一个不改。

## 日志接入

本层接的是 `tickneko.core.logger` 的进程门面，名为 `bridge`（相对核心 `tickneko` ->
`tickneko.bridge`）。**业务模块一律不直接 `default_core()`** —— 要日志实例就调
`bridge_logger()`；装配层也可把实例经 `logger=` 传给网关 / 各适配器。核心由装配层
（`tickneko.bootstrap` 或 `tickneko.wiring.wire_loggers`）经 `set_core()` 存进本模块槽位；
`import` 本模块**零副作用**，没装配就调 `bridge_logger()` 会当场抛错（fail fast），
不会默默按默认参数建一份把配置定死的核心。
