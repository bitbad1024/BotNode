"""工作流运行器：把一张已校验通过的图按拓扑顺序跑起来，值沿边流。

**节点执行函数不在这里**：一类节点一个文件，都在 :mod:`nacho.workflow.nodes`。本模块只管
「怎么按顺序跑、值怎么沿边流」：只跑 **start 可达的主流程节点**（孤儿永不执行），按入边把
上游产出投递到入口、按输出端口名记下产出，**分流节点按选中出口剪枝**并级联下游，同步执行
（不并发）。**业务失败停止向下传播**：节点抛 ``NodeFailure``（算不出来 / 对方回了错这类
「没做成」）时本节点不产出、出边置死，下游整段跳过，别的分支照跑；环境问题抛普通异常才
中断整条。触发是**开始节点**自己的事（``trigger=time`` 登记到调度器）。图算法在
:mod:`nacho.workflow.graph`。运行语义的完整清单见 ``docs/workflow/workflow.md`` 第 7.3 节。

这里只再导出 ``SimpleWorkflowRunner`` / ``NodeExecutionContext`` / ``get_executor`` 三个：
老代码 ``from nacho.workflow.executor import ...`` 还能用，新代码直接从 :mod:`nacho.workflow` 取。
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
from .nodes import NodeExecutionContext, get_executor, get_spec
from .nodes.base import NodeFailure

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
    没有执行器也不影响主流程）。分流节点（``spec.branching``，如 ``condition``）执行后按
    「选中出口」剪枝：没走的出口出边置死，入边全死的节点整段跳过（级联），汇合点只要有
    一条活入边就照常执行。
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
        #: 结构入度快照（拓扑推进会改 in_degree；「这个节点原本有没有入边」以它为准）
        in_total = dict(in_degree)
        #: 还「活」的入边条数：分流剪枝减它；原本有入边、减到 0 => 这个节点整段跳过
        live_in: dict[str, int] = dict(in_degree)
        ran: set[str] = set()
        #: 被剪枝跳过的节点：不执行，出边同样置死（级联到它的下游）
        skipped: set[str] = set()
        #: 节点 ID -> 它被跳过的**原因**（上游失败还是分支未选中），skip 日志用
        skip_reasons: dict[str, str] = {}
        #: 节点 ID -> 它的产出（键 = 输出端口名）；下游按边从这里取
        produced: dict[str, dict[str, Any]] = {}
        while queue:
            current_id = queue.popleft()
            if current_id in ran or current_id in skipped:
                continue
            if in_total[current_id] > 0 and live_in[current_id] == 0:
                # 入边全被剪死（分流节点没走这边 / 上游业务失败）：整段跳过，跳过也留痕可查
                skipped.add(current_id)
                reason = skip_reasons.get(current_id, "分支未选中")
                ctx.log.append(f"[skip] {current_id}: {reason}，未执行")
                self._release(
                    current_id,
                    out_edges,
                    runnable,
                    in_degree,
                    live_in,
                    queue,
                    dead=True,
                    skip_reasons=skip_reasons,
                    reason=reason,
                )
                continue
            node = by_id[current_id]
            executor = get_executor(node.type)
            if executor is None:
                raise NotImplementedError(f"节点类型 {node.type!r} 暂无执行器（节点 {current_id}）")
            ctx.inputs = self._inputs_of(current_id, in_edges, produced)
            try:
                output = await executor(node, ctx)
            except NodeFailure as exc:
                # 业务失败：不算事故，只**停止向下传播** —— 本节点不产出值（下游取不到，
                # 回落到手填值），出边全部置死让下游整段跳过；别的分支照常跑
                ctx.logger.warning(f"[{node.type}:{current_id}] {exc}")
                ctx.log.append(f"[failed] {current_id}: {exc}")
                ran.add(current_id)
                self._release(
                    current_id,
                    out_edges,
                    runnable,
                    in_degree,
                    live_in,
                    queue,
                    dead=True,
                    skip_reasons=skip_reasons,
                    reason=f"上游 {node.type}:{current_id} 失败",
                )
                continue
            produced[current_id] = output
            ran.add(current_id)
            spec = get_spec(node.type)
            if spec is not None and spec.branching:
                # 分流节点：只让**选中出口**（返回值里给了真值的输出端口）的边活着，
                # 其余出口的出边剪死 —— condition 只返回走的那一边
                taken = {name for name, value in (output or {}).items() if value}
                for edge in graph.edges:
                    if edge.source != current_id or edge_source_port(edge) in taken:
                        continue
                    if edge.target in runnable:
                        live_in[edge.target] -= 1
            self._release(current_id, out_edges, runnable, in_degree, live_in, queue, dead=False)

        if len(ran) + len(skipped) != len(runnable):
            missing = [nid for nid in runnable if nid not in ran and nid not in skipped]
            raise RuntimeError(f"工作流执行未完成，剩余节点：{missing}")

    @staticmethod
    def _release(
        node_id: str,
        out_edges: Mapping[str, Sequence[str]],
        runnable: set[str],
        in_degree: dict[str, int],
        live_in: dict[str, int],
        queue: deque[str],
        *,
        dead: bool,
        skip_reasons: dict[str, str] | None = None,
        reason: str = "",
    ) -> None:
        """节点处理完（执行 / 跳过）后放行下游：结构入度减 1，减到 0 的进拓扑队列。

        ``dead=True``（整个节点被剪枝跳过 / 上游业务失败）时出边全部置死：下游的「活入边」
        跟着减，死路就这样一级一级传下去，直到和别的活分支汇合。``reason`` 会顺着死路
        往下传，所以下下游的 ``[skip]`` 也写得出是被谁带停的。
        """
        for target in out_edges[node_id]:
            if target not in runnable:
                continue
            if dead:
                live_in[target] -= 1
                if skip_reasons is not None and reason:
                    skip_reasons[target] = reason
            in_degree[target] -= 1
            if in_degree[target] == 0:
                queue.append(target)

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
