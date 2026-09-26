"""工作流运行器：把一张已校验通过的图按拓扑顺序跑起来，值沿边流。

**节点执行函数不在这里**：一类节点一个文件，都在 :mod:`nacho.workflow.nodes`（写自己的
节点看那个包）。本模块只管「怎么按顺序跑、值怎么沿边流」：

* 只跑 **start 沿出边可达的主流程节点**：孤儿节点（没接进主流程的散点）允许存在于图里、
  允许保存，但永不执行、也不会因为没有执行器而拖垮主流程；
* 按拓扑顺序逐个跑节点。跑之前按**入边**把上游产出投递到本节点的入口（``ctx.inputs``，
  键 = 目标端口名），跑完把返回值按**输出端口名**记下来，供下游取；
* 同步执行（不并发），因为单条图的节点之间有数据依赖；并行 / 分支执行留给将来新增的
  分流类节点（内置里没有这类，要加得先补执行器）；
* 触发是**开始节点**自己的事（``start`` 的 ``config.trigger``）：``time`` 时它把整张流程图
  登记到 :class:`~nacho.core.scheduler.TaskManager`，由调度器按 cron 触发整条流程；
  ``message``（缺省）被动等消息接入，发布 / 试跑时只写一条开始日志。

图的公共算法（出边索引 / 可达集合 / 入口节点 / 边端口）在 :mod:`nacho.workflow.graph`，
与校验器共用同一份口径。

这里只再导出 ``SimpleWorkflowRunner`` / ``NodeExecutionContext`` / ``get_executor`` 三个：
老代码 ``from nacho.workflow.executor import ...`` 的写法还能用，新代码直接从
:mod:`nacho.workflow` 取。
"""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

from .graph import (
    DEFAULT_EDGE_PORT,
    edge_source_port,
    edge_target_port,
    entry_id,
    out_targets,
    reachable_from,
)
from .models import WorkflowEdge, WorkflowGraph
from .nodes import NodeExecutionContext, get_executor

__all__ = [
    # 老 import 路径留的门（新代码从 nacho.workflow 取）
    "NodeExecutionContext",
    "SimpleWorkflowRunner",
    "get_executor",
]


class SimpleWorkflowRunner:
    """按拓扑顺序串行跑图的简单执行器。

    只执行 start 可达的主流程：先在可达子图上算入度，再按 Kahn 顺序跑；每个节点执行前，
    引擎按入边把上游产出投进 ``ctx.inputs``（键 = 目标端口名；控制流端口不送值，上游没跑过
    的边不算数，跑过但没产出的才送空串）。孤儿节点不在可达集合里，永远不执行（就算它的类型
    没有执行器也不影响主流程）。并发 / 分支语义留给将来新增的分流类节点。
    """

    async def run(self, graph: WorkflowGraph, ctx: NodeExecutionContext) -> None:
        by_id = {node.id: node for node in graph.nodes}
        out_edges = out_targets(graph)
        #: 目标节点 -> 指向它的边（按边投递数据时用）
        in_edges: dict[str, list[WorkflowEdge]] = {node.id: [] for node in graph.nodes}
        for edge in graph.edges:
            in_edges[edge.target].append(edge)

        entry = entry_id(graph)
        if entry is None:
            # 拓扑阶段要求 start 有且仅有一个，所以走到这儿说明图没过校验
            raise RuntimeError("工作流没有 start 节点，无法确定入口")

        runnable = reachable_from(out_edges, entry)

        # 入度只数 runnable 内部的边：孤儿指向主流程的入边不能把主节点卡住（孤儿永不执行，
        # 口径与校验器一致——校验器的入度同样只在 start 可达子图内统计）
        in_degree: dict[str, int] = {node_id: 0 for node_id in runnable}
        for node_id in runnable:
            for target in out_edges[node_id]:
                if target in in_degree:
                    in_degree[target] += 1

        queue = deque(node_id for node_id in runnable if in_degree[node_id] == 0)
        ran: set[str] = set()
        #: 节点 ID -> 它的产出（键 = 输出端口名）；下游按边从这里取
        produced: dict[str, dict[str, Any]] = {}
        while queue:
            current_id = queue.popleft()
            if current_id in ran:
                continue
            node = by_id[current_id]
            executor = get_executor(node.type)
            if executor is None:
                raise NotImplementedError(f"节点类型 {node.type!r} 暂无执行器（节点 {current_id}）")
            ctx.inputs = self._inputs_of(current_id, in_edges, produced)
            produced[current_id] = await executor(node, ctx)
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
    def _inputs_of(
        node_id: str,
        in_edges: Mapping[str, Sequence[WorkflowEdge]],
        produced: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        """按入边拼出这个节点的入口值：键 = 目标端口名，值 = 上游对应端口的产出。

        * 控制流端口（``trigger``）不送值；
        * 上游**没执行**（孤儿 / 从 start 到不了的节点）连出来的边**不算数**：这个键干脆
          不放进 ``inputs``，让 :func:`~nacho.workflow.nodes.base.input_value` 回落到 config
          里手填的值 —— 与校验器「孤儿连出来的线不算数」是同一口径（见 ``validator``）；
        * 上游跑了、只是那个端口没产出（例如 start 的时间触发没有 message）才送空串 ——
          节点拿到的是「这个入口确实接了线，只是线上没值」。
        """
        inputs: dict[str, Any] = {}
        for edge in in_edges.get(node_id, ()):
            if edge.source not in produced:  # 上游没执行：这根线不算数，别拿空串顶掉手填值
                continue
            target_port = edge_target_port(edge)
            if target_port == DEFAULT_EDGE_PORT:
                continue
            values = produced[edge.source] or {}  # 执行函数返回 None 时按「没有产出」处理
            inputs[target_port] = values.get(edge_source_port(edge), "")
        return inputs
