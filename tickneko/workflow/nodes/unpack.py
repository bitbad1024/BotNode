"""unpack 节点：把会话定位（target）**解包**成一组字符串字段（平台 / 会话类型 / 会话号 / 发送者 / 消息号 / 归属）。

与 send / target 相对：target 是「回程地址」（:class:`~tickneko.platforms.bridge.models.ChatTarget`
或平台特化 target），画布上直接接线用 —— 但**判断**得靠拆开的字段：「收到的是群聊还是私聊」、
「谁发的」、「哪条消息」，还有日志里要落「发到哪」。unpack 就是那个拆的动作：把结构化的
会话定位拆成普通文本字段，下游能接线 / 能比较 / 能进日志。

**按平台分两个节点**（``unpack-onebot`` / ``unpack-kook``）—— 各平台的 target 定位字段名
不同：OneBot 的群号叫 ``group_id`` 且是**整数**，Kook 的频道号叫 ``chat_id`` 且是**字符串**。
节点知道自己平台的字段名，读出来**统一字符串化**（``int -> str``、没有 / ``None`` 都是空串）：
解包出来的六路输出**全是字符串**，下游不必逐平台认字段。

**装配错位看得见**：解包节点只认**本平台**的 target（生产与消费同平台，见
:class:`~tickneko.platforms.bridge.models.ChatTarget`）—— 收到别平台的会话定位当场
ValueError，不静默拆出空字段（那会让人以为「字段名错了」）。

**没有会话定位不打断**：``target`` 端口接了线但运行值是 ``None``（定时触发 / 事件没有会话
指向，target 节点自动分支产出的就是 None）时，照常送**全空串**到下游 —— 与 send 节点
「没有 target 就不发」同一口径，数据流节点不因空值炸流程。

config：无（target 只能接线，不能手填 —— 会话定位是结构值，表单填不了）。

输出端口（``platform`` / ``chat`` / ``chat_id`` / ``user_id`` / ``message_id`` / ``owner_id``）
全是字符串：``chat`` 是 ``group`` / ``private`` / ``other``（与 make_target / send 同一口径）；
``chat_id`` 是会话号（群聊才有；私聊的对方号在 ``user_id``）；``owner_id`` 是这条定位属于谁
（回程地址所在的那条连接的主人，日志 / 排查用）。
"""
from __future__ import annotations

from typing import Any

from ..models import WorkflowNode
from .base import TRIGGER_PORT, NodeExecutionContext, PortSpec, input_value
from .registry import register_node


def _field(target: object, name: str) -> str:
    """鸭子读 target 的一个定位字段：没有 / ``None`` 都按空串；有的字符串化（``int -> str``）。"""
    value = getattr(target, name, None)
    return "" if value is None else str(value)


def _unpack(node: WorkflowNode, ctx: NodeExecutionContext, platform: str, chat_id_attr: str) -> dict[str, Any]:
    """解包公共逻辑：拆出六路字符串字段；平台错位当场炸；没有会话定位送全空串。"""
    target = input_value(node, ctx, "target", default=None)
    if target is None:
        # 没有会话定位（定时触发 / 事件没有指向）：全空串照常送下游，不打断流程
        ctx.log.append(f"[unpack:{node.id}] 没有会话定位，输出全空串")
        ctx.logger.debug("会话解包：没有会话定位", node_id=node.id)
        return {key: "" for key in ("platform", "chat", "chat_id", "user_id", "message_id", "owner_id")}

    actual = getattr(target, "platform", "")
    if actual and actual != platform:
        # 装配错位看得见：解包某平台的节点却收到别平台的 target，当场 ValueError（同 reply 口径）
        raise ValueError(
            f"会话解包·{platform} 收到的会话定位来自 {actual!r} 平台，生产与消费必须同平台"
        )

    result = {
        "platform": _field(target, "platform"),
        "chat": _field(target, "chat"),
        "chat_id": _field(target, chat_id_attr),
        "user_id": _field(target, "user_id"),
        "message_id": _field(target, "message_id"),
        "owner_id": _field(target, "owner_id"),
    }
    ctx.log.append(f"[unpack:{node.id}] {platform} 会话定位 -> 平台/类型/会话号/发送者/消息号")
    ctx.logger.debug("会话解包", node_id=node.id, **result)
    return result


@register_node(
    "unpack-onebot",
    label="会话解包·OneBot",
    color="#38bdf8",
    order=36,
    category="onebot",
    inputs=[TRIGGER_PORT, PortSpec("target", "target", "会话定位", required=True)],
    outputs=[
        TRIGGER_PORT,
        PortSpec("platform", "message", "平台"),
        PortSpec("chat", "message", "会话类型"),
        PortSpec("chat_id", "message", "会话号"),
        PortSpec("user_id", "message", "发送者"),
        PortSpec("message_id", "message", "消息ID"),
        PortSpec("owner_id", "message", "归属"),
    ],
)
async def exec_unpack_onebot(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """OneBot 会话定位 -> 字符串字段：群号读 ``group_id``（整数，转字符串）。"""
    return _unpack(node, ctx, "onebot", chat_id_attr="group_id")


@register_node(
    "unpack-kook",
    label="会话解包·Kook",
    color="#0284c7",
    order=37,
    category="kook",
    inputs=[TRIGGER_PORT, PortSpec("target", "target", "会话定位", required=True)],
    outputs=[
        TRIGGER_PORT,
        PortSpec("platform", "message", "平台"),
        PortSpec("chat", "message", "会话类型"),
        PortSpec("chat_id", "message", "会话号"),
        PortSpec("user_id", "message", "发送者"),
        PortSpec("message_id", "message", "消息ID"),
        PortSpec("owner_id", "message", "归属"),
    ],
)
async def exec_unpack_kook(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """Kook 会话定位 -> 字符串字段：频道号读 ``chat_id``（本来就是字符串）。"""
    return _unpack(node, ctx, "kook", chat_id_attr="chat_id")
