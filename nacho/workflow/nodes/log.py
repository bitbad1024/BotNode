"""写日志节点：按级别把 message 写进业务日志（支持 ``{{变量}}`` 替换）。

config:
    message: 日志内容（必填，可含 ``{{变量}}``）
    level:   DEBUG / INFO / WARNING / ERROR / CRITICAL，缺省 INFO
"""
from __future__ import annotations

from typing import Any

from ..models import WorkflowNode
from .base import NodeExecutionContext, render_variables
from .registry import register_node


@register_node("log")
async def exec_log(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """写日志节点：按级别把 message 写进业务日志（支持 ``{{变量}}`` 替换）。"""
    message = render_variables(str(node.config.get("message", "")), ctx.variables)
    level = str(node.config.get("level", "INFO")).upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        level = "INFO"
    getattr(ctx.logger, level.lower())(f"[log:{node.id}] {message}")
    ctx.log.append(f"[{level}] {node.id}: {message}")
    return {"log_message": message}
