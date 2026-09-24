"""写日志节点：按级别把 message 写进业务日志（支持 ``{{变量}}`` 替换）。

config:
    message: 日志内容（必填，可含 ``{{变量}}``）
    level:   DEBUG / INFO / WARNING / ERROR / CRITICAL，缺省 INFO（保存时自动补）

必填与默认值在注册规格的 :class:`~nacho.workflow.nodes.base.ConfigField` 里声明，
level 枚举校验在 :func:`validate_log_node` 里。
"""
from __future__ import annotations

from typing import Any

from ..models import ValidationIssue, WorkflowNode
from .base import ConfigField, NodeExecutionContext, render_variables
from .registry import register_node

#: 合法日志级别（config.level 缺省 INFO）
LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def validate_log_node(node: WorkflowNode) -> list[ValidationIssue]:
    """level 给了就得是合法级别（缺省由默认值字段补 INFO，不用这里管）。"""
    level = node.config.get("level")
    if level is not None and (not isinstance(level, str) or level.upper() not in LOG_LEVELS):
        return [
            ValidationIssue(
                node_id=node.id,
                code="INVALID_LOG_LEVEL",
                message=f"log 节点 {node.id} 的级别 {level!r} 不合法",
                suggestion=f"可选级别：{', '.join(sorted(LOG_LEVELS))}（缺省 INFO）",
            )
        ]
    return []


@register_node(
    "log",
    fields=[
        ConfigField("message", "日志内容", required=True),
        ConfigField("level", "日志级别", default="INFO"),
    ],
    validator=validate_log_node,
)
async def exec_log(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """写日志节点：按级别把 message 写进业务日志（支持 ``{{变量}}`` 替换）。"""
    message = render_variables(str(node.config.get("message", "")), ctx.variables)
    level = str(node.config.get("level", "INFO")).upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        level = "INFO"
    getattr(ctx.logger, level.lower())(f"[log:{node.id}] {message}")
    ctx.log.append(f"[{level}] {node.id}: {message}")
    return {"log_message": message}
