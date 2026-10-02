"""工作流入口包：``<prefix>/workflows/*`` 一组接口。

存储只认 :class:`~botnode.api.api.workflow.protocols.WorkflowStoreLike` 这份协议（不认
``botnode.workflow`` 的实现类），装配时由主程序挂到 ``app.state.workflow_store`` 上。
"""
from __future__ import annotations

from .protocols import WorkflowStoreLike, WorkflowTriggerLike
from .requests import SetEnabledRequest
from .responses import PublishedWorkflowData
from .router import router

__all__ = [
    "PublishedWorkflowData",
    "SetEnabledRequest",
    "WorkflowStoreLike",
    "WorkflowTriggerLike",
    "router",
]
