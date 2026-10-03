"""占位节点：**没有任何功能**，只参与画布理线 —— 仿佛不存在一样。

它把入口的值**原样透传**到出口（「输出直接输出」）：不读、不改、不写日志、
不产副作用。用途是画布上理线：连线的走向被它接住再放出去，图面更整洁，
流程语义和「线直接连」完全等价 —— 画布想删掉它时，把上下游的两段线接起来即可。

config:
    无配置字段。``value`` 入口没接线时透传空串。
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
    # 透传口是泛型：输入接什么类型（message / target / list …），输出就是什么类型 ——
    # 接会话定位就出会话定位。泛型只接数据流端口，不接触发（见 port_types 的接线语义）。
    # 输入输出用 tie 互相声明成「透传对」：画布上输入输出显示同一种类型（接什么显什么），
    # 两端同色即表示这层对应。
    inputs=[TRIGGER_PORT, PortSpec("value", "generic", "透传值", tie="value")],
    outputs=[TRIGGER_PORT, PortSpec("value", "generic", "透传结果", tie="value")],
)
async def exec_placeholder(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, str]:
    """透传：入口的值原样放上出口 —— 不做任何处理，也不留运行痕迹（仿佛不存在）。"""
    return {"value": input_value(node, ctx, "value", default="")}
