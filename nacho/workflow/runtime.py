"""工作流运行时：把已发布版本的图加载出来并执行，让开始节点的触发配置生效。

发布工作流时调一次 :func:`run_published_workflow`——它会跑整张图，其中
``trigger=time`` 的开始节点会把整条流程登记到调度器；之后调度器到点自动触发。

到点触发的回调 :func:`make_trigger` 会重新加载该版本的图再跑一遍（幂等：
开始节点会先移除旧任务再重新登记，不会叠加）。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from nacho.core.logger import get_logger
from nacho.core.scheduler import TaskManager

from .executor import NodeExecutionContext, SimpleWorkflowRunner

if TYPE_CHECKING:
    from .store import SqlWorkflowStore

_logger = get_logger("workflow.runtime")


def make_trigger(
    workflow_id: str,
    version: int,
    store: "SqlWorkflowStore",
    scheduler: TaskManager,
) -> Callable[[], Awaitable[None]]:
    """构造到点触发回调：重新加载版本图并执行整条流程。

    每次触发都重新跑一遍，时间触发的开始节点会幂等地重新登记自己（先移除再添加），
    所以不会因为重复触发而在调度器里堆积任务。
    """

    async def trigger() -> None:
        await run_published_workflow(workflow_id, version, store, scheduler)

    return trigger


async def run_published_workflow(
    workflow_id: str,
    version: int,
    store: "SqlWorkflowStore",
    scheduler: TaskManager,
) -> None:
    """加载指定版本的图并执行（发布时调用一次，让时间触发的开始节点登记到调度器）。"""
    record = await store.get_version(workflow_id, version)
    if record is None:
        _logger.warning(
            "工作流版本不存在，跳过执行",
            workflow_id=workflow_id,
            version=version,
        )
        return

    graph = record.graph()
    # 到点回调：再跑一次同一个版本（时间触发开始节点幂等重登记）
    trigger = make_trigger(workflow_id, version, store, scheduler)
    ctx = NodeExecutionContext(scheduler=scheduler, run=trigger)
    runner = SimpleWorkflowRunner()
    try:
        await runner.run(graph, ctx)
        _logger.info(
            "工作流执行完成",
            workflow_id=workflow_id,
            version=version,
            node_count=len(graph.nodes),
        )
    except Exception as exc:  # noqa: BLE001 — 执行引擎异常不能让发布接口挂掉
        _logger.error(
            "工作流执行失败",
            workflow_id=workflow_id,
            version=version,
            error=str(exc),
        )
