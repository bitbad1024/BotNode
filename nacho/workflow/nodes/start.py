"""开始节点：图的起点，透传输入、不产出新变量。

config: 无（``start`` 就是入口标记本身）。
"""
from __future__ import annotations

from typing import Any

from ..models import WorkflowNode
from .base import NodeExecutionContext
from .registry import register_node


@register_node("start")
async def exec_start(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """开始节点：透传输入，不产出新变量（图的起点）。"""
    ctx.log.append(f"[start] {node.id} 流程开始")
    ctx.logger.info("工作流开始", node_id=node.id)
    return {}
