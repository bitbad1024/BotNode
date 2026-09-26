"""OneBot 节点：向「这条工作流归属的」在线连接发一个动作（发消息 / 撤回）。

连接从哪来 —— **装配层注入**：``bootstrap`` 把 ``OneBotServer`` 交给工作流运行时
（``load_published_workflows(..., onebot=...)``），运行时登记 / 到点执行时把它连同工作流
归属一起注入节点上下文（``ctx.onebot`` / ``ctx.owner_id``）。节点在这个服务端的在线连接里
挑「谁的」连接：握手时令牌定下的 ``conn.id`` 与工作流归属是**同一套 id 空间**；同一归属
连着多条时取**最近连上**的那条。

动作与参数（``config.action``；参数都能**从连线来**：线上优先，手填兜底）::

    send_msg         智能发消息：group_id 有值就发群，否则发私聊（user_id 要有）
    send_group_msg   发群消息：group_id + message
    send_private_msg 发私聊消息：user_id + message
    delete_msg       撤回消息：message_id

群号 / 用户号 / 消息号发出去前**转成整数** —— OneBot 协议里它们是整数。

**两种失败是分开的**（与 ``http`` 节点同一口径）：

* 环境 / 配置问题 —— 没接 OneBot 服务、归属下没有在线连接、参数没给、号不是整数 ——
  当场抛出去，整条流程停在这里，看得出是没配好；
* 对方收下了但回执不成功（``status`` / ``retcode``）—— 记一条 warning 照常往下走，回执
  从 ``onebot_retcode`` / ``onebot_data`` 原样送下去（要不要处理交给下游，比如接
  ``condition`` 按 retcode 分流）。

小抄::

    群播报:   action="send_group_msg" group_id="123456" message="巡检完成 {{now_text}}"
    私聊提醒: action="send_private_msg" user_id="{{u}}" message="验证码 {{code}}"
"""
from __future__ import annotations

import json
from typing import Any

from ..models import ValidationIssue, WorkflowNode
from .base import TRIGGER_PORT, ConfigField, NodeExecutionContext, PortSpec, input_value
from .registry import register_node

#: 支持的动作，**顺序即画布下拉顺序**
ONEBOT_ACTION_ORDER: tuple[str, ...] = (
    "send_msg",
    "send_group_msg",
    "send_private_msg",
    "delete_msg",
)


def validate_onebot_node(node: WorkflowNode) -> list[ValidationIssue]:
    """动作必须是枚举里的一个（拼错保存时就拦，运行时不猜）。

    参数是否齐（群号 / 用户号 / 消息号）取决于动作，做不到表格里的「必填」，留给运行期抛
    —— 见模块文档的失败口径。
    """
    action = node.config.get("action")
    if isinstance(action, str) and action.strip() and action.strip() not in ONEBOT_ACTION_ORDER:
        return [
            ValidationIssue(
                node_id=node.id,
                code="INVALID_ONEBOT_ACTION",
                message=f"onebot 节点 {node.id} 的动作 {action!r} 不合法",
                suggestion=f"可选：{' '.join(ONEBOT_ACTION_ORDER)}",
            )
        ]
    return []


def _pick_connection(ctx: NodeExecutionContext, node_id: str) -> Any:
    """挑出「这条工作流归属的」连接；同一归属连着多条时取最近连上的那条。"""
    server = ctx.onebot
    if server is None:
        raise ConnectionError(
            f"[onebot:{node_id}] 需要 OneBot 服务：装配时把 OneBotServer 交给工作流运行时"
        )
    matches = [conn for conn in server.connections if conn.id == ctx.owner_id]
    if not matches:
        online = "、".join(sorted({conn.id or "(匿名)" for conn in server.connections})) or "无"
        raise ConnectionError(
            f"[onebot:{node_id}] 没有归属 {ctx.owner_id or '(空)'} 的在线连接（当前在线：{online}）"
        )
    return max(matches, key=lambda conn: conn.connected_at)


def _require(raw: str, name: str, node_id: str) -> str:
    """必须给的值：空白 = 没给（接线和手填都没有），当场抛。"""
    if not raw.strip():
        raise ValueError(f"[onebot:{node_id}] 动作需要 {name}，但没接线也没手填")
    return raw


def _int_of(raw: str, name: str, node_id: str) -> int:
    """号类参数转整数（协议里群号 / 用户号 / 消息号都是整数）。"""
    try:
        return int(raw.strip())
    except ValueError:
        raise ValueError(f"[onebot:{node_id}] {name} {raw!r} 不是整数") from None


def _params_of(action: str, node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, object]:
    """按动作把入口值组装成动作参数（键 = OneBot 协议里的参数名）。"""
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
        raise ValueError(f"[onebot:{node.id}] send_msg 至少要给 group_id（发群）或 user_id（发私聊）")
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


def _dump(data: object) -> str:
    """回执数据转文本：紧凑 JSON（不是 JSON 原生的值用 ``str`` 兜底）。"""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


@register_node(
    "onebot",
    label="OneBot",
    order=120,
    # 参数是否必填取决于动作（发群要群号、撤回要消息号），做不到「表格必填」，
    # 所以入口都不标 required，缺什么运行期抛（见模块文档）
    inputs=[
        TRIGGER_PORT,
        PortSpec("message", "message", "消息内容"),
        PortSpec("group_id", "message", "群号"),
        PortSpec("user_id", "message", "用户号"),
        PortSpec("message_id", "message", "消息号"),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("onebot_retcode", "message", "回执码"),
        PortSpec("onebot_data", "message", "回执数据"),
    ],
    fields=[
        ConfigField("action", "动作", default="send_msg", options=ONEBOT_ACTION_ORDER),
        ConfigField("message", "消息内容"),
        ConfigField("group_id", "群号"),
        ConfigField("user_id", "用户号"),
        ConfigField("message_id", "消息号"),
    ],
    validator=validate_onebot_node,
)
async def exec_onebot(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """向归属连接发一个动作，把回执从 ``onebot_retcode`` / ``onebot_data`` 送下去。"""
    action = str(node.config.get("action", "")).strip() or "send_msg"
    if action not in ONEBOT_ACTION_ORDER:
        # 保存时会被校验拦住；这里兜住「没走过校验」的图
        raise ValueError(
            f"[onebot:{node.id}] 动作 {action!r} 不在支持列表里"
            f"（可选 {' '.join(ONEBOT_ACTION_ORDER)}）"
        )

    params = _params_of(action, node, ctx)
    conn = _pick_connection(ctx, node.id)
    response = await conn.call(action, **params)

    # 回执不成功只是「对方的回答」：记 warning 照常往下走（连不上 / 没连接那种才抛，见模块文档）
    report = ctx.logger.info if response.ok else ctx.logger.warning
    report(
        f"[onebot:{node.id}] {action} -> retcode {response.retcode}",
        status=response.status,
        retcode=response.retcode,
        data=response.data,
    )
    ctx.log.append(f"[onebot] {node.id}: {action} -> retcode {response.retcode}")
    return {"onebot_retcode": response.retcode, "onebot_data": _dump(response.data)}
