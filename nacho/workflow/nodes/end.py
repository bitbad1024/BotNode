"""结束节点：图的终点，写一条完成日志、不产出新变量。

config: 无。
"""
from __future__ import annotations

from typing import Any

from ..models import WorkflowNode
from .base import NodeExecutionContext
from .registry import register_node


@register_node("end")
async def exec_end(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """结束节点：写一条完成日志，不产出新变量。"""
    ctx.log.append(f"[end] {node.id} 流程结束")
    ctx.logger.info("工作流结束", node_id=node.id, variables=list(ctx.variables))
    return {}
