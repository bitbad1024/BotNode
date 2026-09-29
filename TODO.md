# Nacho 接入层改造 Roadmap

目标：抽象统一适配器层，令牌列表融合在线状态，让后续接入 Kook 等新平台时**图、节点、令牌列表全都不用动**。

## 总览

- [x] **P1** 令牌融合在线状态（接口聚合 + 前端状态列）
  - 说明：`online` 是**派生态**（内存实时态），不落库；接口层 `list_tokens` 时聚合各适配器的在线连接。
- [x] **P2** 适配器层抽象：新增 `nacho/bridge/`（`PlatformEvent` + `BotAdapter` 协议 + `Gateway` 总线），把 `OneBotServer` 包成第一个适配器
- [ ] **P3** workflow 泛化：消息触发真正接通（start `trigger=message`）+ `onebot` 节点泛化为 `send` 节点（platform 参数）
- [ ] **P4** Kook 适配器：作为第二个适配器验证抽象是否通用

## P3 拆分

设计基调（与 P2 同路数：每步独立可验证、全量测试始终绿）：

- **消息触发 = 时间触发的对偶**：`trigger=time` 把整条流程登记到调度器（cron 到点跑）；
  `trigger=message` 把整条流程登记到「消息路由」（收到 message 事件时跑）。两者都在
  「登记那一趟」（拨运行开关 / 启动载入 / 发布新版）登记，执行那一趟不碰登记表 ——
  与 `NodeExecutionContext.register_triggers` 同一套口径。
- **路由键 = owner_id**：消息发给哪个机器人（owner，握手时令牌定下的 id），就触发那个
  owner 下所有 `trigger=message` 且开着运行开关的已发布工作流；发消息的人是 `user_id`
  （`ctx.trigger_data` 带上 message 等字段，start 的 message 端口原样送下去）。
- **依赖方向不破**：消息路由**不 import bridge** —— 它只认普通数据（``trigger_data``
  字典 + ``owner_id`` / ``user_id``），「PlatformEvent 拆成这些普通数据」这件事由装配层
  （bootstrap）做，与 P2「bridge 不 import workflow / onebot」对得上。
- **send 节点 = onebot 节点的平台无关版**：动作与参数沿用 onebot 那套集合（现在它是唯一
  平台），发送从「直接摸 `ctx.onebot` 挑连接」换成「`gateway.send(platform, owner_id,
  action, **params)` 按平台路由」；`platform` 参数缺省 `"onebot"`。onebot 专属的动作语义
  （群号 / 用户号 / 消息号转整数）仍由节点自己管 —— 那是这个平台的动作契约，不是别的
  平台的事。
- **兼容面**：旧图里的 `onebot` 类型保留为 `send` 的别名（platform 恒 onebot），库里已存的
  图照跑照校验；接口层 `OneBotLike` 与 `ctx.onebot` 兼容面 P2 已满足，本阶段不破坏。
- **注入点**：`ctx` 新增 `gateway`（发送能力，与 `onebot` 并列）；登记链路构造的到点闭包
  要把它带上（与 onebot 同一路数，见 MODULES.md §5「给 ctx 注入新能力」）—— 消息触发跑
  整条流程那一趟也要能拿得到它发动作。

- [ ] **P3-1** 消息触发运行时：`run_published_workflow` 支持注入 `trigger_data` / `user_id`
  （消息进 start 的 message 端口，user_id 进 `ctx.user_id`）；`runtime.py` 加
  `MessageRouter` —— `register(workflow_id, version, owner_id)` / `unregister(...)` /
  `dispatch(owner_id, *, trigger_data)`（按 owner_id 找到匹配工作流逐个跑整条流程，注入
  trigger_data / user_id，单个失败不淹其它，口径同 `load_published_workflows`）。纯逻辑
  + FakeStore 单测
- [ ] **P3-2** 登记链路接通消息触发：`register_published_workflow` / `stop_published_workflow`
  / `load_published_workflows` / `WorkflowTriggers` 识别 `trigger=message` 的开始节点，
  登记 / 摘除到 `MessageRouter`（与 time 触发对称：登记那一趟加，执行那一趟不碰）
- [ ] **P3-3** 装配接通：`bootstrap.py` 建 `MessageRouter`、`on_platform_event` 收到
  `kind=message` 事件时拆成普通数据并 `dispatch`；全量回归对齐基线（全绿才算完）
- [ ] **P3-4** send 节点：`nodes/onebot.py` 泛化为 `send` —— `platform` 参数（缺省 onebot），
  发送改走 `ctx.gateway`（`gateway.send(platform, owner_id, action, **params)`），回执从
  `ActionResult` 泛化成 `send_ok` / `send_data` 送下游（失败回执照常送、不打断流程，
  环境问题当场抛，口径不变）
- [ ] **P3-5** 透传 gateway + onebot 别名：`NodeExecutionContext` 加 `gateway` 注入点；
  `runtime.py` / `bootstrap.py` 全链路透传（登记闭包带上）；旧图 `onebot` 类型注册为
  `send` 的别名（platform 恒 onebot），库里已存的 onebot 节点照跑
- [ ] **P3-6** 收尾：前端颜色表 `onebot` -> `send`（面板项 / 中文名 / 端口 / 表单全从后端
  注册表来，前端只补颜色）；`nodes/__init__.py` / MODULES.md 文档同步；全量回归对齐基线
