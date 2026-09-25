"""工作流 JSON 入库前的校验流水线：结构 → 拓扑 → 语义（→ 将来还有 Dry Run）。

一句话：**结构对不对 → 走不走得通 → 跑不跑得动 →（试不试一遍）→ 入库**。

三个阶段由 :func:`validate_graph` 同步跑完，阶段间**短路**：前一阶段没过，后面不跑
（结构都不对，拓扑 / 语义无从谈起）。第四阶段 Dry Run（mock 输入走一遍执行引擎）目前
只留了阶段名常量 ``STAGE_DRY_RUN``，协议与实现都还没接 —— 执行引擎就位后再加。

错误收集口径：一个阶段内把错误**收齐**再返回（前端一次性把所有红点画出来，而不是
挤牙膏），所以阶段内部的检查不互相打断；阶段之间才短路。

校验规则**全部从节点注册表推导**（见 :mod:`nacho.workflow.nodes.registry`）：认不认识
某个类型、哪些 config 必填、缺省填什么、有什么专属约束，都在节点注册时声明，新增节点
类型不需要改本文件。

**孤儿节点允许存在**：校验器只保证「start 沿出边能正常拓扑展开到 end」。从 start
不可达的节点（没接进主流程的散点、独立小图、哪怕里面有环）一律不产生任何错误，也不
参与语义检查；运行器同样只跑 start 可达的节点，孤儿永远不影响主流程。
"""
from __future__ import annotations

import re
from collections import deque
from difflib import get_close_matches
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from .graph import out_targets, reachable_from, start_ids
from .models import (
    STAGE_SEMANTIC,
    STAGE_STRUCTURE,
    STAGE_TOPOLOGY,
    ValidationIssue,
    ValidationReport,
    WorkflowGraph,
    WorkflowNode,
)
from .nodes.base import MISSING_DEFAULT
from .nodes.registry import get_spec, registered_types

#: 配置字符串里的变量引用：{{ name }}
_VARIABLE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

# --------------------------------------------------------------------------- 表达式引擎位
@runtime_checkable
class ExpressionSyntaxChecker(Protocol):
    """表达式语法检查器（**只 parse 不执行**）的协议位。

    默认实现 :class:`AcceptAllExpressions` 全放行——执行引擎 / 表达式库还没接，
    先把阶段与错误结构定下来；接入时传一个「parse 失败就返回错误描述」的实现即可。
    """

    def check(self, node_id: str, expression: str) -> ValidationIssue | None:
        """语法树构建成功返回 None；失败返回一条语义错误（code=EXPRESSION_SYNTAX）。"""
        ...


class AcceptAllExpressions:
    """默认表达式检查器：不执行任何语法判断（执行引擎就位前的占位）。"""

    def check(self, node_id: str, expression: str) -> ValidationIssue | None:
        return None


# --------------------------------------------------------------------------- 入口
def validate_graph(
    raw: dict[str, Any] | WorkflowGraph,
    *,
    expression_checker: ExpressionSyntaxChecker | None = None,
) -> ValidationReport:
    """跑前三阶段（结构 / 拓扑 / 语义），返回完整报告；不抛「图内容」类异常。

    :param raw: 前端提交的图 dict（或已是 :class:`WorkflowGraph`——那时结构阶段照跑一遍
        不变量检查，只是不会有解析错误）；
    :param expression_checker: 表达式语法检查器，默认全放行（占位，见类文档）。
    """
    graph, structure_errors = _structure_stage(raw)
    if structure_errors:
        return ValidationReport.reject(STAGE_STRUCTURE, structure_errors)

    # 默认值字段先补到解析出的图上再进拓扑 / 语义：节点的自定义校验器看到的是补全后的
    # config（缺省值不需要每个校验器自己猜）。这里只动本轮解析出的模型，不回写入参 dict；
    # 保存版本时 router 会再幂等补一遍并落库。
    graph = apply_config_defaults(graph)

    topology_errors, reachable = _topology_stage(graph)
    if topology_errors:
        return ValidationReport.reject(STAGE_TOPOLOGY, topology_errors)

    checker: ExpressionSyntaxChecker = (
        expression_checker if expression_checker is not None else AcceptAllExpressions()
    )
    semantic_errors = _semantic_stage(graph, reachable, checker)
    if semantic_errors:
        return ValidationReport.reject(STAGE_SEMANTIC, semantic_errors)

    return ValidationReport.ok()


