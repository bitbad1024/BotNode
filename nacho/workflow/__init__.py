"""工作流编排：图校验流水线 + 定义 / 版本落库。

业务核心包（同 :mod:`nacho.onebot` 的地位，不 import FastAPI）：

* :mod:`nacho.workflow.models`    图（节点 / 边）、校验报告、落库记录、规范 JSON / 摘要
* :mod:`nacho.workflow.validator` 入库前校验：结构 → 拓扑 → 语义（Dry Run 留协议位）
* :mod:`nacho.workflow.store`     双表落库（定义 + 不可变版本），归属隔离
"""
from __future__ import annotations

from .models import (
    STAGE_DRY_RUN,
    STAGE_SEMANTIC,
    STAGE_STRUCTURE,
    STAGE_TOPOLOGY,
    WorkflowDefinitionRecord,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    WorkflowStatus,
    WorkflowVersionRecord,
    ValidationIssue,
    ValidationReport,
    canonical_graph_json,
    graph_checksum,
)
from .store import (
    NAME_MAX_LENGTH,
    NOTE_MAX_LENGTH,
    SqlWorkflowStore,
    WorkflowDefinitionTable,
    WorkflowError,
    WorkflowNameConflict,
    WorkflowVersionTable,
)
from .validator import (
    NODE_TYPES,
    AcceptAllExpressions,
    DryRunner,
    ExpressionSyntaxChecker,
    validate_graph,
    validate_with_dry_run,
)

__all__ = [
    # 模型
    "WorkflowGraph",
    "WorkflowNode",
    "WorkflowEdge",
    "WorkflowStatus",
    "WorkflowDefinitionRecord",
    "WorkflowVersionRecord",
    "ValidationIssue",
    "ValidationReport",
    "canonical_graph_json",
    "graph_checksum",
    # 校验
    "NODE_TYPES",
    "STAGE_STRUCTURE",
    "STAGE_TOPOLOGY",
    "STAGE_SEMANTIC",
    "STAGE_DRY_RUN",
    "validate_graph",
    "validate_with_dry_run",
    "ExpressionSyntaxChecker",
    "AcceptAllExpressions",
    "DryRunner",
    # 落库
    "SqlWorkflowStore",
    "WorkflowDefinitionTable",
    "WorkflowVersionTable",
    "WorkflowError",
    "WorkflowNameConflict",
    "NAME_MAX_LENGTH",
    "NOTE_MAX_LENGTH",
]
