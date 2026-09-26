"""工作流运行时：把已发布版本的图加载出来，让开始节点的触发配置生效。

**发布 ≠ 运行**：发布接口只挪发布指针；要不要真的跑由定义上的**运行开关**（``enabled``）
决定，默认关着。服务启动时调一次 :func:`load_published_workflows` —— 只挑**开关开着**的
已发布工作流，把 ``trigger=time`` 的开始节点按 cron 登记到调度器，整张图**不执行**；
运行期间拨开关由 :class:`WorkflowTriggers` 即时启停（接口层的开关接口调它）。

（登记只调开始节点自己，见 :func:`register_published_workflow` —— 以前这里靠「跑一遍图、
顺带登记」，代价是每次启动都真的把整条流程执行一遍。停用是对称的，见
:func:`stop_published_workflow`，同样不跑图。）

调度器到点后走 :func:`make_trigger`：重新加载该版本的图并**跑整条流程**。这一趟**不碰调度器**
（``ctx.register_triggers=False``）—— 任务在调度器里排着，而它在派发前就重排好了下一次；
加 / 摘任务只发生在「登记那一趟」，见 :func:`register_published_workflow`。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from nacho.core.logger import BaseLogger, get_logger
from nacho.core.scheduler import TaskManager

from .executor import NodeExecutionContext, SimpleWorkflowRunner
from .graph import start_ids
from .nodes import get_executor, workflow_task_id

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
    store: SqlWorkflowStore,
    scheduler: TaskManager,
) -> Callable[[], Awaitable[None]]:
    """构造到点触发回调：重新加载版本图并执行整条流程。

    每次触发都重新加载该版本的图跑一遍。开始节点在这一趟**不再动调度器**（任务在它触发之前
    就已经排好了下一次），所以不会因为重复触发而在调度器里堆积任务。
    """

    async def trigger() -> None:
        await run_published_workflow(workflow_id, version, store, scheduler)

    return trigger


async def run_published_workflow(
    workflow_id: str,
    version: int,
    store: SqlWorkflowStore,
    scheduler: TaskManager,
) -> None:
    """加载指定版本的图并**执行整条流程**；到点回调走它（见 :func:`make_trigger`）。

    启动载入**不走这里** —— 那一步只登记触发、不执行图，见
    :func:`register_published_workflow`。
    """
    record = await store.get_version(workflow_id, version)
    if record is None:
        _log().warning(
            "工作流版本不存在，跳过执行",
            workflow_id=workflow_id,
            version=version,
        )
        return

    graph = record.graph()
    # 到点回调：这个版本下次再到点，还是从这儿跑一遍（与本次同一个入口）
    trigger = make_trigger(workflow_id, version, store, scheduler)
    # 执行那一趟（register_triggers 缺省 False）：开始节点不碰调度器，它自己排下一次
    ctx = NodeExecutionContext(scheduler=scheduler, run=trigger, workflow_id=workflow_id)
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


async def register_published_workflow(
    workflow_id: str,
    version: int,
    store: SqlWorkflowStore,
    scheduler: TaskManager,
) -> int:
    """**只跑开始节点、不跑下游**：把这一版的触发登记好，返回跑过的开始节点数量。

    时间触发的开始节点，执行器做的就是「按 cron 把整条流程登记到调度器」（见
    :func:`nacho.workflow.nodes.start.exec_start`）；消息触发的只写一条「等待消息」日志。
    两种都只需要跑**开始节点自己**，后面的节点一个都不跑 —— 启动载入不是执行。以前这里是
    「跑一遍整张图，靠开始节点顺带登记」，代价是每次开机都真的把整条流程执行一次（下游的
    http / log 全都跟着跑了），而登记本身只是点个名。

    这是**登记那一趟**（``ctx.register_triggers=True``）：加 / 摘任务只在这儿发生；真正整图
    执行（cron 到点走 :func:`run_published_workflow`）那一趟不碰调度器，它自己会排下一次。
    """
    record = await store.get_version(workflow_id, version)
    if record is None:
        _log().warning(
            "工作流版本不存在，跳过登记",
            workflow_id=workflow_id,
            version=version,
        )
        return 0

    graph = record.graph()
    starts = set(start_ids(graph.nodes))
    # 登记时给的到点回调是「跑整条流程」那个（与到点触发同一条路）；
    # register_triggers=True：这才是「登记那一趟」，开始节点据此去调度器加 / 改任务
    ctx = NodeExecutionContext(
        scheduler=scheduler,
        run=make_trigger(workflow_id, version, store, scheduler),
        workflow_id=workflow_id,
        register_triggers=True,
    )
    primed = 0
    for node in graph.nodes:
        if node.id not in starts:
            continue
        executor = get_executor(node.type)
        if executor is None:
            _log().warning(
                "开始节点没有执行器，跳过载入",
                workflow_id=workflow_id,
                version=version,
                node_id=node.id,
                node_type=node.type,
            )
            continue
        await executor(node, ctx)
        primed += 1
    return primed


async def stop_published_workflow(
    workflow_id: str,
    version: int,
    store: SqlWorkflowStore,
    scheduler: TaskManager,
) -> int:
    """把这一版里**开始节点登记过的定时任务**摘掉，返回摘掉的数量。

    与登记对称：任务名由 :func:`nacho.workflow.nodes.start.workflow_task_id` 定
    （``wf-<工作流 id>-<节点 id>``），照图里的开始节点算一遍 id 去摘即可 ——
    **不用把图跑一遍**（那是执行，不是停机）。
    """
    record = await store.get_version(workflow_id, version)
    if record is None:
        _log().warning(
            "工作流版本不存在，跳过停用",
            workflow_id=workflow_id,
            version=version,
        )
        return 0

    removed = 0
    for node_id in start_ids(record.graph().nodes):
        if scheduler.remove(workflow_task_id(workflow_id, node_id)):
            removed += 1
    if removed:
        _log().info(
            "已停止定时触发",
            workflow_id=workflow_id,
            version=version,
            count=removed,
        )
    return removed


class WorkflowTriggers:
    """启停某个已发布版本的时间触发（接口层的「运行开关」靠它**即时生效**）。

    接口层只认结构化的 ``start`` / ``stop`` 两个方法（见
    :mod:`nacho.api.api.workflow.protocols`），**不 import 本模块**；装配时由主程序把这一份
    传进 ``create_app(workflow_triggers=...)``。没传的场合（直接 ``create_app`` 的测试 / 示例）
    开关照样落库，只是生效点在下次启动载入。
    """

    def __init__(self, store: SqlWorkflowStore, scheduler: TaskManager) -> None:
        self._store: SqlWorkflowStore = store
        self._scheduler: TaskManager = scheduler

    async def start(self, workflow_id: str, version: int) -> int:
        """登记这一版的时间触发（重复调用幂等），返回跑过的开始节点数量。"""
        return await register_published_workflow(
            workflow_id, version, self._store, self._scheduler
        )

    async def stop(self, workflow_id: str, version: int) -> int:
        """摘掉这一版登记过的定时任务（重复调用无害），返回摘掉的数量。"""
        return await stop_published_workflow(
            workflow_id, version, self._store, self._scheduler
        )


async def load_published_workflows(
    store: SqlWorkflowStore,
    scheduler: TaskManager,
    *,
    limit: int = 500,
) -> int:
    """启动时把**开着运行开关**的已发布工作流登记就绪，返回载入的开始节点数量。

    遍历 ``status=published`` 且 ``published_version>0`` **且 ``enabled``** 的定义，逐个跑其
    已发布版本的**开始节点**（时间触发的据此把整条流程登记到 cron；**下游一个都不执行**，见
    :func:`register_published_workflow`）。发布 ≠ 运行：刚发布的（开关默认关）不在这里被跑。
    单个失败不影响其他工作流，异常只记 error。
    """
    definitions = await store.list(owner_id=None, limit=limit)
    primed = 0
    for definition in definitions:
        if definition.status != "published" or definition.published_version <= 0:
            continue
        if not definition.enabled:
            continue  # 已发布但开关关着：不登记、不跑（新发布默认就是这个状态）
        try:
            primed += await register_published_workflow(
                definition.id, definition.published_version, store, scheduler
            )
        except Exception as exc:  # noqa: BLE001 — 单个坏工作流不能挡住启动
            _log().error(
                "启动载入已发布工作流失败",
                workflow_id=definition.id,
                version=definition.published_version,
                error=str(exc),
            )
    if primed:
        _log().info("已发布工作流启动载入完成", count=primed)
    return primed
