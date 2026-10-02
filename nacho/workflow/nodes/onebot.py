"""onebot 节点：``send`` 节点的别名（target 化改造后与 send 同款输入），给库里已存的旧图兜底。

target 化之前，``onebot`` 节点带 ``action`` / ``group_id`` / ``user_id`` / ``message_id``
参数，按动作组装请求走 ``ctx.gateway.send``。target 化改造后（breaking）：**onebot 与 send
同款输入** —— ``target``（会话定位）+ ``message``（消息内容），执行同样走
``ctx.gateway.reply(target, message)``；唯一差别是回执按**老端口名**（``onebot_retcode`` /
``onebot_data``）转述出去。

回执转述口径：

* ``onebot_data`` —— 与 ``send`` 的 ``send_data`` 一样，是回执数据的紧凑 JSON；
* ``onebot_retcode`` —— OneBot 的回执码（从平台回执的 ``raw`` 上取；取不到时成功记 0、
  失败记 1）。这是**老字段**：跨平台后平台无关的信号是 ``send_ok``，retcode 是 OneBot 专属的，
  留在 ``raw`` 里兜底。

**没有 target 就不发**（与 send 同一口径）：``target`` 没接、或接了但运行值是 None / 空串，
不发 —— ``onebot_retcode`` 记 1（没发出去）、``onebot_data`` 空串送下游，不抛。

**内容为空也不发**（与 send 同一口径）：``message`` 是空串 / 纯空白时同样不发 ——
``onebot_retcode`` 记 1、``onebot_data`` 空串送下游。

**breaking 提醒**：库里旧的 ``onebot`` 图（用 ``action`` / ``group_id`` 配置、上游接
``message`` / ``group_id`` 端口）已不能照跑，需迁移到 ``send`` + ``target`` 节点
（去向统一走 target 值端口）。
"""
from __future__ import annotations

from typing import Any

from ..models import WorkflowNode
from .base import TRIGGER_PORT, ConfigField, NodeExecutionContext, PortSpec, input_value
from .registry import register_node
from .send import _clip, _dump


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
    # 与 send 同款输入：去向（target，只能接线）+ 内容（message，接线或手填）
    inputs=[
        TRIGGER_PORT,
        PortSpec("target", "target", "会话定位", required=True),
        PortSpec("message", "message", "消息内容", required=True),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("onebot_retcode", "message", "回执码"),
        PortSpec("onebot_data", "message", "回执数据"),
    ],
    fields=[
        ConfigField("message", "消息内容"),
    ],
)
async def exec_onebot(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """onebot 别名：与 send 同款输入，走 ``ctx.gateway.reply``，回执转老端口名。"""
    target = input_value(node, ctx, "target", default=None)
    if not target:
        # 没有会话定位：不发（与 send 同一口径），retcode 记 1（没发出去）
        ctx.logger.info(f"[onebot:{node.id}] 没有会话定位，跳过发送", node_id=node.id)
        ctx.log.append(f"[onebot] {node.id}: 没有会话定位，跳过")
        return {"onebot_retcode": 1, "onebot_data": ""}

    message = str(input_value(node, ctx, "message", default=""))
    gateway = ctx.gateway
    if gateway is None:
        raise ConnectionError(
            f"[onebot:{node.id}] 需要平台总线：装配时把 Gateway 交给工作流运行时（ctx.gateway）"
        )

    if not message.strip():
        # 内容为空（上游「算不出来」送的就是空串）：不发，retcode 记 1（与 send 同一口径）
        ctx.logger.warning(f"[onebot:{node.id}] 消息内容为空，跳过发送", node_id=node.id)
        ctx.log.append(f"[onebot] {node.id}: 内容为空，跳过")
        return {"onebot_retcode": 1, "onebot_data": ""}

    response = await gateway.reply(target, message)
    ok = bool(response.ok)
    retcode = _retcode_of(response)
    data = _dump(response.data)

    # 回执不成功只是「对方的回答」：记 warning 照常往下走（连不上 / 没连接那种才抛，见模块文档）
    report = ctx.logger.info if ok else ctx.logger.warning
    fields: dict[str, object] = {
        "ok": ok,
        "retcode": retcode,
        "data": data,
        "message_chars": len(message),
    }
    if not ok:  # 失败才带内容片段：看得出到底发了什么
        fields["message_preview"] = _clip(message)
    report(f"[onebot:{node.id}] reply -> retcode {retcode}", **fields)
    ctx.log.append(f"[onebot] {node.id}: reply -> retcode {retcode}")
    return {"onebot_retcode": retcode, "onebot_data": data}
