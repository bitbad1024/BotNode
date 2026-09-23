"""工作流运行器：把一张已校验通过的图按拓扑顺序跑起来。

**节点执行函数不在这里**：一类节点一个文件，都在 :mod:`nacho.workflow.nodes`（写自己的
节点看那个包）。本模块只管「怎么按顺序跑」：

* 按拓扑顺序逐个跑节点，每个节点接收上游所有节点的输出（变量上下文）；
* 同步执行（不并发），因为单条图的节点之间有数据依赖；并行执行留给将来的 gateway fork；
* 触发是**开始节点**自己的事（``start`` 的 ``config.trigger``）：``time`` 时它把整张流程图
  登记到 :class:`~nacho.core.scheduler.TaskManager`，由调度器按 cron 触发整条流程；
  ``message``（缺省）被动等消息接入，发布 / 试跑时只写一条开始日志。

节点那一套（``NodeExecutionContext`` / ``register_executor`` / ``get_executor`` 等）在这里
**原样再导出**一份：老代码 ``from nacho.workflow.executor import get_executor`` 照旧能用，
新代码建议直接从 :mod:`nacho.workflow.nodes` 取。
"""
from __future__ import annotations

from collections import deque
from typing import Protocol, runtime_checkable

from .models import WorkflowGraph
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
    # 运行器
    "WorkflowRunner",
    "SimpleWorkflowRunner",
    # 节点契约与注册表（再导出，见模块文档）
    "NodeExecutor",
    "NodeExecutionContext",
    "get_executor",
    "load_node_modules",
    "register_executor",
    "register_node",
    "registered_types",
    "render_variables",
]


@runtime_checkable
class WorkflowRunner(Protocol):
    """整张流程图的运行器协议（将来换成真正的执行引擎时只换这一处）。"""

    async def run(self, graph: WorkflowGraph, ctx: NodeExecutionContext) -> None:
        ...


class SimpleWorkflowRunner:
    """按拓扑顺序串行跑图的简单执行器。

    拓扑阶段已保证 DAG，这里直接按 Kahn 顺序跑；每个节点拿到**所有已跑节点的合并输出**
    作为变量上下文。并发 / 分支语义留给 gateway 节点的执行器。
    """

    async def run(self, graph: WorkflowGraph, ctx: NodeExecutionContext) -> None:
        by_id = {node.id: node for node in graph.nodes}
        in_degree: dict[str, int] = {node.id: 0 for node in graph.nodes}
        out_edges: dict[str, list[str]] = {node.id: [] for node in graph.nodes}
        for edge in graph.edges:
            out_edges[edge.source].append(edge.target)
            in_degree[edge.target] += 1

        queue = deque(node_id for node_id, deg in in_degree.items() if deg == 0)
        ran: set[str] = set()
        while queue:
            current_id = queue.popleft()
            if current_id in ran:
                continue
            node = by_id[current_id]
            executor = get_executor(node.type)
            if executor is None:
                raise NotImplementedError(f"节点类型 {node.type!r} 暂无执行器（节点 {current_id}）")
            outputs = await executor(node, ctx)
            # 合并输出到上下文（下游节点可 {{引用}}）
            ctx.variables.update(outputs)
            ran.add(current_id)
            for target in out_edges[current_id]:
                in_degree[target] -= 1
                if in_degree[target] == 0:
                    queue.append(target)

        if len(ran) != len(graph.nodes):
            missing = [nid for nid in by_id if nid not in ran]
            raise RuntimeError(f"工作流执行未完成，剩余节点：{missing}")
