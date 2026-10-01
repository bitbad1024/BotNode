"""onebot 节点：``send`` 节点的别名（platform 恒 onebot），给库里已存的旧图兜底。

P3 把 ``onebot`` 节点泛化成了 ``send``（``platform`` 参数 + 走 ``ctx.gateway`` 按平台路由），
本模块是**兼容面**：``onebot`` 类型继续注册在表里，老图里的 ``onebot`` 节点照跑照校验 ——
执行逻辑直接复用 :func:`nacho.workflow.nodes.send.send_response`（platform 锁死 ``"onebot"``），
只把回执按老端口名（``onebot_retcode`` / ``onebot_data``）转述出去。

回执转述口径：

* ``onebot_data`` —— 与 ``send`` 的 ``send_data`` 一样，是回执数据的紧凑 JSON；
* ``onebot_retcode`` —— OneBot 的回执码（从平台回执的 ``raw`` 上取；取不到时成功记 0、
  失败记 1）。这是**老字段**：跨平台后平台无关的信号是 ``send_ok``，retcode 是 OneBot 专属的，
  留在 ``raw`` 里兜底。

新图直接写 ``send``（``platform`` 缺省就是 onebot）；只有「库里已有的旧 onebot 节点」走这里。
"""
from __future__ import annotations

from typing import Any

from ..models import ValidationIssue, WorkflowNode
from .base import TRIGGER_PORT, ConfigField, NodeExecutionContext, PortSpec
from .registry import register_node
from .send import SEND_ACTION_ORDER, _dump, _params_of, send_response

#: 与 send 的动作集同一份（顺序即画布下拉顺序）；onebot 是 send 的别名，动作一个不差
ONEBOT_ACTION_ORDER: tuple[str, ...] = SEND_ACTION_ORDER

#: onebot 别名锁死的平台
ONEBOT_PLATFORM: str = "onebot"


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


def _retcode_of(response: Any) -> int:
    """从平台回执取 OneBot 的 retcode：优先下探 ``raw``，取不到用成功 / 失败兜底。"""
    raw = getattr(response, "raw", None)
    if raw is not None:
        retcode = getattr(raw, "retcode", None)
        if retcode is not None:
            try:
                return int(retcode)
            except (TypeError, ValueError):
                pass  # retcode 不是整数时按成功 / 失败兜底，别让回执转述打断流程
    return 0 if getattr(response, "ok", False) else 1


@register_node(
    "onebot",
    label="OneBot",
    order=120,
    category="action",
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
    """onebot 别名：platform 锁死 onebot，走 ``ctx.gateway`` 发动作，回执转老端口名。"""
    action = str(node.config.get("action", "")).strip() or "send_msg"
    if action not in ONEBOT_ACTION_ORDER:
        # 保存时会被校验拦住；这里兜住「没走过校验」的图
        raise ValueError(
            f"[onebot:{node.id}] 动作 {action!r} 不在支持列表里"
            f"（可选 {' '.join(ONEBOT_ACTION_ORDER)}）"
        )

    # 锁死 platform：onebot 是 send 的别名，不管 config 里写没写、写什么，都发 onebot
    node = node.model_copy(update={"config": {**node.config, "platform": ONEBOT_PLATFORM}})
    params = _params_of(action, node, ctx)
    response = await send_response(node, ctx, action, params)

    ok = bool(response.ok)
    retcode = _retcode_of(response)
    data = _dump(response.data)

    # 回执不成功只是「对方的回答」：记 warning 照常往下走（连不上 / 没连接那种才抛，见模块文档）
    report = ctx.logger.info if ok else ctx.logger.warning
    report(
        f"[onebot:{node.id}] {action} -> retcode {retcode}",
        ok=ok,
        retcode=retcode,
        data=data,
    )
    ctx.log.append(f"[onebot] {node.id}: {action} -> retcode {retcode}")
    return {"onebot_retcode": retcode, "onebot_data": data}
