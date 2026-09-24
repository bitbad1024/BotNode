"""工作流运行器：把一张已校验通过的图按拓扑顺序跑起来。

**节点执行函数不在这里**：一类节点一个文件，都在 :mod:`nacho.workflow.nodes`（写自己的
节点看那个包）。本模块只管「怎么按顺序跑」：

* 只跑 **start 沿出边可达的主流程节点**：孤儿节点（没接进主流程的散点）允许存在于图里、
  允许保存，但永不执行、也不会因为没有执行器而拖垮主流程；
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

from .models import WorkflowGraph, WorkflowNode
from .nodes import (
    NodeExecutionContext,
    NodeExecutor,
    get_executor,
    get_spec,
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

    只执行 start 可达的主流程：先在可达子图上算入度，再按 Kahn 顺序跑；每个节点拿到
    **所有已跑节点的合并输出**作为变量上下文。孤儿节点不在可达集合里，永远不执行
    （就算它的类型没有执行器也不影响主流程）。并发 / 分支语义留给 gateway 节点的执行器。

    图里找不到 start 角色节点时（直接构造的测试图、最早期的旧快照）退回「跑全部节点」，
    与历史行为一致。
    """

    async def run(self, graph: WorkflowGraph, ctx: NodeExecutionContext) -> None:
        by_id = {node.id: node for node in graph.nodes}
        out_edges: dict[str, list[str]] = {node.id: [] for node in graph.nodes}
        for edge in graph.edges:
            out_edges[edge.source].append(edge.target)

        entry = self._entry_id(graph.nodes)
        runnable = (
            self._reachable_ids(out_edges, entry)
            if entry is not None
            else set(by_id)  # 没有 start（测试图 / 最早期快照）：退回跑全部，沿用历史行为
        )

        # 入度只数 runnable 内部的边：孤儿指向主流程的入边不能把主节点卡住（孤儿永不执行，
        # 口径与校验器一致——校验器的入度同样只在 start 可达子图内统计）
        in_degree: dict[str, int] = {node_id: 0 for node_id in runnable}
        for node_id in runnable:
            for target in out_edges[node_id]:
                if target in in_degree:
                    in_degree[target] += 1

        queue = deque(node_id for node_id in runnable if in_degree[node_id] == 0)
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
            # 合并输出到上下文（下游可 {{引用}}）
            ctx.variables.update(outputs)
            ran.add(current_id)
            for target in out_edges[current_id]:
                if target not in runnable:
                    continue
                in_degree[target] -= 1
                if in_degree[target] == 0:
                    queue.append(target)

        if len(ran) != len(runnable):
            missing = [nid for nid in runnable if nid not in ran]
            raise RuntimeError(f"工作流执行未完成，剩余节点：{missing}")

    @staticmethod
    def _entry_id(nodes: list[WorkflowNode]) -> str | None:
        """找入口：注册角色为 start 的节点；没有时退回 type 字面量为 start 的旧图。"""
        for node in nodes:
            spec = get_spec(node.type)
            if spec is not None and spec.role == "start":
                return node.id
        for node in nodes:
            if node.type == "start":
                return node.id
        return None

    @staticmethod
    def _reachable_ids(out_edges: dict[str, list[str]], start_id: str) -> set[str]:
        """从入口沿出边可达的节点集合（含入口自己）。"""
        reachable: set[str] = set()
        queue: deque[str] = deque([start_id])
        while queue:
            current = queue.popleft()
            if current in reachable:
                continue
            reachable.add(current)
            for target in out_edges.get(current, ()):
                if target not in reachable:
                    queue.append(target)
        return reachable
