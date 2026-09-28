# Nacho 接入层改造 Roadmap

目标：抽象统一适配器层，令牌列表融合在线状态，让后续接入 Kook 等新平台时**图、节点、令牌列表全都不用动**。

## 总览

- [x] **P1** 令牌融合在线状态（接口聚合 + 前端状态列）
  - 说明：`online` 是**派生态**（内存实时态），不落库；接口层 `list_tokens` 时聚合各适配器的在线连接。
- [ ] **P2** 适配器层抽象：新增 `nacho/bridge/`（`PlatformEvent` + `BotAdapter` 协议 + `Gateway` 总线），把 `OneBotServer` 包成第一个适配器
- [ ] **P3** workflow 泛化：消息触发真正接通（start `trigger=message`）+ `onebot` 节点泛化为 `send` 节点（platform 参数）
- [ ] **P4** Kook 适配器：作为第二个适配器验证抽象是否通用

## P1 拆分

- [x] **P1-1** 后端：`TokenData` 模型加 `online` / `clients` 字段（`responses.py`）
- [x] **P1-2** 后端：`list_tokens` 聚合在线状态（`router.py`；`protocols.py` 无需改，`roster(id=)` 本就在协议里）
- [x] **P1-3** 前端：令牌列表加在线状态列（状态灯 + 在线连接信息，悬浮看详情）
- [x] **P1-4** 测试验证 + 收尾（`test_onebot.py` 补在线聚合断言；全量 612 passed / 1 skipped）
- [x] **P1-5** 后端：同一令牌同时只允许一条连接（新连接顶掉旧连接，`server.py` 登记处）
- [x] **P1-6** 前端：令牌列表改卡片式（每卡一令牌，开关管一个机器人，吊销入口保留）
- [x] **P1-7** 前端：点击卡片展开当前连接详情（QQ 号 / 对端地址 / 在线时长，前端实时算）
- [ ] **P1-8** 收尾：移除独立「在线客户端」面板（信息并入卡片），补测试，全量验证