def apply_config_defaults(raw: dict[str, Any] | WorkflowGraph) -> WorkflowGraph:
    """按各类型注册的默认值字段补全 config，返回补全后的图（原 dict 不被修改）。

    在**保存版本**时、校验通过之后调用：键缺失或值为 None 的默认值字段用注册时声明的
    默认值代替（如 start.trigger=message、log.level=INFO、http.timeout=10），
    让落库快照配置完整、运行期不必再猜缺省。
    """
    graph = raw if isinstance(raw, WorkflowGraph) else WorkflowGraph.model_validate(raw)
    for node in graph.nodes:
        spec = get_spec(node.type)
        if spec is None:
            continue
        for field in spec.fields:
            if field.default is not MISSING_DEFAULT and node.config.get(field.name) is None:
                node.config[field.name] = field.default
    return graph


# --------------------------------------------------------------------------- ① 结构
def _structure_stage(raw: dict[str, Any] | WorkflowGraph) -> tuple[WorkflowGraph | None, list[ValidationIssue]]:
    """结构阶段：必填字段 / 节点 ID 唯一 / 边的端点存在 / 可达节点类型已注册。

    返回 ``(解析出的图, 错误)``：有错时图是 None。pydantic 的字段级错误在这里翻译成
    业务错误码，不把库的错误原文漏给前端。

    类型是否合法查注册表；**孤儿节点的类型不查**——没接进主流程的节点允许保存。
    """
    if isinstance(raw, WorkflowGraph):
        graph, errors = raw, []
    else:
        graph, errors = _parse_graph(raw)
        if errors:
            return None, errors

    seen: set[str] = set()
    for node in graph.nodes:
        if not node.id.strip():
            errors.append(
                ValidationIssue(
                    code="EMPTY_NODE_ID",
                    message="存在空的节点 ID",
                    suggestion="给每个节点一个非空 ID",
                )
            )
        elif node.id in seen:
            errors.append(
                ValidationIssue(
                    node_id=node.id,
                    code="DUPLICATE_NODE_ID",
                    message=f"节点 ID {node.id} 重复",
                    suggestion="节点 ID 必须在一张图内全局唯一",
                )
            )
        else:
            seen.add(node.id)

    for index, edge in enumerate(graph.edges):
        if edge.source not in seen:
            errors.append(
                ValidationIssue(
                    node_id=edge.source,
                    code="EDGE_ENDPOINT_MISSING",
                    message=f"第 {index + 1} 条边的起点 {edge.source!r} 不存在",
                    suggestion="边的 source 必须指向一个存在的节点 ID",
                )
            )
        if edge.target not in seen:
            errors.append(
                ValidationIssue(
                    node_id=edge.target,
                    code="EDGE_ENDPOINT_MISSING",
                    message=f"第 {index + 1} 条边的终点 {edge.target!r} 不存在",
                    suggestion="边的 target 必须指向一个存在的节点 ID",
                )
            )
    if errors:
        return None, errors

    # 边端点都有效后算可达域：唯一 start 时只查主流程上的类型；start 数量异常（0 或多个）
    # 时没法界定主流程，退回查全部节点——拓扑阶段随后会报 START_NOT_UNIQUE
    starts = start_ids(graph.nodes)
    if len(starts) == 1:
        scope = reachable_from(out_targets(graph), starts[0])
    else:
        scope = set(seen)
    for node in graph.nodes:
        if node.id in scope and get_spec(node.type) is None:
            errors.append(
                ValidationIssue(
                    node_id=node.id,
                    code="UNKNOWN_NODE_TYPE",
                    message=f"节点 {node.id} 的类型 {node.type!r} 未注册",
                    suggestion=f"已注册类型：{', '.join(registered_types())}",
                )
            )
    return (graph if not errors else None), errors


