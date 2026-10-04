"""Opt-in message ownership. Place after a successful send to consume only handled messages."""
from __future__ import annotations

from typing import Any

from ..models import WorkflowNode
from .base import TRIGGER_PORT, NodeExecutionContext, PortSpec, input_value
from .registry import register_node


@register_node(
    "consume", label="接管消息", order=115, category="control",
    inputs=[TRIGGER_PORT, PortSpec("only_if", "message", "接管条件（可选）")], outputs=[TRIGGER_PORT],
)
async def exec_consume(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    accepted = input_value(node, ctx, "only_if", True)
    if not accepted or (isinstance(accepted, str) and accepted.strip().lower() in ("false", "0", "no", "none")):
        ctx.log.append(f"[consume] {node.id} 接管条件不成立")
        return {}
    ctx.consume_message()
    ctx.log.append(f"[consume] {node.id} 消息已接管；后续消费者不再处理")
    ctx.logger.info("消息已接管", node_id=node.id)
    return {}
