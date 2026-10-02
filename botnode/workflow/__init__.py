"""工作流编排：图校验流水线 + 定义 / 版本落库 + 节点执行器。

业务核心包（同 :mod:`botnode.platforms.onebot` 的地位，不 import FastAPI）：

* :mod:`botnode.workflow.models`    图（节点 / 边）、校验报告、落库记录、规范 JSON / 摘要
* :mod:`botnode.workflow.validator` 入库前校验：结构 → 拓扑 → 语义（Dry Run 留协议位）
* :mod:`botnode.workflow.store`     双表落库（定义 + 不可变版本），归属隔离
* :mod:`botnode.workflow.nodes`     节点执行器：**一类节点一个文件** + 注册表（写自己的节点看这里）
* :mod:`botnode.workflow.executor`  运行器：按拓扑顺序把图跑起来（老路径再导出节点那套）
* :mod:`botnode.workflow.runtime`   运行时：只给**开着运行开关**的已发布工作流登记定时触发，
                                  到点后加载该版本跑整条流程；开关的即时启停也在那儿

写自己的节点：新建一个模块，里面用 ``@register_node("类型")`` 标一下，启动时
``load_node_modules("你的模块")`` 装进来即可，不用改框架里的任何文件::

    from botnode.workflow import NodeExecutionContext, load_node_modules, register_node

    @register_node("dingtalk")
    async def exec_dingtalk(node, ctx: NodeExecutionContext) -> dict[str, object]:
        ...

完整指南（注册规格、必填 / 默认值、孤儿节点、可选依赖与测试写法）见
``docs/workflow/workflow.md`` 第 5 节；各模块的设计要点在同文件第 7 节。
"""
from __future__ import annotations

from .models import (
    STAGE_DRY_RUN,
    STAGE_SEMANTIC,
    STAGE_STRUCTURE,
    STAGE_TOPOLOGY,
    DraftGraph,
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
    AcceptAllExpressions,
    ExpressionSyntaxChecker,
    apply_config_defaults,
    validate_graph,
)
from .executor import SimpleWorkflowRunner
from .nodes import (
    MISSING_DEFAULT,
    NO_USER_ID,
    ConfigField,
    NodeConfigValidator,
    NodeExecutionContext,
    NodeExecutor,
    NodeRole,
    NodeSpec,
    PortSpec,
    PortType,
    TRIGGER_PORT,
    declare_node_type,
    get_executor,
    get_spec,
    input_value,
    load_node_modules,
    register_executor,
    register_node,
    registered_types,
)

__all__ = [
    # 模型
    "WorkflowGraph",
    "WorkflowNode",
    "WorkflowEdge",
    "WorkflowStatus",
    "DraftGraph",
    "WorkflowDefinitionRecord",
    "WorkflowVersionRecord",
    "ValidationIssue",
    "ValidationReport",
    "canonical_graph_json",
    "canonical_draft_json",
    "graph_checksum",
    # 校验
    "apply_config_defaults",
    "STAGE_STRUCTURE",
    "STAGE_TOPOLOGY",
    "STAGE_SEMANTIC",
    "STAGE_DRY_RUN",
    "validate_graph",
    "ExpressionSyntaxChecker",
    "AcceptAllExpressions",
    # 节点：契约 + 注册表（写自己的节点用这些）
    "NodeExecutor",
    "NodeExecutionContext",
    "NodeSpec",
    "NodeRole",
    "NodeConfigValidator",
    "ConfigField",
    "PortSpec",
    "PortType",
    "TRIGGER_PORT",
    "MISSING_DEFAULT",
    "NO_USER_ID",
    "input_value",
    "register_executor",
    "register_node",
    "declare_node_type",
    "get_executor",
    "get_spec",
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
