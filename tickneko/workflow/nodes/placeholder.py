"""占位节点：**没有任何功能**，只参与画布理线 —— 仿佛不存在一样。

它把入口的值**原样透传**到出口（「输出直接输出」）：不读、不改、不写日志、
不产副作用。用途是画布上理线：连线的走向被它接住再放出去，图面更整洁，
流程语义和「线直接连」完全等价 —— 画布想删掉它时，把上下游的两段线接起来即可。

config:
    无配置字段。``message`` 入口没接线时透传空串。
"""
from __future__ import annotations

from ..models import WorkflowNode
from .base import TRIGGER_PORT, NodeExecutionContext, PortSpec, input_value
from .registry import register_node


@register_node(
    "placeholder",
    label="占位",
    color="#94a3b8",
    order=49,
    category="data",
    inputs=[TRIGGER_PORT, PortSpec("message", "message", "透传内容")],
    outputs=[TRIGGER_PORT, PortSpec("message", "message", "透传结果")],
)
async def exec_placeholder(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, str]:
    """透传：入口的值原样放上出口 —— 不做任何处理，也不留运行痕迹（仿佛不存在）。"""
    return {"message": input_value(node, ctx, "message", default="")}
