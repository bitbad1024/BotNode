"""target 节点：产出「发到哪」的会话定位值，从 ``target`` 端口（数据流）送下去。

发给谁这件事从 send 节点的配置里拆出来，单独成一个可在图上流转的值：target 节点给出
「发到哪」（:class:`~botnode.platforms.bridge.models.ChatTarget`，或平台特有 target），
下游 send 节点消费它发内容。两种来源，按 ``config.platform`` 留空与否切换：

* **自动（从触发消息取）** —— ``config.platform`` 留空：直接用触发这条消息的会话定位。
  装配层把 ``PlatformEvent.target`` 塞进 ``ctx.trigger_data["target"]``（见
  :func:`botnode.bootstrap.bootstrap`），start 原样透到 target 出口，这里接上就拿到
  「回触发它的会话」的回程地址 —— 最常见的「回复它」场景，一个号都不用填；
* **手动填** —— ``config.platform`` 填了：走注入的 ``ctx.gateway.make_target`` 按平台
  构造（群号 / 对方号 / 消息号是通用文本字段，适配器按自己口径转，OneBot 转整数、
  Kook 原样字符串）。给「主动发到指定会话」的场景（定时任务发到某群）。

没有会话定位（定时触发 / 离线跑 / 事件没有会话指向）时自动分支产出 ``None``，
由下游 send 节点自己判断「没有 target 就不发」。

config:
    platform: 手动填时选平台（onebot / kook）；**留空 = 自动从触发消息取**
    chat:     会话指向（group / private），手动填用
    chat_id:  会话号（群号 / 频道号 / 对方号），手动填用
    user_id:  对方账号（私聊兜底，缺省回退 chat_id）
    message_id: 消息号（撤回一类动作要用），可选手动填

输出端口 ``target``：会话定位值（ChatTarget）。
"""
from __future__ import annotations

from typing import Any

from ..models import WorkflowNode
from .base import TRIGGER_PORT, ConfigField, NodeExecutionContext, PortSpec
from .registry import register_node


@register_node(
    "target",
    label="目标",
    order=35,
    category="target",
    inputs=[TRIGGER_PORT],
    outputs=[TRIGGER_PORT, PortSpec("target", "target", "会话定位")],
    fields=[
        ConfigField("platform", "平台", options=("", "onebot", "kook")),
        ConfigField("chat", "会话类型", options=("", "group", "private")),
        ConfigField("chat_id", "会话号"),
        ConfigField("user_id", "对方号"),
        ConfigField("message_id", "消息号"),
    ],
)
async def exec_target(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """按 ``config.platform`` 决定来源：留空自动取触发消息的会话定位，填了手动构造。"""
    platform = str(node.config.get("platform", "") or "").strip()
    if platform:
        # 手动填：交给平台适配器按它自己的口径构造（workflow 只认注入的 gateway，不 import bridge）
        if ctx.gateway is None:
            raise RuntimeError(
                "target 节点手动填会话需要 gateway（平台总线），当前上下文没装配——"
                "要么接上平台适配器，要么把「平台」留空、从触发消息自动取会话定位"
            )
        target = ctx.gateway.make_target(
            platform,
            owner_id=ctx.owner_id,
            chat=str(node.config.get("chat", "") or ""),
            chat_id=str(node.config.get("chat_id", "") or ""),
            user_id=str(node.config.get("user_id", "") or ""),
            message_id=str(node.config.get("message_id", "") or ""),
        )
        ctx.log.append(f"[target:{node.id}] 手动构造 {platform} 会话定位")
        ctx.logger.debug("手动构造会话定位", node_id=node.id, platform=platform)
        return {"target": target}
    # 自动：触发这条消息的会话定位原样透出（没有就是 None，下游自己判断）
    target = ctx.trigger_data.get("target")
    ctx.log.append(
        f"[target:{node.id}] 取触发消息的会话定位"
        + ("" if target is not None else "（无会话定位）")
    )
    ctx.logger.debug("从触发消息取会话定位", node_id=node.id)
    return {"target": target}
