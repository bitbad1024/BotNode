"""pack 节点：把字符串字段**封装**成会话定位（target）—— unpack 的逆操作。

unpack 把 target 拆成字符串（平台 / 会话类型 / 会话号 / 发送者 …），pack 把「发到哪」**拼回**
一个平台特化 target，交给下游 send 节点发消息 —— 「从参数里挑几个号拼成发到哪」、
「把存档的会话号重新变成能回复的地址」都靠它。

**输入只有两样：会话类型 + 一个号**（照着适配器的口径来）：

* ``chat`` —— ``group``（群聊 / 频道）还是 ``private``（私聊）；
* ``chat_id`` —— **按 ``chat`` 解释的号**：群聊填群号（Kook 是频道号），私聊填对方账号。

不另设「对方号」输入：两个平台的 ``make_target`` 在私聊时本来就拿 ``chat_id`` 兜底
（``user_id or chat_id``），填一个号就够。也不管消息号（``message_id``）—— 那是「撤回 /
引用回复」一类**针对某条消息**的动作才要的，发消息用不上；真要做那些动作时，事件带来的
target 本身就带着消息号，走 unpack 拿。

**按平台分两个节点**（``pack-onebot`` / ``pack-kook``）：平台写死在节点上，不用在节点上
选 —— 字符串转回**协议口径**（OneBot 号转整数、Kook 原样字符串）由各平台适配器的
``make_target`` 管，节点只做转述（workflow 只认注入的 gateway，不 import bridge 类型）。

走 ``ctx.gateway.make_target``，``owner_id`` 用这一趟的 ``ctx.owner_id``（这条会话定位
属于「谁的」机器人）。没接总线是**环境问题**，当场抛。
构造出的 target 回不了 —— 由 send 节点的 reply 在发的时候说清楚缺什么，这里不猜。

config:
    chat:       会话指向（group / private）；不填默认 other（定位不出会话，回不了）
    chat_id:    会话号：群聊填群号（Kook 填频道号），私聊填对方账号

输入端口（``chat`` / ``chat_id``）：都可接线，接上后覆盖手填。
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
            "要么接上平台适配器，要么直接用 start 的 target 出口（自动取触发消息的会话定位）"
        )
    target = gateway.make_target(
        platform,
        owner_id=ctx.owner_id,
        chat=str(input_value(node, ctx, "chat", default="") or ""),
        chat_id=str(input_value(node, ctx, "chat_id", default="") or ""),
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
        PortSpec("chat_id", "message", "会话号（群聊群号 / 私聊对方号）"),
    ],
    outputs=[TRIGGER_PORT, PortSpec("target", "target", "会话定位")],
    fields=[
        ConfigField("chat", "会话类型", options=("", "group", "private")),
        ConfigField("chat_id", "会话号（群聊群号 / 私聊对方号）"),
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
        PortSpec("chat_id", "message", "会话号（群聊频道号 / 私聊对方号）"),
    ],
    outputs=[TRIGGER_PORT, PortSpec("target", "target", "会话定位")],
    fields=[
        ConfigField("chat", "会话类型", options=("", "group", "private")),
        ConfigField("chat_id", "会话号（群聊频道号 / 私聊对方号）"),
    ],
)
async def exec_pack_kook(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """字符串字段 -> Kook 会话定位（id 本来就是字符串，原样存）。"""
    return _pack(node, ctx, "kook")
