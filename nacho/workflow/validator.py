"""工作流 JSON 入库前的校验流水线：结构 → 拓扑 → 语义（→ 可选 Dry Run）。

一句话：**结构对不对 → 走不走得通 → 跑不跑得动 → 试不试一遍 → 入库**。

前三个阶段由 :func:`validate_graph` 同步跑完，阶段间**短路**：前一阶段没过，后面不跑
（结构都不对，拓扑 / 语义无从谈起）。第四阶段 Dry Run（mock 输入走执行引擎）是
:class:`DryRunner` 协议位，执行引擎就位后由 :func:`validate_with_dry_run` 挂上。

错误收集口径：一个阶段内把错误**收齐**再返回（前端一次性把所有红点画出来，而不是
挤牙膏），所以阶段内部的检查不互相打断；阶段之间才短路。
"""
from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable
from difflib import get_close_matches
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from .models import (
    STAGE_DRY_RUN,
    STAGE_SEMANTIC,
    STAGE_STRUCTURE,
    STAGE_TOPOLOGY,
    ValidationIssue,
    ValidationReport,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
)

#: 合法节点类型（与 models.NodeType 保持一致；这里用 frozenset 做成员判断）
NODE_TYPES: frozenset[str] = frozenset(
    {"start", "end", "gateway", "approval", "expression", "http", "condition", "task",
     "log", "test"}
)

#: start 节点的触发方式：time = cron 定时触发（需配 cron）；message = 消息触发（无需配置）
START_TRIGGERS: frozenset[str] = frozenset({"time", "message"})

#: 各类型节点必须在 config 里给出的字段：(字段, 给人看的字段名)
#: start 的 cron 是**条件必填**（仅 trigger=time 时），见 :func:`_type_specific`
REQUIRED_CONFIG: dict[str, tuple[tuple[str, str], ...]] = {
    "expression": (("expression", "表达式"),),
    "http": (("url", "请求地址"), ("method", "请求方法")),
    "approval": (("assignee", "审批人"),),
    "condition": (("condition", "条件"),),
    "log": (("message", "日志内容"),),
}

#: 合法日志级别（log 节点 config.level 可选，缺省 INFO）
LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

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


@runtime_checkable
class DryRunner(Protocol):
    """Dry Run（④）的协议位：拿 mock 输入走一遍执行引擎，不调外部服务、不落业务库。

    执行引擎就位后实现它；跑出来的问题（类型错误 / 空值 / 死分支 / 运行时异常）
    以 :class:`ValidationIssue` 列表返回，空列表 = 试跑通过。
    """

    async def run(self, graph: WorkflowGraph, mock_inputs: dict[str, Any]) -> list[ValidationIssue]:
        ...


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

    topology_errors = _topology_stage(graph)
    if topology_errors:
        return ValidationReport.reject(STAGE_TOPOLOGY, topology_errors)

    checker: ExpressionSyntaxChecker = (
        expression_checker if expression_checker is not None else AcceptAllExpressions()
    )
    semantic_errors = _semantic_stage(graph, checker)
    if semantic_errors:
        return ValidationReport.reject(STAGE_SEMANTIC, semantic_errors)

    return ValidationReport.ok()


async def validate_with_dry_run(
    raw: dict[str, Any] | WorkflowGraph,
    runner: DryRunner,
    *,
    mock_inputs: dict[str, Any] | None = None,
    expression_checker: ExpressionSyntaxChecker | None = None,
) -> ValidationReport:
    """前三阶段 + ④ Dry Run：前三阶段过了才试跑（短路同前）。"""
    report = validate_graph(raw, expression_checker=expression_checker)
    if not report.valid:
        return report
    graph = raw if isinstance(raw, WorkflowGraph) else WorkflowGraph.model_validate(raw)
    issues = await runner.run(graph, mock_inputs or {})
    if issues:
        return ValidationReport.reject(STAGE_DRY_RUN, issues)
    return ValidationReport.ok()


