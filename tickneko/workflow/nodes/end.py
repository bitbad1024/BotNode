"""结束节点：图的终点，写一条完成日志、不产出值。

config: 无。拓扑约束（至少一个可达 end / end 不允许出边）由注册规格里的
``role="end"`` 与 ``max_outgoing=0`` 声明，校验器从注册表读取，不硬编码类型名。
"""
from __future__ import annotations

from typing import Any

from ..models import WorkflowNode
from .base import TRIGGER_PORT, NodeExecutionContext
from .registry import register_node


@register_node(
    "end",
    role="end",
    max_outgoing=0,
    label="结束",
    color="#ef4444",
    order=20,
    category="end",
    inputs=[TRIGGER_PORT],
)
async def exec_end(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """结束节点：写一条完成日志（带上收尾入口有哪些），不产出值。"""
    ctx.log.append(f"[end] {node.id} 流程结束")
    ctx.logger.info("工作流结束", node_id=node.id, inputs=sorted(ctx.inputs))
    return {}
