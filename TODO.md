# Nacho 接入层改造 Roadmap

目标：抽象统一适配器层，令牌列表融合在线状态，让后续接入 Kook 等新平台时**图、节点、令牌列表全都不用动**。

## 总览

- [x] **P1** 令牌融合在线状态（接口聚合 + 前端状态列）
  - 说明：`online` 是**派生态**（内存实时态），不落库；接口层 `list_tokens` 时聚合各适配器的在线连接。
- [ ] **P2** 适配器层抽象：新增 `nacho/bridge/`（`PlatformEvent` + `BotAdapter` 协议 + `Gateway` 总线），把 `OneBotServer` 包成第一个适配器
- [ ] **P3** workflow 泛化：消息触发真正接通（start `trigger=message`）+ `onebot` 节点泛化为 `send` 节点（platform 参数）
- [ ] **P4** Kook 适配器：作为第二个适配器验证抽象是否通用

## P2 拆分

设计基调（与 P1 同路数：每步独立可验证、全量测试始终绿）：

- **包一层，不改一层**：`nacho/onebot/` 已经是干净的「连接 + 协议」层，**一行不动**；
  适配器在 bridge 里**包住** `OneBotServer`，事件翻译（`OneBotEvent` -> `PlatformEvent`）、
  能力转述（roster / kick / 发动作）都发生在包的这层。Kook（P4）照此再写一个适配器即可。
- **依赖方向**：`nacho/bridge/` 只依赖 `nacho.core`（logger）；不 import
  `nacho.api` / `nacho.workflow` / `nacho.onebot`。协议靠结构化 Protocol + PEP 563 惰性注解，
  套路抄 `api/api/onebot/protocols.py`（那边已验证：api 与 onebot 互不 import 也能对上）。
- **兼容面**：P2 不动接口层与工作流（泛化是 P3/P4 的事）。Gateway / 适配器要继续**结构化
  满足** `OneBotLike`（roster / kick / revoke_by_id / set_token_enabled / tokens）与
  `ctx.onebot`（`connections` + `conn.id` + `conn.call`）——`router.py` 与 `onebot.py` 节点
  零改动，这是 P2 的验收线之一。
- **归属 id 空间先定口径**：跨平台后「谁的」不再唯一——Gateway 层的身份是
  `(platform, owner_id)` 复合键；本阶段令牌仍是 onebot 专属（Kook 的令牌形态留给 P4），
  复合键只进 `PlatformEvent` 与 Gateway 内部，不落库、不进接口层。

- [ ] **P2-1** `bridge/models.py`：`PlatformEvent` 规范化事件——`platform` / `owner_id` /
  机器人自身账号（`self_id`）/ 事件大类（message / notice / request / meta）/ 会话指向
  （群或私聊对方的标识）/ `raw`（原始事件引用，翻译不了的字段从这里兜）；纯模型无 IO，
  配单测（字段口径 + `raw` 兜底）
- [ ] **P2-2** `bridge/protocols.py`：`BotAdapter` 协议——`platform` 标识、生命周期
  `start` / `stop`、在线列表 `clients()`（含归属与连接身份）、按归属发动作
  `send(owner_id, action, **params)`；结构化 Protocol，不 import onebot
- [ ] **P2-3** `bridge/gateway.py`：`Gateway` 总线——`register(adapter)` / `subscribe(handler)` /
  按平台路由的发送入口（找不到平台抛错，口径同 onebot 节点的「环境问题当场抛」）；
  事件分发异常口径沿用 onebot（handler 抛异常只记日志，不淹总线）；单测用 FakeAdapter
- [ ] **P2-4** `bridge/onebot.py`：`OneBotAdapter` 包 `OneBotServer`——handler 里把
  `OneBotEvent` 翻译成 `PlatformEvent` 投给 Gateway（心跳过滤沿用 `_emit` 的口径）；
  roster / kick / revoke_by_id / set_token_enabled / connections / conn.call 透传；
  测试复用 `test_onebot.py` 的真 WS 基建，断言翻译结果与透传行为
- [ ] **P2-5** 装配切换 `bootstrap.py`：建 `OneBotServer` -> 包成 `OneBotAdapter` -> 注册进
  `Gateway`；`on_event` 日志钩子改为订阅 Gateway（记规范化字段，不再认识 OneBotEvent）；
  接口层与工作流注入点换成 Gateway 对外的兼容面（对 `router.py` / `nodes/onebot.py` 形状不变）
- [ ] **P2-6** 收尾：`tests/test_bridge.py`（FakeAdapter 走总线全链路：注册 -> 事件 ->
  订阅 -> 路由发送）；全量回归对齐当前基线（全绿才算完）；`nacho/bridge/__init__.py`
  文档写清「适配器怎么写第二个」
