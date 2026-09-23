"""兼容节点：旧版独立 ``time-trigger`` 类型（已发布的历史版本快照里可能还有）。

新画布只产生 ``start`` + ``config.trigger=time``（cron 登记那套在 :mod:`.start`）；这里
保留注册只是让旧快照不重新发布也能继续触发。类型已从
:data:`~nacho.workflow.models.NodeType` 白名单里去掉，新图不要再建这个节点。
"""
from __future__ import annotations

from typing import Any

from ..models import WorkflowNode
from .base import NodeExecutionContext
from .registry import register_node
from .start import _register_cron


@register_node("time-trigger")
async def exec_legacy_time_trigger(
    node: WorkflowNode, ctx: NodeExecutionContext
) -> dict[str, Any]:
    """旧版 ``time-trigger`` 节点的兼容执行器：行为同 ``start`` 的时间触发。"""
    return await _register_cron(node, ctx)
