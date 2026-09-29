# Nacho 接入层改造 Roadmap

目标：抽象统一适配器层，令牌列表融合在线状态，让后续接入 Kook 等新平台时**图、节点、令牌列表全都不用动**。

## 总览

- [x] **P1** 令牌融合在线状态（接口聚合 + 前端状态列）
  - 说明：`online` 是**派生态**（内存实时态），不落库；接口层 `list_tokens` 时聚合各适配器的在线连接。
- [x] **P2** 适配器层抽象：新增 `nacho/bridge/`（`PlatformEvent` + `BotAdapter` 协议 + `Gateway` 总线），把 `OneBotServer` 包成第一个适配器
- [x] **P3** workflow 泛化：消息触发真正接通（start `trigger=message`）+ `onebot` 节点泛化为 `send` 节点（platform 参数）
- [x] **P4** Kook 适配器：作为第二个适配器验证抽象是否通用
- [ ] **P5** 通用机器人基础设施：`/api/onebot/*` 泛化成 `/api/bots/*`；令牌表泛化成「机器人行」
  （一条一个机器人，platform + owner_id + bot_id）；OneBot 兼容（旧客户端用旧令牌仍能连）
- [ ] **P6** Kook 接入管理面：Kook 机器人也走 `/api/bots/*`（添 Bot Token 时填 platform=kook）；
  前端「机器人」页改成「添加机器人选底层适配器（onebot / kook）」

设计基调：

- **「机器人」= 一行凭证**：用户「添加机器人」时选**底层适配器**（onebot / kook），生成的
  是一行「凭证行」（``platform`` + ``owner_id``（归属用户）+ ``bot_id``（这一行的主键，
  不再是 owner_id 主键））。一个用户可有多个机器人（多实例），身份从「owner_id 主键」
  升级为「(platform, bot_id) 复合键」。
- **OneBot 兼容面（不破坏现有）**：旧客户端用旧令牌（``nbo_``）仍能连 —— 反向 WS 握手
  按 token 解析时，**带 platform 列的旧表行被当成 platform=onebot**；新签的 OneBot 机器人
  生成新 id，旧行（owner_id 主键）按需迁移成 bot_id 主键。**接口层 OneBotLike 管理面
  不破坏**（P2 验收线之一）。
- **Kook 的「机器人」= 一个 Bot Token**：Kook 不是「签令牌连反向 WS」，是「填 Bot Token
  连正向网关」。所以 Kook 的「凭证行」存的是**用户填的 Bot Token**（敏感，需加密存储或
  只存摘要 + 用户回填），owner_id 是归属用户，bot_id 是这一行。
- **接口泛化，语义不硬套**：`/api/bots/*` 是通用入口；OneBot 的「踢客户端 / 吊销令牌」
  语义套不到 Kook（Kook 单 bot 无「多归属客户端」概念），Kook 的管理面在 P6 按「启用 /
  停用 / 删」做，不硬套 OneBot 的「踢 / revoke」。
- **前端名字泛化**：导航 `/tokens` -> `/bots`，页面标题「机器人」；`tokensApi` ->
  `botsApi`（类型 `BotToken` / `BotClient` 不再带 onebot 前缀）；接口路径 `/api/bots/*`。

- [x] **P5-1** 模型与存储：新增通用「机器人行」模型（platform / owner_id / bot_id /
  加密 token / enabled / account / remark / created_at），`bot_credentials` 表；兼容
  旧 `onebot_tokens`（读时按 platform=onebot 兜底，老客户端握手仍认）。配单测（表结构 /
  加密 / 读旧行）。
- [x] **P5-2** 接口层泛化：`/api/onebot/*` 改挂 `/api/bots/*`（旧路径保留转发或标记废弃）；
  `OneBotLike` 协议泛化成 `BotLike`（按 platform 路由到对应适配器）；签发改成「按 platform
  生成凭证行」。配单测（接口路径 / 签发分平台）。
- [ ] **P5-3** 前端泛化：导航 `/tokens` -> `/bots`，`tokensApi` -> `botsApi`，「添加机器人」
  弹窗加「底层适配器」下拉（onebot / kook，kook 暂置灰或提示 P6 开放）；OneBot 机器人
  走旧流程。配前端联调。
- [ ] **P5-4** 兼容与迁移：老 `onebot_tokens` 数据按需迁移成 `bot_credentials`（owner_id
  主键 -> bot_id）；保留旧 `/api/onebot/*` 兼容期；全量回归。
- [ ] **P6-1** Kook 凭证行：`bot_credentials` 支持 platform=kook（存 Bot Token，加密）；
  装配层读 Kook 凭证行建 KookAdapter（不再只认 `[kook]` 配置节）。
- [ ] **P6-2** Kook 管理面：`/api/bots/*` 支持 Kook 机器人的「添加 / 启用 / 停用 / 删」
  （不硬套 OneBot 的踢/revoke）；前端「添加机器人」开放 kook 选项。
- [ ] **P6-3** 收尾：`bridge/__init__.py` / MODULES.md 文档同步（「凭证行」概念、platform
  复合键）；全量回归；勾掉 P5 / P6。
