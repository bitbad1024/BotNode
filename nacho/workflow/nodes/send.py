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

**两种失败是分开的**（与 ``http`` 节点同一口径）：

* 环境 / 配置问题 —— 没接总线（``ctx.gateway`` 是 None）—— 当场抛出去，整条流程
  停在这里，看得出是没配好；
* 对方收下了但回执不成功（``ActionResult.ok`` 为 False）—— 记一条 warning 照常往下
  走，回执从 ``send_ok`` / ``send_data`` 送下去（要不要处理交给下游，比如接
  ``condition`` 按 ``send_ok`` 分流）。

小抄::

    回复触发消息: start.target -> send.target, start.message -> send.message
    群播报:      target 节点（platform="onebot" chat="group" chat_id="123456"）-> send.target,
                 message 手填或接常量
"""
from __future__ import annotations

import json
from typing import Any

from ..models import WorkflowNode
from .base import TRIGGER_PORT, ConfigField, NodeExecutionContext, PortSpec, input_value
from .registry import register_node

#: onebot 支持的动作，**顺序即画布下拉顺序**（onebot 别名节点沿用这份动作集）
SEND_ACTION_ORDER: tuple[str, ...] = (
    "send_msg",
    "send_group_msg",
    "send_private_msg",
    "delete_msg",
)

#: kook 支持的动作（独立一组，不复用 onebot 的）：Kook 的 id 是**字符串**，动作语义不同
KOOK_ACTION_ORDER: tuple[str, ...] = (
    "send_channel_msg",
    "send_dm_msg",
    "delete_msg",
)

#: 平台 -> 动作组：onebot 别名节点按平台决定「认哪些动作 / 怎么转参数」
ACTIONS_BY_PLATFORM: dict[str, tuple[str, ...]] = {
    "onebot": SEND_ACTION_ORDER,
    "kook": KOOK_ACTION_ORDER,
}

#: 平台参数的缺省值：现在唯一平台是 onebot
DEFAULT_PLATFORM: str = "onebot"


def _platform_of(node: WorkflowNode) -> str:
    """这个节点发到哪个平台（``config.platform``，缺省 onebot）。"""
    return str(node.config.get("platform", "")).strip() or DEFAULT_PLATFORM


def _require(raw: str, name: str, node_id: str) -> str:
    """必须给的值：空白 = 没给（接线和手填都没有），当场抛。"""
    if not raw.strip():
        raise ValueError(f"[send:{node_id}] 动作需要 {name}，但没接线也没手填")
    return raw


def _int_of(raw: str, name: str, node_id: str) -> int:
    """号类参数转整数（OneBot 协议里群号 / 用户号 / 消息号都是整数）。"""
    try:
        return int(raw.strip())
    except ValueError:
        raise ValueError(f"[send:{node_id}] {name} {raw!r} 不是整数") from None


def _params_of(action: str, node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, object]:
    """按动作把入口值组装成动作参数（键 = 平台协议里的参数名）。

    按 ``config.platform`` 分派：onebot 走号转整数那套，kook 走字符串那套（id 不转整数）。
    """
    if _platform_of(node) == "kook":
        return _kook_params(action, node, ctx)
    return _onebot_params(action, node, ctx)


def _onebot_params(
    action: str, node: WorkflowNode, ctx: NodeExecutionContext
) -> dict[str, object]:
    """onebot 平台的参数：群号 / 用户号 / 消息号转整数。"""
    message = str(input_value(node, ctx, "message", default=""))
    group_id = str(input_value(node, ctx, "group_id", default=""))
    user_id = str(input_value(node, ctx, "user_id", default=""))
    message_id = str(input_value(node, ctx, "message_id", default=""))

    if action == "send_group_msg":
        return {
            "group_id": _int_of(_require(group_id, "group_id", node.id), "group_id", node.id),
            "message": _require(message, "message", node.id),
        }
    if action == "send_private_msg":
        return {
            "user_id": _int_of(_require(user_id, "user_id", node.id), "user_id", node.id),
            "message": _require(message, "message", node.id),
        }
    if action == "delete_msg":
        return {
            "message_id": _int_of(
                _require(message_id, "message_id", node.id), "message_id", node.id
            ),
        }
    # send_msg：智能分流 —— 填了群号就发群，否则发私聊
    if not group_id.strip() and not user_id.strip():
        raise ValueError(f"[send:{node.id}] send_msg 至少要给 group_id（发群）或 user_id（发私聊）")
    if group_id.strip():
        return {
            "message_type": "group",
            "group_id": _int_of(group_id, "group_id", node.id),
            "message": _require(message, "message", node.id),
        }
    return {
        "message_type": "private",
        "user_id": _int_of(user_id, "user_id", node.id),
        "message": _require(message, "message", node.id),
    }


def _kook_params(
    action: str, node: WorkflowNode, ctx: NodeExecutionContext
) -> dict[str, object]:
    """kook 平台的参数：id 一律字符串**不转整数**，键名用 Kook 的（target_id / content / msg_id）。"""
    message = str(input_value(node, ctx, "message", default=""))
    group_id = str(input_value(node, ctx, "group_id", default=""))  # 频道 id（channel_id）
    user_id = str(input_value(node, ctx, "user_id", default=""))  # 私聊对方
    message_id = str(input_value(node, ctx, "message_id", default=""))

    if action == "send_channel_msg":
        return {
            "target_id": _require(group_id, "group_id（频道号）", node.id),
            "content": _require(message, "message", node.id),
        }
    if action == "send_dm_msg":
        return {
            "target_id": _require(user_id, "user_id", node.id),
            "content": _require(message, "message", node.id),
        }
    # delete_msg
    return {
        "msg_id": _require(message_id, "message_id", node.id),
    }


def _dump(data: object) -> str:
    """回执数据转文本：紧凑 JSON（不是 JSON 原生的值用 ``str`` 兜底）。"""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


async def send_response(
    node: WorkflowNode, ctx: NodeExecutionContext, action: str, params: dict[str, object]
) -> Any:
    """按平台路由发动作，返回**平台回执**（``ActionResult``）；环境问题当场抛。

    这是 ``onebot`` 别名节点的发送口（send 节点自己走 ``ctx.gateway.reply``，不经过这里）：
    ``onebot`` 节点借它发、再按老端口（``onebot_retcode`` / ``onebot_data``）转述回执。
    """
    gateway = ctx.gateway
    if gateway is None:
        raise ConnectionError(
            f"[send:{node.id}] 需要平台总线：装配时把 Gateway 交给工作流运行时（ctx.gateway）"
        )
    platform = _platform_of(node)
    return await gateway.send(platform, ctx.owner_id, action, **params)


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

    response = await gateway.reply(target, message)
    ok = bool(response.ok)
    data = _dump(response.data)

    # 回执不成功只是「对方的回答」：记 warning 照常往下走（连不上 / 没连接那种才抛，见模块文档）
    report = ctx.logger.info if ok else ctx.logger.warning
    report(
        f"[send:{node.id}] reply -> {'ok' if ok else 'failed'}",
        target_platform=getattr(target, "platform", ""),
        ok=ok,
        data=data,
    )
    ctx.log.append(f"[send] {node.id}: reply -> {'ok' if ok else 'failed'}")
    return {"send_ok": ok, "send_data": data}