def _parse_graph(raw: Any) -> tuple[WorkflowGraph | None, list[ValidationIssue]]:
    """把任意 JSON 入参解析成图；解析失败翻译成结构错误（而非库异常）。"""
    if not isinstance(raw, dict):
        return None, [
            ValidationIssue(
                code="GRAPH_NOT_OBJECT",
                message="工作流定义必须是一个对象，包含 nodes 与 edges",
                suggestion="检查提交的 JSON 顶层结构",
            )
        ]
    try:
        return WorkflowGraph.model_validate(raw), []
    except ValidationError as exc:
        issues: list[ValidationIssue] = []
        for err in exc.errors():
            loc = ".".join(str(part) for part in err.get("loc", ()))
            issues.append(
                ValidationIssue(
                    node_id=_node_id_of_loc(loc, raw),
                    code="INVALID_GRAPH_SCHEMA",
                    message=f"字段 {loc or '<根>'} 不合法：{err.get('msg', '类型错误')}",
                    suggestion="检查节点 id / type 与 edges 的 source / target 是否齐全",
                )
            )
        return None, issues


def _node_id_of_loc(loc: str, raw: dict[str, Any]) -> str:
    """从 pydantic 错误定位（如 ``nodes.2.id``）里尽量抠出节点 ID，抠不到就空串。"""
    parts = loc.split(".")
    if len(parts) >= 2 and parts[0] == "nodes":
        try:
            node = raw["nodes"][int(parts[1])]
            if isinstance(node, dict):
                return str(node.get("id", ""))
        except (KeyError, IndexError, TypeError, ValueError):
            return ""
    return ""


# --------------------------------------------------------------------------- ② 拓扑
def _topology_stage(
    graph: WorkflowGraph,
) -> tuple[list[ValidationIssue], set[str]]:
    """拓扑阶段（只看 start 可达的主流程）：start 唯一 / end≥1 可达 / 无环 / 注册的出入边约束。

    返回 ``(错误, start 可达节点集合)``：语义阶段只检查可达集合里的节点。
    孤儿节点（不可达）不产生任何错误——它们不会被执行，允许先画在画布上保存。
    """
    errors: list[ValidationIssue] = []
    out_edges = out_targets(graph)

    starts = start_ids(graph.nodes)
    if len(starts) != 1:
        errors.append(
            ValidationIssue(
                code="START_NOT_UNIQUE",
                message=f"start 节点必须有且仅有 1 个，当前有 {len(starts)} 个",
                suggestion="保留一个 start 作为唯一入口",
            )
        )
        # 入口不唯一时主流程无从界定，后续检查不跑（可达域给空集，语义阶段也不会误报）
        return errors, set()

    start_id = starts[0]
    reachable = reachable_from(out_edges, start_id)

    # 主流程上至少一个 end 角色节点
    if not any(
        (spec := get_spec(node.type)) is not None and spec.role == "end"
        for node in graph.nodes
        if node.id in reachable
    ):
        errors.append(
            ValidationIssue(
                code="END_MISSING",
                message="从 start 可达的路径上没有 end 节点（至少 1 个）",
                suggestion="给主流程接一个 end 出口（没接进来的 end 不算）",
            )
        )

    # 主流程内的入度（只计可达边）与自环
    in_degree: dict[str, int] = {node_id: 0 for node_id in reachable}
    for node_id in reachable:
        for target in out_edges[node_id]:
            if target == node_id:
                errors.append(
                    ValidationIssue(
                        node_id=node_id,
                        code="SELF_LOOP",
                        message=f"节点 {node_id} 存在指向自己的边",
                        suggestion="去掉自环（审批节点也不能自己连自己）",
                    )
                )
            if target in in_degree:
                in_degree[target] += 1

    # 各类型注册的出入边条数约束（分流类节点至少 2 条出边、end 不许有出边……）
    for node in graph.nodes:
        if node.id not in reachable:
            continue
        spec = get_spec(node.type)
        if spec is None:  # 结构阶段已拦，防御性跳过
            continue
        outgoing = out_edges[node.id]
        if len(outgoing) < spec.min_outgoing:
            errors.append(
                ValidationIssue(
                    node_id=node.id,
                    code="GATEWAY_NEEDS_BRANCHES",
                    message=(
                        f"节点 {node.id}（{node.type}）至少要有 {spec.min_outgoing} 条出边，"
                        f"当前 {len(outgoing)} 条"
                    ),
                    suggestion="给每个分支各接一条出边",
                )
            )
        if spec.max_outgoing is not None and len(outgoing) > spec.max_outgoing:
            errors.append(
                ValidationIssue(
                    node_id=node.id,
                    code="END_HAS_OUTGOING",
                    message=f"节点 {node.id}（{node.type}）最多允许 {spec.max_outgoing} 条出边",
                    suggestion="该类型是终点语义，删掉它后面的边",
                )
            )

    # Kahn 拓扑排序（仅可达子图）：能取完 = 主流程是 DAG；取不完 = 剩下的全在环上。
    # 孤儿组件里就算有环也不在可达子图内——永不执行，不拦。
    remaining = deque(node_id for node_id in reachable if in_degree[node_id] == 0)
    visited = 0
    degrees = dict(in_degree)
    while remaining:
        current = remaining.popleft()
        visited += 1
        for target in out_edges[current]:
            if target not in degrees:
                continue
            degrees[target] -= 1
            if degrees[target] == 0:
                remaining.append(target)
    if visited != len(reachable):
        cyclic = sorted(node_id for node_id, degree in degrees.items() if degree > 0)
        errors.append(
            ValidationIssue(
                node_id=cyclic[0] if cyclic else start_id,
                code="CYCLE_DETECTED",
                message=f"主流程存在环路，涉及节点：{', '.join(cyclic)}",
                suggestion="工作流必须是 DAG：断开回边或改用循环节点表达重复执行",
            )
        )
    return errors, reachable


