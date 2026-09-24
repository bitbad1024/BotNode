"""工作流编排：图校验流水线 + 定义 / 版本落库 + 节点执行器。

业务核心包（同 :mod:`nacho.onebot` 的地位，不 import FastAPI）：

* :mod:`nacho.workflow.models`    图（节点 / 边）、校验报告、落库记录、规范 JSON / 摘要
* :mod:`nacho.workflow.validator` 入库前校验：结构 → 拓扑 → 语义（Dry Run 留协议位）
* :mod:`nacho.workflow.store`     双表落库（定义 + 不可变版本），归属隔离
* :mod:`nacho.workflow.nodes`     节点执行器：**一类节点一个文件** + 注册表（写自己的节点看这里）
* :mod:`nacho.workflow.executor`  运行器：按拓扑顺序把图跑起来（老路径再导出节点那套）
* :mod:`nacho.workflow.runtime`   运行时：加载已发布版本的图并执行（时间触发的 start 到点后走它）

写自己的节点：新建一个模块，里面用 ``@register_node("类型")`` 标一下，启动时
``load_node_modules("你的模块")`` 装进来即可，不用改框架里的任何文件::

    from nacho.workflow import NodeExecutionContext, load_node_modules, register_node

    @register_node("dingtalk")
    async def exec_dingtalk(node, ctx: NodeExecutionContext) -> dict[str, object]:
        ...

完整指南（含类型白名单、可选依赖与测试写法）见 :file:`nacho/workflow/MODULES.md` 第 5 节。
"""
from __future__ import annotations

from .models import (
    STAGE_DRY_RUN,
    STAGE_SEMANTIC,
    STAGE_STRUCTURE,
    STAGE_TOPOLOGY,
    CurrentRef,
    DraftEdge,
    DraftGraph,
    DraftNode,
    WorkflowDefinitionRecord,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    WorkflowStatus,
    WorkflowVersionRecord,
    ValidationIssue,
    ValidationReport,
    canonical_draft_json,
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
from .executor import SimpleWorkflowRunner
from .nodes import (
    NodeExecutionContext,
    NodeExecutor,
    get_executor,
    load_node_modules,
    register_executor,
    register_node,
    registered_types,
    render_variables,
)

__all__ = [
    # 模型
    "WorkflowGraph",
    "WorkflowNode",
    "WorkflowEdge",
    "WorkflowStatus",
    "CurrentRef",
    "DraftGraph",
    "DraftNode",
    "DraftEdge",
    "WorkflowDefinitionRecord",
    "WorkflowVersionRecord",
    "ValidationIssue",
    "ValidationReport",
    "canonical_graph_json",
    "canonical_draft_json",
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
    # 节点：契约 + 注册表（写自己的节点用这些）
    "NodeExecutor",
    "NodeExecutionContext",
    "render_variables",
    "register_executor",
    "register_node",
    "get_executor",
    "registered_types",
    "load_node_modules",
    # 运行器
    "SimpleWorkflowRunner",
    # 落库
    "SqlWorkflowStore",
    "WorkflowDefinitionTable",
    "WorkflowVersionTable",
    "WorkflowError",
    "WorkflowNameConflict",
    "NAME_MAX_LENGTH",
    "NOTE_MAX_LENGTH",
]
