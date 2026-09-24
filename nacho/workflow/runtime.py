"""工作流运行时：把已发布版本的图加载出来并执行，让开始节点的触发配置生效。

服务启动时调一次 :func:`load_published_workflows`——遍历所有 status=published 的
工作流，跑一遍其已发布版本的图，``trigger=time`` 的开始节点会把整条流程登记到调度器；
之后调度器到点自动触发。发布接口本身**不执行图**（只挪发布指针），改了触发配置后
重启服务即按新版本登记。

到点触发的回调 :func:`make_trigger` 会重新加载该版本的图再跑一遍（幂等：
开始节点会先移除旧任务再重新登记，不会叠加）。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from nacho.core.logger import BaseLogger, get_logger
from nacho.core.scheduler import TaskManager

from .executor import NodeExecutionContext, SimpleWorkflowRunner

if TYPE_CHECKING:
    from .store import SqlWorkflowStore


def _log() -> BaseLogger:
    """取本模块的日志实例：**用到才取**，不要在模块级取。

    模块级 ``_logger = get_logger(...)`` 是**导入即执行**的：谁先 import 这个模块，谁就顺手把
    进程默认日志核心按默认参数建出来（那时配置还没读），于是 ``[logging]`` 里的颜色 / 级别被
    定死之后再也传不进去 —— 入口后面那次 ``configure(console_color=True)`` 只会撞上
    「已存在核心」被丢掉（见 :mod:`nacho.core.logger.manager`）。用到才取，核心由启动顺序建。
    """
    return get_logger("workflow.runtime")


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
        _log().warning(
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
        _log().info(
            "工作流执行完成",
            workflow_id=workflow_id,
            version=version,
            node_count=len(graph.nodes),
        )
    except Exception as exc:  # noqa: BLE001 — 执行引擎异常不能让发布接口挂掉
        _log().error(
            "工作流执行失败",
            workflow_id=workflow_id,
            version=version,
            error=str(exc),
        )


async def load_published_workflows(
    store: "SqlWorkflowStore",
    scheduler: TaskManager,
    *,
    limit: int = 500,
) -> int:
    """启动时把所有**已发布**工作流载入调度器，返回成功触发登记的工作流数量。

    遍历 ``status=published`` 且 ``published_version>0`` 的定义，逐个跑其已发布版本
    （时间触发的开始节点在里面完成 cron 登记；消息触发只写日志）。单个失败不影响
    其他工作流，异常只记 error。
    """
    definitions = await store.list(owner_id=None, limit=limit)
    loaded = 0
    for definition in definitions:
        if definition.status != "published" or definition.published_version <= 0:
            continue
        try:
            await run_published_workflow(
                definition.id, definition.published_version, store, scheduler
            )
            loaded += 1
        except Exception as exc:  # noqa: BLE001 — 单个坏工作流不能挡住启动
            _log().error(
                "启动载入已发布工作流失败",
                workflow_id=definition.id,
                version=definition.published_version,
                error=str(exc),
            )
    if loaded:
        _log().info("已发布工作流启动载入完成", count=loaded)
    return loaded