# --------------------------------------------------------------------------- ③ 语义
def _semantic_stage(
    graph: WorkflowGraph,
    reachable: set[str],
    checker: ExpressionSyntaxChecker,
) -> list[ValidationIssue]:
    """语义阶段（只看主流程可达节点）：注册字段必填 → 节点自注册校验 → 表达式语法 → 变量作用域。"""
    errors: list[ValidationIssue] = []
    by_id: dict[str, WorkflowNode] = {node.id: node for node in graph.nodes}
    errors.extend(_config_completeness(graph, reachable))
    errors.extend(_node_validators(graph, reachable))
    errors.extend(_expression_syntax(graph, reachable, checker))
    errors.extend(_variable_scope(graph, reachable, by_id))
    return errors


def _config_completeness(graph: WorkflowGraph, reachable: set[str]) -> list[ValidationIssue]:
    """③-C 各类型注册的必填 config 字段：缺失 / None / 空白字符串都算缺。"""
    issues: list[ValidationIssue] = []
    for node in graph.nodes:
        if node.id not in reachable:
            continue
        spec = get_spec(node.type)
        if spec is None:
            continue
        for field in spec.fields:
            if not field.required:
                continue
            value = node.config.get(field.name)
            if value is None or (isinstance(value, str) and not value.strip()):
                label = f"（{field.label}）" if field.label else ""
                issues.append(
                    ValidationIssue(
                        node_id=node.id,
                        code="MISSING_CONFIG",
                        message=f"{node.type} 节点 {node.id} 缺少必填配置 {field.name}{label}",
                        suggestion=f"在 config.{field.name} 中补充{field.label or '该字段'}",
                    )
                )
    return issues


def _node_validators(graph: WorkflowGraph, reachable: set[str]) -> list[ValidationIssue]:
    """③-D 各节点在注册时挂上的自定义校验器（枚举、条件必填等）。"""
    issues: list[ValidationIssue] = []
    for node in graph.nodes:
        if node.id not in reachable:
            continue
        spec = get_spec(node.type)
        if spec is not None and spec.validator is not None:
            issues.extend(spec.validator(node))
    return issues


