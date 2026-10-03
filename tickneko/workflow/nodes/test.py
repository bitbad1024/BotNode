"""测试 / 调试节点：把入口送来的值回显到日志，再从自己的出口送下去。

它两头都有端口，所以常用在中间「看一眼线上到底流过了什么」：上游连到 ``message`` 入口，
自己的 ``message`` 出口再接给下游。

config:
    message: 回显内容（**没接线时**的手填值，缺省 ``hello``；手写图里连这个键都没有时用节点 id）
"""
from __future__ import annotations

from typing import Any

from ..models import WorkflowNode
from .base import TRIGGER_PORT, ConfigField, NodeExecutionContext, PortSpec, input_value
from .registry import register_node


@register_node(
    "test",
    label="测试",
    color="#8b5cf6",
    order=50,
    category="data",
    inputs=[TRIGGER_PORT, PortSpec("message", "message", "回显内容")],
    outputs=[TRIGGER_PORT, PortSpec("message", "message", "回显")],
    fields=[ConfigField("message", "回显内容", default="hello")],
)
async def exec_test(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """回显入口的值（没接线时用 config.message），原样从 ``message`` 出口送下去。"""
    echo = str(input_value(node, ctx, "message", default=node.id))
    ctx.logger.info(f"[test:{node.id}] {echo}", inputs=sorted(ctx.inputs))
    ctx.log.append(f"[test] {node.id}: {echo}")
    return {"message": echo}