# --------------------------------------------------------------------------- ① 结构
def _structure_stage(raw: dict[str, Any] | WorkflowGraph) -> tuple[WorkflowGraph | None, list[ValidationIssue]]:
    """结构阶段：必填字段 / 节点 ID 唯一 / type 合法 / 边的端点存在。

    返回 ``(解析出的图, 错误)``：有错时图是 None。pydantic 的字段级错误在这里翻译成
    业务错误码，不把库的错误原文漏给前端。
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
        if node.type not in NODE_TYPES:
            errors.append(
                ValidationIssue(
                    node_id=node.id,
                    code="UNKNOWN_NODE_TYPE",
                    message=f"节点 {node.id} 的类型 {node.type!r} 不合法",
                    suggestion=f"合法类型：{', '.join(sorted(NODE_TYPES))}",
                )
            )

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
def _topology_stage(graph: WorkflowGraph) -> list[ValidationIssue]:
    """拓扑阶段：DAG 无环 / 无孤儿 / start 唯一 / end≥1 / gateway 出边≥2 / 不自环。"""
    errors: list[ValidationIssue] = []
    by_id: dict[str, WorkflowNode] = {node.id: node for node in graph.nodes}

    starts = [node.id for node in graph.nodes if node.type == "start"]
    ends = [node.id for node in graph.nodes if node.type == "end"]
    if len(starts) != 1:
        errors.append(
            ValidationIssue(
                code="START_NOT_UNIQUE",
                message=f"start 节点必须有且仅有 1 个，当前有 {len(starts)} 个",
                suggestion="保留一个 start 作为唯一入口",
            )
        )
    if not ends:
        errors.append(
            ValidationIssue(
                code="END_MISSING",
                message="图中没有 end 节点（至少 1 个）",
                suggestion="给每条分支一个 end 出口",
            )
        )

    out_edges: dict[str, list[WorkflowEdge]] = {node.id: [] for node in graph.nodes}
    in_degree: dict[str, int] = {node.id: 0 for node in graph.nodes}
    for edge in graph.edges:
        out_edges[edge.source].append(edge)
        in_degree[edge.target] += 1
        if edge.source == edge.target:
            errors.append(
                ValidationIssue(
                    node_id=edge.source,
                    code="SELF_LOOP",
                    message=f"节点 {edge.source} 存在指向自己的边",
                    suggestion="去掉自环（审批节点也不能自己连自己）",
                )
            )

    # gateway 出边 ≥ 2（没分支就没法分流）
    for node in graph.nodes:
        if node.type == "gateway" and len(out_edges[node.id]) < 2:
            errors.append(
                ValidationIssue(
                    node_id=node.id,
                    code="GATEWAY_NEEDS_BRANCHES",
                    message=f"网关节点 {node.id} 至少要有 2 条出边，当前 {len(out_edges[node.id])} 条",
                    suggestion="给每个分支各接一条出边",
                )
            )

    # Kahn 拓扑排序：能取完 = DAG；取不完 = 剩下的全在环上
    remaining = deque(node.id for node in graph.nodes if in_degree[node.id] == 0)
    visited = 0
    degrees = dict(in_degree)
    while remaining:
        current = remaining.popleft()
        visited += 1
        for edge in out_edges[current]:
            degrees[edge.target] -= 1
            if degrees[edge.target] == 0:
                remaining.append(edge.target)
    if visited != len(graph.nodes):
        cyclic = sorted(node_id for node_id, degree in degrees.items() if degree > 0)
        errors.append(
            ValidationIssue(
                node_id=cyclic[0] if cyclic else "",
                code="CYCLE_DETECTED",
                message=f"图中存在环路，涉及节点：{', '.join(cyclic)}",
                suggestion="工作流必须是 DAG：断开回边或改用循环节点表达重复执行",
            )
        )

    # 孤儿：从唯一 start 沿出边不可达的节点（start 不唯一时这步没法判定，前面已报错）
    if len(starts) == 1:
        reachable: set[str] = set()
        queue: deque[str] = deque([starts[0]])
        while queue:
            current = queue.popleft()
            if current in reachable:
                continue
            reachable.add(current)
            for edge in out_edges[current]:
                if edge.target not in reachable:
                    queue.append(edge.target)
        for node in graph.nodes:
            if node.id not in reachable:
                errors.append(
                    ValidationIssue(
                        node_id=node.id,
                        code="ORPHAN_NODE",
                        message=f"节点 {node.id} 从 start 不可达（孤儿节点）",
                        suggestion="把它接进主流程，或删掉无用节点",
                    )
                )

    # end 节点不该再有出边（有出口的 end 语义上不通）
    for end_id in ends:
        if out_edges[end_id]:
            errors.append(
                ValidationIssue(
                    node_id=end_id,
                    code="END_HAS_OUTGOING",
                    message=f"end 节点 {end_id} 不允许有出边",
                    suggestion="end 是终点，删掉它后面的边",
                )
            )
    return errors


# --------------------------------------------------------------------------- ③ 语义
def _semantic_stage(graph: WorkflowGraph, checker: ExpressionSyntaxChecker) -> list[ValidationIssue]:
    """语义阶段：节点配置完整性 → 变量作用域 → 表达式语法 → 类型专属校验。"""
    errors: list[ValidationIssue] = []
    by_id: dict[str, WorkflowNode] = {node.id: node for node in graph.nodes}
    errors.extend(_config_completeness(graph))
    errors.extend(_type_specific(graph))
    errors.extend(_variable_scope(graph, by_id))
    errors.extend(_expression_syntax(graph, checker))
    return errors


def _type_specific(graph: WorkflowGraph) -> list[ValidationIssue]:
    """③-D 类型专属配置校验：start 的触发方式与 cron、log 的 level 合法性。"""
    from nacho.core.scheduler import CronExpr, CronError  # 局部导入避免循环依赖

    issues: list[ValidationIssue] = []
    for node in graph.nodes:
        if node.type == "start":
            issues.extend(_validate_start_trigger(node, CronExpr, CronError))
        elif node.type == "log":
            level = node.config.get("level")
            if level is not None and (not isinstance(level, str) or level.upper() not in LOG_LEVELS):
                issues.append(
                    ValidationIssue(
                        node_id=node.id,
                        code="INVALID_LOG_LEVEL",
                        message=f"log 节点 {node.id} 的级别 {level!r} 不合法",
                        suggestion=f"可选级别：{', '.join(sorted(LOG_LEVELS))}（缺省 INFO）",
                    )
                )
    return issues


def _validate_start_trigger(
    node: WorkflowNode, cron_expr_cls: type, cron_error_cls: type
) -> list[ValidationIssue]:
    """start 节点：config.trigger 必须是 time/message（缺省按 message）；time 时 cron 必填且合法。"""
    trigger = node.config.get("trigger", "message")
    if not isinstance(trigger, str) or trigger not in START_TRIGGERS:
        return [
            ValidationIssue(
                node_id=node.id,
                code="INVALID_TRIGGER",
                message=f"start 节点 {node.id} 的触发方式 {trigger!r} 不合法",
                suggestion="config.trigger 只能是 time（定时）或 message（消息）",
            )
        ]
    if trigger != "time":
        return []

    cron = node.config.get("cron")
    if not isinstance(cron, str) or not cron.strip():
        return [
            ValidationIssue(
                node_id=node.id,
                code="MISSING_CONFIG",
                message=f"start 节点 {node.id} 选择了时间触发，但缺少必填配置 cron（cron 表达式）",
                suggestion="在 config.cron 中补充 5/6 段 cron 表达式，如 */5 * * * *",
            )
        ]
    try:
        cron_expr_cls.parse(cron.strip())
    except cron_error_cls as exc:
        return [
            ValidationIssue(
                node_id=node.id,
                code="INVALID_CRON",
                message=f"start 节点 {node.id} 的 cron 表达式不合法：{exc}",
                suggestion="cron 用 5 段（分 时 日 月 周）或 6 段（秒 分 时 日 月 周），如 */5 * * * *",
            )
        ]
    return []


def _config_completeness(graph: WorkflowGraph) -> list[ValidationIssue]:
    """③-C 各类型节点的必填 config 字段（非空字符串）。"""
    issues: list[ValidationIssue] = []
    for node in graph.nodes:
        for field, label in REQUIRED_CONFIG.get(node.type, ()):
            value = node.config.get(field)
            if not isinstance(value, str) or not value.strip():
                issues.append(
                    ValidationIssue(
                        node_id=node.id,
                        code="MISSING_CONFIG",
                        message=f"{node.type} 节点 {node.id} 缺少必填配置 {field}（{label}）",
                        suggestion=f"在 config.{field} 中补充{label}",
                    )
                )
    return issues


def _ancestors_of(graph: WorkflowGraph) -> dict[str, frozenset[str]]:
    """算每个节点的**前置节点集合**：沿边能走到它的所有节点（变量作用域的来源）。

    拓扑阶段已保证无环，这里直接记忆化 DFS 反图。
    """
    reverse: dict[str, list[str]] = {node.id: [] for node in graph.nodes}
    for edge in graph.edges:
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

    for node in graph.nodes:
        _ = resolve(node.id, set())
    return ancestors


def _variable_scope(
    graph: WorkflowGraph, by_id: dict[str, WorkflowNode]
) -> list[ValidationIssue]:
    """③-A 变量作用域：配置里 ``{{x}}`` 引用的 x 必须是某个**前置节点**声明的输出。

    拼错名字时用 difflib 在全图已声明变量里找最像的，给「是否想用」建议。
    """
    ancestors = _ancestors_of(graph)
    declared_by: dict[str, str] = {}  # 变量名 -> 声明它的节点 ID（全图）
    for node in graph.nodes:
        for name in node.outputs:
            declared_by.setdefault(name, node.id)

    issues: list[ValidationIssue] = []
    for node in graph.nodes:
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


def _expression_syntax(graph: WorkflowGraph, checker: ExpressionSyntaxChecker) -> list[ValidationIssue]:
    """③-B 表达式语法：交给可替换的检查器（默认占位全放行，见模块文档）。"""
    issues: list[ValidationIssue] = []
    for node in graph.nodes:
        if node.type != "expression":
            continue
        expression = node.config.get("expression")
        if isinstance(expression, str) and expression.strip():
            issue = checker.check(node.id, expression)
            if issue is not None:
                issues.append(issue)
    return issues