def _ancestors_of(
    graph: WorkflowGraph, scope: set[str]
) -> dict[str, frozenset[str]]:
    """算域内每个节点的**前置节点集合**：沿边能走到它的所有节点（变量作用域的来源）。

    拓扑阶段已保证主流程无环，这里直接记忆化 DFS 反图；孤儿不在 scope 里，直接跳过。
    """
    reverse: dict[str, list[str]] = {node_id: [] for node_id in scope}
    for edge in graph.edges:
        if edge.source in scope and edge.target in scope:
            reverse[edge.target].append(edge.source)

    ancestors: dict[str, frozenset[str]] = {}

    def resolve(node_id: str, stack: set[str]) -> frozenset[str]:
        if node_id in ancestors:
            return ancestors[node_id]
        if node_id in stack:  # 理论上拓扑阶段已拦住环，这只是防御性兜底
            return frozenset()
        stack.add(node_id)
        found: set[str] = set()
        for parent in reverse[node_id]:
            found.add(parent)
            found.update(resolve(parent, stack))
        stack.discard(node_id)
        frozen = frozenset(found)
        ancestors[node_id] = frozen
        return frozen

    for node_id in scope:
        _ = resolve(node_id, set())
    return ancestors


def _variable_scope(
    graph: WorkflowGraph,
    reachable: set[str],
    by_id: dict[str, WorkflowNode],
) -> list[ValidationIssue]:
    """③-A 变量作用域：配置里 ``{{x}}`` 引用的 x 必须是某个**前置节点**声明的输出。

    只分析主流程：孤儿节点既不产出变量给下游，也不被检查。拼错名字时用 difflib 在
    主流程已声明变量里找最像的，给「是否想用」建议。
    """
    ancestors = _ancestors_of(graph, reachable)
    declared_by: dict[str, str] = {}  # 变量名 -> 声明它的节点 ID（主流程内）
    for node in graph.nodes:
        if node.id not in reachable:
            continue
        for name in node.outputs:
            declared_by.setdefault(name, node.id)

    issues: list[ValidationIssue] = []
    for node in graph.nodes:
        if node.id not in reachable:
            continue
        for name in _references_in(node.config):
            declarer = declared_by.get(name)
            if declarer is None:
                issues.append(
                    ValidationIssue(
                        node_id=node.id,
                        code="VARIABLE_NOT_DECLARED",
                        message=f"变量 {name!r} 没有任何节点声明",
                        suggestion=_spell_hint(name, declared_by)
                        or "检查前置节点的 outputs 声明或修正拼写",
                    )
                )
            elif declarer not in ancestors[node.id]:
                issues.append(
                    ValidationIssue(
                        node_id=node.id,
                        code="VARIABLE_OUT_OF_SCOPE",
                        message=(
                            f"变量 {name!r} 由节点 {declarer} 声明，但它不在 {node.id} 的前置链路上"
                        ),
                        suggestion="变量只能沿边向下游传递：把声明节点接到本节点之前",
                    )
                )
    return issues


def _references_in(value: Any) -> list[str]:
    """递归收集任意 config 值里所有 ``{{name}}`` 引用（字符串内），保序去重。"""
    found: list[str] = []
    seen: set[str] = set()

    def walk(item: Any) -> None:
        if isinstance(item, str):
            for match in _VARIABLE_RE.finditer(item):
                name = match.group(1)
                if name not in seen:
                    seen.add(name)
                    found.append(name)
        elif isinstance(item, dict):
            for sub in item.values():
                walk(sub)
        elif isinstance(item, list):
            for sub in item:
                walk(sub)

    walk(value)
    return found


def _spell_hint(name: str, declared_by: dict[str, str]) -> str:
    """拼错检测：在已声明变量名里找最像的，找到就给一句「是否想用」。"""
    matches = get_close_matches(name, list(declared_by), n=1, cutoff=0.6)
    if not matches:
        return ""
    return f"变量 {name!r} 未定义，是否想用 {matches[0]!r}？"


def _expression_syntax(
    graph: WorkflowGraph,
    reachable: set[str],
    checker: ExpressionSyntaxChecker,
) -> list[ValidationIssue]:
    """③-B 表达式语法：节点注册了 ``expression_field`` 就把该字段交给可替换的检查器。"""
    issues: list[ValidationIssue] = []
    for node in graph.nodes:
        if node.id not in reachable:
            continue
        spec = get_spec(node.type)
        if spec is None or not spec.expression_field:
            continue
        expression = node.config.get(spec.expression_field)
        if isinstance(expression, str) and expression.strip():
            issue = checker.check(node.id, expression)
            if issue is not None:
                issues.append(issue)
    return issues
