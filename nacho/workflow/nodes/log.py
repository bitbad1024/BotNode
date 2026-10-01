"""写日志节点：按级别把一条内容写进业务日志。

内容从 **message 入口**来：连了线就用线上送来的值（上游节点的产出），没接线就用同名字段
``config.message`` 手填的字面量。这是「数据沿连线走」的一个典型消费端 —— 自己不产出值。

config:
    message: 日志内容（**没接线时**的手填值）
    level:   DEBUG / INFO / WARNING / ERROR / CRITICAL，缺省 INFO（保存时自动补）

端口与「入口必填」在注册规格里声明，level 枚举校验在 :func:`validate_log_node` 里。
"""
from __future__ import annotations

from typing import Any

from ..models import ValidationIssue, WorkflowNode
from .base import TRIGGER_PORT, ConfigField, NodeExecutionContext, PortSpec, input_value
from .registry import register_node

#: 合法日志级别，**顺序即画布下拉顺序**（config.level 缺省 INFO）
LOG_LEVEL_ORDER: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

#: 合法日志级别的集合形态（校验用；与上面的顺序表同一份内容）
LOG_LEVELS: frozenset[str] = frozenset(LOG_LEVEL_ORDER)


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
    label="写日志",
    order=40,
    category="data",
    # message 入口是数据端口（required：必须接线或手填）；同名字段是没接线时的字面量兜底
    inputs=[TRIGGER_PORT, PortSpec("message", "message", "日志内容", required=True)],
    outputs=[TRIGGER_PORT],
    fields=[
        ConfigField("message", "日志内容"),
        ConfigField("level", "日志级别", default="INFO", options=LOG_LEVEL_ORDER),
    ],
    validator=validate_log_node,
)
async def exec_log(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """写日志节点：把入口（或手填）的内容按级别写进业务日志；自己不产出值。"""
    message = str(input_value(node, ctx, "message", default=""))
    level = str(node.config.get("level", "INFO")).upper()
    if level not in LOG_LEVELS:
        level = "INFO"
    getattr(ctx.logger, level.lower())(f"[log:{node.id}] {message}")
    ctx.log.append(f"[{level}] {node.id}: {message}")
    return {}
