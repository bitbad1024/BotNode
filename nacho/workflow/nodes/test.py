"""测试 / 调试节点：把配置内容回显到日志和上下文，供画布联调用。

config:
    echo: 要回显的内容（可含 ``{{变量}}``）。保存时默认填 ``hello``；手写图里没这个键时
          执行器退回用节点 id
"""
from __future__ import annotations

from typing import Any

from ..models import WorkflowNode
from .base import ConfigField, NodeExecutionContext, render_variables
from .registry import register_node


@register_node("test", fields=[ConfigField("echo", "回显内容", default="hello")])
async def exec_test(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """测试 / 调试节点：把配置内容回显到日志和上下文，供画布联调用。"""
    echo = render_variables(str(node.config.get("echo", node.id)), ctx.variables)
    ctx.logger.info(f"[test:{node.id}] {echo}", variables=dict(ctx.variables))
    ctx.log.append(f"[test] {node.id}: {echo}")
    return {"echo": echo}
