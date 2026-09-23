"""工作流节点执行器：把一张已校验通过的图跑起来。

当前实现覆盖**基础节点**——开始（时间触发 / 消息触发）/ 结束 / 写日志 / 测试回显；
条件 / 网关 / HTTP / 审批 / 表达式等业务节点留协议位，后续逐个接。

执行模型：
* 按拓扑顺序逐个跑节点，每个节点接收上游所有节点的输出（变量上下文）；
* ``NodeExecutionContext`` 是运行时状态：变量上下文、日志收集器、调度器引用；
* 同步执行（不并发），因为单条图的节点之间有数据依赖；并行执行留给将来的 gateway fork。

**开始节点的两种触发方式**（``config.trigger``）：
* ``time``——开始节点不自己"到点执行"，它的工作是把整张流程图登记到
  :class:`~nacho.core.scheduler.TaskManager`，由调度器按 cron 触发整条流程；
* ``message``——被动等待消息触发，发布时只记日志、不登记调度器（消息源接入留待后续）。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from nacho.core.logger import BaseLogger, get_logger
from nacho.core.scheduler import TaskManager

from .models import WorkflowGraph, WorkflowNode


#: 节点执行函数：(节点, 上下文) -> 该节点的输出变量 dict
NodeExecutor = Callable[[WorkflowNode, "NodeExecutionContext"], Awaitable[dict[str, Any]]]


class NodeExecutionContext:
    """节点运行时上下文：上游输出变量 + 日志 + 调度器。

    :param variables: 累积的变量上下文（上游节点的 outputs 合并进来）；
    :param logger: 业务日志实例（log 节点写这里）；
    :param scheduler: 调度器（时间触发的开始节点把流程图登记到这里）；
    :param run: 触发整条流程的回调，cron 到点时调用。
    """

    def __init__(
        self,
        *,
        logger: BaseLogger | None = None,
        scheduler: TaskManager | None = None,
        run: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.variables: dict[str, Any] = {}
        self.log: list[str] = []  # 节点产出的文字日志（供测试 / 前端回显）
        self._logger: BaseLogger = logger if logger is not None else get_logger("workflow")
        self._scheduler: TaskManager | None = scheduler
        self._run: Callable[[], Awaitable[None]] | None = run

    @property
    def logger(self) -> BaseLogger:
        return self._logger

    @property
    def scheduler(self) -> TaskManager | None:
        return self._scheduler

    async def run_workflow(self) -> None:
        """cron 到点时触发整条流程的回调。"""
        if self._run is not None:
            await self._run()


# --------------------------------------------------------------------------- 执行器注册表
_EXECUTORS: dict[str, NodeExecutor] = {}


def register_executor(node_type: str, executor: NodeExecutor) -> None:
    """注册某类型节点的执行函数；重复注册覆盖（方便测试换实现）。"""
    _EXECUTORS[node_type] = executor


def get_executor(node_type: str) -> NodeExecutor | None:
    """取某类型节点的执行函数；没注册返回 None（由主流程报不支持）。"""
    return _EXECUTORS.get(node_type)


# --------------------------------------------------------------------------- 基础节点实现
async def exec_start(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """开始节点：按 ``config.trigger`` 分流。

    * ``time``（时间触发）：把整条流程按 cron 登记到调度器（见 :func:`_register_cron`）；
    * ``message``（消息触发，缺省）：被动等待消息，发布/试跑时只写一条开始日志。
    """
    trigger = str(node.config.get("trigger", "message"))
    if trigger == "time":
        return await _register_cron(node, ctx)

    ctx.log.append(f"[start] {node.id} 流程开始（消息触发）")
    ctx.logger.info("工作流开始（消息触发，等待消息进入）", node_id=node.id)
    return {}


async def exec_end(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """结束节点：写一条完成日志，不产出新变量。"""
    ctx.log.append(f"[end] {node.id} 流程结束")
    ctx.logger.info("工作流结束", node_id=node.id, variables=list(ctx.variables))
    return {}


async def exec_log(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """写日志节点：按级别把 message 写进业务日志（支持 {{变量}} 替换）。

    config:
        message: 日志内容（必填，可含 {{变量}}）
        level:   DEBUG / INFO / WARNING / ERROR / CRITICAL，缺省 INFO
    """
    message = _render(str(node.config.get("message", "")), ctx.variables)
    level = str(node.config.get("level", "INFO")).upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        level = "INFO"
    getattr(ctx.logger, level.lower())(f"[log:{node.id}] {message}")
    ctx.log.append(f"[{level}] {node.id}: {message}")
    return {"log_message": message}


async def exec_test(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """测试 / 调试节点：把配置内容回显到日志和上下文，供画布联调用。

    config:
        echo: 要回显的内容（可含 {{变量}}），缺省用节点 id
    """
    echo = _render(str(node.config.get("echo", node.id)), ctx.variables)
    ctx.logger.info(f"[test:{node.id}] {echo}", variables=dict(ctx.variables))
    ctx.log.append(f"[test] {node.id}: {echo}")
    return {"echo": echo}


async def _register_cron(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """把整条流程按 cron 登记到调度器（start 节点 trigger=time 的行为）。

    config:
        cron:   cron 表达式（必填，5 或 6 段）
        name:   调度任务显示名（可选，缺省用节点 id）

    调度器没注入时只记日志、不实际登记（测试 / 离线场景）；登记的 task_id 固定为
    ``wf-<node.id>``，重复执行会先移除再登记（幂等）。
    """
    cron = str(node.config.get("cron", "")).strip()
    name = str(node.config.get("name", node.id))
    task_id = f"wf-{node.id}"

    if ctx.scheduler is None:
        ctx.logger.warning(
            f"[start:{node.id}] 时间触发未注入调度器，跳过登记",
            node_id=node.id,
            cron=cron,
        )
        ctx.log.append(f"[start:time] {node.id}: 未注入调度器，cron={cron}")
        return {"scheduled": False, "task_id": task_id, "cron": cron}

    # 幂等：先移除同名旧任务再登记（流程重跑 / 改 cron 时不残留）
    try:
        ctx.scheduler.remove(task_id)
    except KeyError:
        pass

    async def _trigger() -> None:
        """到点回调：跑整条流程。"""
        ctx.logger.info(f"[start:{node.id}] cron 触发，开始执行工作流", node_id=node.id)
        await ctx.run_workflow()

    task = ctx.scheduler.add(
        cron,
        _trigger,
        task_id=task_id,
        name=name,
        description=f"工作流开始节点（时间触发）{node.id}",
    )
    ctx.logger.info(
        f"[start:{node.id}] 已登记到调度器",
        node_id=node.id,
        cron=cron,
        task_id=task_id,
        next_run=str(task.next_run) if task.next_run else None,
    )
    ctx.log.append(f"[start:time] {node.id}: 已登记 cron={cron}, task_id={task_id}")
    return {"scheduled": True, "task_id": task_id, "cron": cron}


async def exec_legacy_time_trigger(
    node: WorkflowNode, ctx: NodeExecutionContext
) -> dict[str, Any]:
    """旧版 ``time-trigger`` 节点的兼容执行器（已发布的历史版本快照里可能还有）。

    新版画布只产生 ``start`` + ``trigger=time``；这里保证旧快照不重新发布也能继续触发。
    """
    return await _register_cron(node, ctx)


def _render(template: str, variables: dict[str, Any]) -> str:
    """把模板里的 {{name}} 替换成变量值（变量不存在留空串）。"""
    import re

    def sub(match: re.Match[str]) -> str:
        name = match.group(1)
        return str(variables.get(name, ""))

    return re.sub(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", sub, template)


# --------------------------------------------------------------------------- 注册
register_executor("start", exec_start)
register_executor("end", exec_end)
register_executor("log", exec_log)
register_executor("test", exec_test)
# 兼容：旧版独立 time-trigger 类型（历史版本快照）；新图请用 start + trigger=time
register_executor("time-trigger", exec_legacy_time_trigger)


# --------------------------------------------------------------------------- 主流程
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
        from collections import deque

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
