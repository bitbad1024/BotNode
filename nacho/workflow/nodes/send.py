"""send 节点：向「这条工作流归属的」在线连接按**平台路由**发一个动作（发消息 / 撤回）。

这是 ``onebot`` 节点的**平台无关版**（P3）：``onebot`` 节点直接摸 ``ctx.onebot`` 挑连接，
``send`` 节点换成走 ``ctx.gateway``（``gateway.send(platform, owner_id, action, **params)``）
按平台路由 —— 平台差异（OneBot 的整数号、Kook 的字符串号）由各平台的适配器翻译，节点不再
逐平台认字段。

``platform`` 参数（``config.platform``，缺省 ``"onebot"``）选平台；动作与参数沿用 onebot 那套
集合（现在 onebot 是唯一平台，所以动作语义 —— 群号 / 用户号 / 消息号转整数 —— 仍由节点自己
管，那是这个平台的动作契约）。归属用 ``ctx.owner_id``（与 onebot 节点同一套 id 空间）。

动作与参数（``config.action``；参数都能**从连线来**：线上优先，手填兜底）::

    send_msg         智能发消息：group_id 有值就发群，否则发私聊（user_id 要有）
    send_group_msg   发群消息：group_id + message
    send_private_msg 发私聊消息：user_id + message
    delete_msg       撤回消息：message_id

群号 / 用户号 / 消息号发出去前**转成整数** —— OneBot 协议里它们是整数。

**两种失败是分开的**（与 ``http`` 节点同一口径）：

* 环境 / 配置问题 —— 没接总线（``ctx.gateway`` 是 None）、没这个平台、归属下没有在线连接、
  参数没给、号不是整数 —— 当场抛出去，整条流程停在这里，看得出是没配好；
* 对方收下了但回执不成功（``ActionResult.ok`` 为 False）—— 记一条 warning 照常往下走，
  回执从 ``send_ok`` / ``send_data`` 送下去（要不要处理交给下游，比如接 ``condition`` 按
  ``send_ok`` 分流）。

小抄::

    群播报:   platform="onebot" action="send_group_msg" group_id="123456" message="巡检完成 {{now_text}}"
    私聊提醒: platform="onebot" action="send_private_msg" user_id="{{u}}" message="验证码 {{code}}"
"""
from __future__ import annotations

import json
from typing import Any

from ..models import ValidationIssue, WorkflowNode
from .base import TRIGGER_PORT, ConfigField, NodeExecutionContext, PortSpec, input_value
from .registry import register_node

#: 支持的动作，**顺序即画布下拉顺序**
SEND_ACTION_ORDER: tuple[str, ...] = (
    "send_msg",
    "send_group_msg",
    "send_private_msg",
    "delete_msg",
)

#: 平台参数的缺省值：现在唯一平台是 onebot
DEFAULT_PLATFORM: str = "onebot"


def validate_send_node(node: WorkflowNode) -> list[ValidationIssue]:
    """动作必须是枚举里的一个（拼错保存时就拦，运行时不猜）。

    参数是否齐（群号 / 用户号 / 消息号）取决于动作，做不到表格里的「必填」，留给运行期抛
    —— 见模块文档的失败口径。
    """
    action = node.config.get("action")
    if isinstance(action, str) and action.strip() and action.strip() not in SEND_ACTION_ORDER:
        return [
            ValidationIssue(
                node_id=node.id,
                code="INVALID_SEND_ACTION",
                message=f"send 节点 {node.id} 的动作 {action!r} 不合法",
                suggestion=f"可选：{' '.join(SEND_ACTION_ORDER)}",
            )
        ]
    return []


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
    """按动作把入口值组装成动作参数（键 = 平台协议里的参数名；现在就是 OneBot 的）。"""
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


def _dump(data: object) -> str:
    """回执数据转文本：紧凑 JSON（不是 JSON 原生的值用 ``str`` 兜底）。"""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


async def send_response(
    node: WorkflowNode, ctx: NodeExecutionContext, action: str, params: dict[str, object]
) -> Any:
    """按平台路由发动作，返回**平台回执**（``ActionResult``）；环境问题当场抛。

    这是 ``send`` 节点与 ``onebot`` 别名共用的发送口：``onebot`` 节点借它发、再按老端口
    （``onebot_retcode`` / ``onebot_data``）转述回执。
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
        PortSpec("send_ok", "message", "是否发出"),
        PortSpec("send_data", "message", "回执数据"),
    ],
    fields=[
        ConfigField("platform", "平台", default=DEFAULT_PLATFORM),
        ConfigField("action", "动作", default="send_msg", options=SEND_ACTION_ORDER),
        ConfigField("message", "消息内容"),
        ConfigField("group_id", "群号"),
        ConfigField("user_id", "用户号"),
        ConfigField("message_id", "消息号"),
    ],
    validator=validate_send_node,
)
async def exec_send(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """按平台路由向归属连接发动作，把回执从 ``send_ok`` / ``send_data`` 送下去。"""
    action = str(node.config.get("action", "")).strip() or "send_msg"
    if action not in SEND_ACTION_ORDER:
        # 保存时会被校验拦住；这里兜住「没走过校验」的图
        raise ValueError(
            f"[send:{node.id}] 动作 {action!r} 不在支持列表里"
            f"（可选 {' '.join(SEND_ACTION_ORDER)}）"
        )

    platform = _platform_of(node)
    params = _params_of(action, node, ctx)
    response = await send_response(node, ctx, action, params)
    ok = bool(response.ok)
    data = _dump(response.data)

    # 回执不成功只是「对方的回答」：记 warning 照常往下走（连不上 / 没连接那种才抛，见模块文档）
    report = ctx.logger.info if ok else ctx.logger.warning
    report(
        f"[send:{node.id}] {platform}:{action} -> {'ok' if ok else 'failed'}",
        platform=platform,
        ok=ok,
        data=data,
    )
    ctx.log.append(f"[send] {node.id}: {platform}:{action} -> {'ok' if ok else 'failed'}")
    return {"send_ok": ok, "send_data": data}
