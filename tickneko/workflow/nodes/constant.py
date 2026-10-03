"""常量节点：**一个节点只放一个值**，要几个常量就摆几个节点。

数据沿连线走之后，它的职责很单一：给下游的某个入口供一个固定值。以前那种「一个节点塞一组
名字 -> 值、下游用 ``{{名字}}`` 引用」的写法没有了 —— 一个节点一个值，谁要就连一根线；线的
另一端是哪个入口，这个值就进哪个入口（连到 ``log.message`` 就是日志内容，连到 ``http.url``
就是请求地址）。

config:
    value: 常量值（必填；字符串 / 数字都行）

输出端口 ``value``：这个字面量本身。
"""
from __future__ import annotations

from typing import Any

from ..models import WorkflowNode
from .base import TRIGGER_PORT, ConfigField, NodeExecutionContext, PortSpec
from .registry import register_node


@register_node(
    "constant",
    label="常量",
    order=30,
    category="constant",
    inputs=[TRIGGER_PORT],
    outputs=[TRIGGER_PORT, PortSpec("value", "message", "值")],
    fields=[ConfigField("value", "值", required=True)],
)
async def exec_constant(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """把 ``config.value`` 原样从 ``value`` 端口送出去。"""
    # 只记节点不记值：常量里可能有令牌 / 口令这类东西，值不该进日志
    ctx.logger.debug(f"[constant:{node.id}] 产出常量", node_id=node.id)
    return {"value": node.config.get("value", "")}
