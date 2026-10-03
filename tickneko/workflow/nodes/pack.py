"""pack 节点：把字符串字段**封装**成会话定位（target）—— unpack 的逆操作。

unpack 把 target 拆成字符串（平台 / 会话类型 / 会话号 / 发送者 / 消息号），pack 把这几样
**拼回**一个平台特化 target，交给下游 send 节点发消息 —— 「从参数里挑几个号拼成发到哪」、
「把存档的会话号重新变成能回复的地址」都靠它。

**按平台分两个节点**（``pack-onebot`` / ``pack-kook``）：平台写死在节点上，不用像 target
节点那样选 —— 字符串转回**协议口径**（OneBot 号转整数、Kook 原样字符串）由各平台适配器的
``make_target`` 管，节点只做转述（workflow 只认注入的 gateway，不 import bridge 类型）。

与 target 节点手动填分支同一口径：走 ``ctx.gateway.make_target``，``owner_id`` 用这一趟的
``ctx.owner_id``（这条会话定位属于「谁的」机器人）。没接总线是**环境问题**，当场抛。

会话号不全不额外拦（与 target 手动填同口径）：group 没填会话号 / private 没填对方号，
构造出的 target 回不了 —— 由 send 节点的 reply 在发的时候说清楚缺什么，这里不猜。

config:
    chat:       会话指向（group / private）；不填默认 other（定位不出会话，回不了）
    chat_id:    会话号（群号 / 频道号）
    user_id:    对方账号（私聊用；缺省回退 chat_id）
    message_id: 消息号（撤回一类动作要用）

输入端口（``chat`` / ``chat_id`` / ``user_id`` / ``message_id``）：都可接线，接上后覆盖手填。
输出端口 ``target``：会话定位值（ChatTarget）。
"""
from __future__ import annotations

from typing import Any

from ..models import WorkflowNode
from .base import TRIGGER_PORT, ConfigField, NodeExecutionContext, PortSpec, input_value
from .registry import register_node


def _pack(node: WorkflowNode, ctx: NodeExecutionContext, platform: str) -> dict[str, Any]:
    """封装公共逻辑：从端口 / 手填取值，交给平台适配器的 ``make_target`` 拼回会话定位。"""
    gateway = ctx.gateway
    if gateway is None:
        raise RuntimeError(
            f"pack-{platform} 节点封装会话需要 gateway（平台总线），当前上下文没装配——"
            "要么接上平台适配器，要么改用 target 节点从触发消息自动取会话定位"
        )
    target = gateway.make_target(
        platform,
        owner_id=ctx.owner_id,
        chat=str(input_value(node, ctx, "chat", default="") or ""),
        chat_id=str(input_value(node, ctx, "chat_id", default="") or ""),
        user_id=str(input_value(node, ctx, "user_id", default="") or ""),
        message_id=str(input_value(node, ctx, "message_id", default="") or ""),
    )
    ctx.log.append(f"[pack:{node.id}] {platform} 会话定位已封装")
    ctx.logger.debug("会话封装", node_id=node.id, platform=platform)
    return {"target": target}


@register_node(
    "pack-onebot",
    label="会话封装·OneBot",
    color="#2dd4bf",
    order=38,
    category="onebot",
    inputs=[
        TRIGGER_PORT,
        PortSpec("chat", "message", "会话类型"),
        PortSpec("chat_id", "message", "会话号"),
        PortSpec("user_id", "message", "发送者"),
        PortSpec("message_id", "message", "消息ID"),
    ],
    outputs=[TRIGGER_PORT, PortSpec("target", "target", "会话定位")],
    fields=[
        ConfigField("chat", "会话类型", options=("", "group", "private")),
        ConfigField("chat_id", "会话号"),
        ConfigField("user_id", "发送者"),
        ConfigField("message_id", "消息ID"),
    ],
)
async def exec_pack_onebot(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """字符串字段 -> OneBot 会话定位（号转整数由适配器管）。"""
    return _pack(node, ctx, "onebot")


@register_node(
    "pack-kook",
    label="会话封装·Kook",
    color="#0d9488",
    order=39,
    category="kook",
    inputs=[
        TRIGGER_PORT,
        PortSpec("chat", "message", "会话类型"),
        PortSpec("chat_id", "message", "会话号"),
        PortSpec("user_id", "message", "发送者"),
        PortSpec("message_id", "message", "消息ID"),
    ],
    outputs=[TRIGGER_PORT, PortSpec("target", "target", "会话定位")],
    fields=[
        ConfigField("chat", "会话类型", options=("", "group", "private")),
        ConfigField("chat_id", "会话号"),
        ConfigField("user_id", "发送者"),
        ConfigField("message_id", "消息ID"),
    ],
)
async def exec_pack_kook(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """字符串字段 -> Kook 会话定位（id 本来就是字符串，原样存）。"""
    return _pack(node, ctx, "kook")
