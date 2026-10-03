"""send 节点：把 ``message`` 发到 ``target`` 指向的会话（去向统一走 target 值端口）。

这是 target 化改造后的 send：**「发给谁」不再写在 send 节点配置里**，而是从图上
一条数据流拿 —— ``target`` 端口接的必须是**会话定位**（target 类型的值）：

* ``start`` 的 ``target`` 出口原样透出触发消息自带的会话定位（回复触发消息就是
  ``start.target -> send.target`` 一条线）；
* 手动指定去向用 ``target`` 节点：填了 platform 就走 ``gateway.make_target`` 构造
  一个平台特化 target（群聊 / 私聊都行），platform 留空就从触发消息取。

执行时调 ``ctx.gateway.reply(target, message)`` —— 平台差异（OneBot 整数号、Kook
字符串号）由各平台适配器翻译，节点不再逐平台认字段。

**没有 target 就不发**：``target`` 端口接了线但运行值是 None / 空串（定时触发没有
会话指向、事件没有会话定位、target 节点自动分支产出 None）时，不发 —— ``send_ok``
照常送 ``False`` 到下游，不抛。要「到点固定播报」就把 target 节点（手动填）接在
send 前面。

**内容为空也不发**：``message`` 是空串 / 纯空白时不发 —— ``send_ok`` 送 ``False`` 到
下游。上游「算不出来」这类业务失败送下来的就是**空串**（``operator`` / ``json`` /
``regex`` 同一口径），不拦就等于往平台上发一条空消息，平台多半回个参数错误。

**两种失败是分开的**（与 ``http`` 节点同一口径）：

* 环境 / 配置问题 —— 没接总线（``ctx.gateway`` 是 None）—— 当场抛出去，整条流程
  停在这里，看得出是没配好；
* 对方收下了但回执不成功（``ActionResult.ok`` 为 False）—— **业务失败**：记一条 warning
  后抛 :class:`~tickneko.workflow.nodes.base.NodeFailure`，引擎**停止它向下传播**（下游整段
  跳过），不再把「没发出去」当结果往下送。

小抄::

    回复触发消息: start.target -> send.target, start.message -> send.message
    群播报:      target 节点（platform="onebot" chat="group" chat_id="123456"）-> send.target,
                 message 手填或接常量
"""
from __future__ import annotations

import json
from typing import Any

from ..models import WorkflowNode
from .base import (
    TRIGGER_PORT,
    ConfigField,
    NodeExecutionContext,
    NodeFailure,
    PortSpec,
    input_value,
)
from .registry import register_node


#: 日志里展示「发出去的内容」时的截断长度（避免一段长文本刷屏）
CLIP_CHARS: int = 40


def _clip(raw: str) -> str:
    """日志里展示的内容片段：太长截断（够看出发了什么就行）。"""
    return raw if len(raw) <= CLIP_CHARS else raw[:CLIP_CHARS] + "…"


def _dump(data: object) -> str:
    """回执数据转文本：紧凑 JSON（不是 JSON 原生的值用 ``str`` 兜底）。"""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


@register_node(
    "send",
    label="发送",
    order=120,
    category="action",
    # 去向（target）与内容（message）都要有来源：接线或手填（message 可手填，target 只能接线）
    inputs=[
        TRIGGER_PORT,
        PortSpec("target", "target", "会话定位", required=True),
        PortSpec("message", "message", "消息内容", required=True),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("send_ok", "message", "是否发出"),
        PortSpec("send_data", "message", "回执数据"),
    ],
    fields=[
        ConfigField("message", "消息内容"),
    ],
)
async def exec_send(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """把 ``message`` 发到 ``target`` 指向的会话；没有会话定位就不发，回执照常送下游。"""
    target = input_value(node, ctx, "target", default=None)
    if not target:
        # 没有会话定位：不发（target 节点文档口径：没有 target 就不发），send_ok=False 照常送下游
        ctx.logger.info(f"[send:{node.id}] 没有会话定位，跳过发送", node_id=node.id)
        ctx.log.append(f"[send] {node.id}: 没有会话定位，跳过")
        return {"send_ok": False, "send_data": ""}

    message = str(input_value(node, ctx, "message", default=""))
    gateway = ctx.gateway
    if gateway is None:
        raise ConnectionError(
            f"[send:{node.id}] 需要平台总线：装配时把 Gateway 交给工作流运行时（ctx.gateway）"
        )

    if not message.strip():
        # 内容为空（上游「算不出来」送的就是空串）：不发 —— 与「没有 target 就不发」同口径，
        # 免得把上游的空值当内容真的发到平台上
        ctx.logger.warning(f"[send:{node.id}] 消息内容为空，跳过发送", node_id=node.id)
        ctx.log.append(f"[send] {node.id}: 内容为空，跳过")
        return {"send_ok": False, "send_data": ""}

    response = await gateway.reply(target, message)
    ok = bool(response.ok)
    data = _dump(response.data)

    # 回执不成功只是「对方的回答」：记 warning 照常往下走（连不上 / 没连接那种才抛，见模块文档）
    report = ctx.logger.info if ok else ctx.logger.warning
    fields: dict[str, object] = {
        "target_platform": getattr(target, "platform", ""),
        "ok": ok,
        "data": data,
        "message_chars": len(message),
    }
    if not ok:
        # 失败才带内容片段：光一个 ok=False 看不出去的是什么（上游送了空串 / 内容被截断都能一眼看出）
        fields["message_preview"] = _clip(message)
    report(f"[send:{node.id}] reply -> {'ok' if ok else 'failed'}", **fields)
    ctx.log.append(f"[send] {node.id}: reply -> {'ok' if ok else 'failed'}")
    if not ok:
        # 回执不成功是「对方的回答」= 业务失败：停止向下传播（见模块文档）
        raise NodeFailure(f"发送失败：{response.message or '回执不成功'}（{data}）")
    return {"send_ok": True, "send_data": data}
