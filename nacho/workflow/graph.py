"""图的小工具：出边索引 / 可达集合 / 入口节点 —— **校验器与运行器共用同一份口径**。

放在这里是为了让「哪些节点算主流程」只剩一个说法：校验器拿它决定该查谁、该跳谁（孤儿不查），
运行器拿它决定该跑谁（孤儿不跑）。以前这两处各写了一份 BFS，连「找入口」也是两份 —— 改一处
忘一处就会出现「校验说没问题、跑起来却不执行」这种偏差。
"""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence

from .models import WorkflowGraph, WorkflowNode
from .nodes.registry import get_spec


def out_targets(graph: WorkflowGraph) -> dict[str, list[str]]:
    """节点 ID -> 出边指向的**目标节点 ID** 列表（结构阶段已保证端点存在）。"""
    out_edges: dict[str, list[str]] = {node.id: [] for node in graph.nodes}
    for edge in graph.edges:
        out_edges[edge.source].append(edge.target)
    return out_edges


def reachable_from(out_edges: Mapping[str, Sequence[str]], start_id: str) -> set[str]:
    """从 ``start_id`` 沿出边能到达的全部节点（含自己）。"""
    reachable: set[str] = set()
    queue: deque[str] = deque([start_id])
    while queue:
        current = queue.popleft()
        if current in reachable:
            continue
        reachable.add(current)
        for target in out_edges.get(current, ()):  # .get 只是防御：结构阶段已保证端点存在
            if target not in reachable:
                queue.append(target)
    return reachable


def start_ids(nodes: Sequence[WorkflowNode]) -> list[str]:
    """注册角色为 ``start`` 的节点 ID（按节点出现顺序）。"""
    starts: list[str] = []
    for node in nodes:
        spec = get_spec(node.type)
        if spec is not None and spec.role == "start":
            starts.append(node.id)
    return starts


def entry_id(graph: WorkflowGraph) -> str | None:
    """入口节点：注册角色为 start 的那个。

    图里没有 start 角色时（直接构造的测试图、最早期快照）退回字面量 ``type == "start"``
    的旧写法，**再没有**才是 ``None`` —— 此时运行器退回「跑全部节点」，沿用历史行为。
    """
    starts = start_ids(graph.nodes)
    if starts:
        return starts[0]
    for node in graph.nodes:
        if node.type == "start":
            return node.id
    return None
