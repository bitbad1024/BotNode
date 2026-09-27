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
from typing import TYPE_CHECKING, Any

from nacho.core.logger import BaseLogger, BoundLogger, get_logger
from nacho.core.scheduler import TaskManager

from .executor import NodeExecutionContext, SimpleWorkflowRunner
from .graph import start_ids
from .nodes import get_executor, workflow_task_id

if TYPE_CHECKING:
    from .store import SqlWorkflowStore


def _log() -> BaseLogger | BoundLogger:
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
    *,
    onebot: Any | None = None,
) -> Callable[[], Awaitable[None]]:
    """构造到点触发回调：重新加载版本图并执行整条流程。

    每次触发都重新加载该版本的图跑一遍。开始节点在这一趟**不再动调度器**（任务在它触发之前
    就已经排好了下一次），所以不会因为重复触发而在调度器里堆积任务。

    ``onebot``（OneBot 服务端，可选）在这里就得带上：调度器到点执行的是**这一趟构造的
    闭包**，错过这儿后面没机会再补（见 :func:`register_published_workflow`）。
    """

    async def trigger() -> None:
        await run_published_workflow(workflow_id, version, store, scheduler, onebot=onebot)

    return trigger


async def run_published_workflow(
    workflow_id: str,
    version: int,
    store: SqlWorkflowStore,
    scheduler: TaskManager,
    *,
    onebot: Any | None = None,
) -> None:
    """加载指定版本的图并**执行整条流程**；到点回调走它（见 :func:`make_trigger`）。

    启动载入**不走这里** —— 那一步只登记触发、不执行图，见
    :func:`register_published_workflow`。``onebot`` 从这里注进节点上下文：``onebot``
    节点靠它发动作（挑连接的归属 ``ctx.owner_id`` 来自定义表）。

    归属先读出来：这一趟的每条日志都挂在**这条流的主人**名下（与 ``ctx.owner_id`` 同一个
    出处），日志页里按人筛得到、也追得到责 —— 记成公共的话，谁的流在跑都看不出来。
    """
    # 归属（定义表的 owner_id）：onebot 节点按它挑「谁的」连接，日志按它认主人
    definition = await store.get(workflow_id)
    owner_id: str = definition.owner_id if definition is not None else ""
    log: BoundLogger = _log().bind(workflow_id=workflow_id, owner_id=owner_id)

    record = await store.get_version(workflow_id, version)
    if record is None:
        log.warning("工作流版本不存在，跳过执行", version=version)
        return

    graph = record.graph()
    # 到点回调：这个版本下次再到点，还是从这儿跑一遍（与本次同一个入口）
    trigger = make_trigger(workflow_id, version, store, scheduler, onebot=onebot)
    # 执行那一趟（register_triggers 缺省 False）：开始节点不碰调度器，它自己排下一次
    ctx = NodeExecutionContext(
        scheduler=scheduler,
        run=trigger,
        workflow_id=workflow_id,
        owner_id=owner_id,
        onebot=onebot,
    )
    runner = SimpleWorkflowRunner()
    try:
        await runner.run(graph, ctx)
        log.info("工作流执行完成", version=version, node_count=len(graph.nodes))
    except Exception as exc:  # noqa: BLE001 — 执行引擎异常不能让发布接口挂掉
        log.error("工作流执行失败", version=version, error=str(exc))


async def register_published_workflow(
    workflow_id: str,
    version: int,
    store: SqlWorkflowStore,
    scheduler: TaskManager,
    *,
    onebot: Any | None = None,
) -> int:
    """**只跑开始节点、不跑下游**：把这一版的触发登记好，返回跑过的开始节点数量。

    时间触发的开始节点，执行器做的就是「按 cron 把整条流程登记到调度器」（见
    :func:`nacho.workflow.nodes.start.exec_start`）；消息触发的只写一条「等待消息」日志。
    两种都只需要跑**开始节点自己**，后面的节点一个都不跑 —— 启动载入不是执行。以前这里是
    「跑一遍整张图，靠开始节点顺带登记」，代价是每次开机都真的把整条流程执行一次（下游的
    http / log 全都跟着跑了），而登记本身只是点个名。

    这是**登记那一趟**（``ctx.register_triggers=True``）：加 / 摘任务只在这儿发生；真正整图
    执行（cron 到点走 :func:`run_published_workflow`）那一趟不碰调度器，它自己会排下一次。

    ``onebot``（OneBot 服务端，可选）要在这里就带上：交给调度器的到点回调是**这一趟构造
    的**（``make_trigger`` 闭包），到点执行那一趟没机会再补。

    与执行那条路一个口径：归属先读出来，日志都挂在流的**主人**名下（``owner_id``），
    节点上下文也带同一份（见 :meth:`NodeExecutionContext.owner_id`）。
    """
    # 实例策略是**工作流级设置**（定义表里的列），与图无关：登记时读一次，由开始节点带给调度器
    definition = await store.get(workflow_id)
    owner_id: str = definition.owner_id if definition is not None else ""
    log: BoundLogger = _log().bind(workflow_id=workflow_id, owner_id=owner_id)

    record = await store.get_version(workflow_id, version)
    if record is None:
        log.warning("工作流版本不存在，跳过登记", version=version)
        return 0

    graph = record.graph()
    starts = set(start_ids(graph.nodes))
    # 登记时给的到点回调是「跑整条流程」那个（与到点触发同一条路）；
    # register_triggers=True：这才是「登记那一趟」，开始节点据此去调度器加 / 改任务
    ctx = NodeExecutionContext(
        scheduler=scheduler,
        run=make_trigger(workflow_id, version, store, scheduler, onebot=onebot),
        workflow_id=workflow_id,
        register_triggers=True,
        multi_instance=definition.multi_instance if definition is not None else False,
        owner_id=owner_id,
        onebot=onebot,
    )
    primed = 0
    for node in graph.nodes:
        if node.id not in starts:
            continue
        executor = get_executor(node.type)
        if executor is None:
            log.warning(
                "开始节点没有执行器，跳过载入",
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

    与登记对称，归属一样从定义表读：停用也是「谁的流被停了」，记成公共就没法按人查。
    """
    definition = await store.get(workflow_id)
    owner_id: str = definition.owner_id if definition is not None else ""
    log: BoundLogger = _log().bind(workflow_id=workflow_id, owner_id=owner_id)

    record = await store.get_version(workflow_id, version)
    if record is None:
        log.warning("工作流版本不存在，跳过停用", version=version)
        return 0

    removed = 0
    for node_id in start_ids(record.graph().nodes):
        if scheduler.remove(workflow_task_id(workflow_id, node_id)):
            removed += 1
    if removed:
        log.info("已停止定时触发", version=version, count=removed)
    return removed


class WorkflowTriggers:
    """启停某个已发布版本的时间触发（接口层的「运行开关」靠它**即时生效**）。

    接口层只认结构化的 ``start`` / ``stop`` 两个方法（见
    :mod:`nacho.api.api.workflow.protocols`），**不 import 本模块**；装配时由主程序把这一份
    传进 ``create_app(workflow_triggers=...)``。没传的场合（直接 ``create_app`` 的测试 / 示例）
    开关照样落库，只是生效点在下次启动载入。

    ``onebot``（OneBot 服务端，可选）装配时给：拨开关即时生效走的是这儿的登记，登记构造的
    到点闭包要带上它（见 :func:`make_trigger`）。
    """

    def __init__(
        self,
        store: SqlWorkflowStore,
        scheduler: TaskManager,
        *,
        onebot: Any | None = None,
    ) -> None:
        self._store: SqlWorkflowStore = store
        self._scheduler: TaskManager = scheduler
        self._onebot: Any | None = onebot

    async def start(self, workflow_id: str, version: int) -> int:
        """登记这一版的时间触发（重复调用幂等），返回跑过的开始节点数量。"""
        return await register_published_workflow(
            workflow_id, version, self._store, self._scheduler, onebot=self._onebot
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
    onebot: Any | None = None,
    page_size: int = 500,
) -> int:
    """启动时把**开着运行开关**的已发布工作流登记就绪，返回载入的开始节点数量。

    遍历 ``status=published`` 且 ``published_version>0`` **且 ``enabled``** 的定义，逐个跑其
    已发布版本的**开始节点**（时间触发的据此把整条流程登记到 cron；**下游一个都不执行**，见
    :func:`register_published_workflow`）。发布 ≠ 运行：刚发布的（开关默认关）不在这里被跑。
    单个失败不影响其他工作流，异常只记 error。

    **翻页翻到底，不给自己设总量上限**：以前是写死 ``limit=500`` 一次拉完 —— 超过 500 条的
    那些工作流开机根本不会登记（静默漏跑，最难查的那种）。现在按 ``page_size`` 一页页拉
    （``store.list`` 的 ``limit``/``offset``），拉空为止。

    分页按 ``updated_at`` 倒序进行，而这一趟**只读库**（登记不写定义表，见
    :func:`register_published_workflow`），所以遍历期间顺序稳定；即便如此，接口层此刻已经在
    跑、可能并发保存暂存区（会改 ``updated_at``、把行挪到另一页），因此对同一 id 只登记一次
    —— 重复登记无害，漏掉才致命。

    ``onebot``（OneBot 服务端，可选）由装配层传进来，跟着登记一起进到点闭包（见
    :func:`make_trigger`）；没接 OneBot 的场合不传，``onebot`` 节点跑到时当场报错。

    日志：开头一条「开始载入」，结尾一条「载入完成」带各档条数（扫过多少、登记了哪些、开关
    关着跳过了多少、登记到几个开始节点）—— **一条都没登记也照记**，好把「载入跑过了，只是
    没得跑」和「载入压根没跑」分开。哪条工作流被登记，看开始节点那条（``已登记到调度器`` /
    ``工作流开始（消息触发…）``，都带 ``workflow_id``）。

    归属：开头 / 结尾这两条是**跨所有工作流**的全局事件，归公共；单条工作流的事（载入失败的
    error、开始节点那几条）挂在它自己的 ``owner_id`` 名下。
    """
    log: BaseLogger = _log()
    primed = 0  # 登记到的开始节点数
    registered = 0  # 真正登记上的工作流条数
    disabled = 0  # 已发布但开关关着：跳过
    scanned = 0  # 扫过的定义数（含没发布 / 开关关着的）
    seen: set[str] = set()
    offset = 0
    log.info("开始载入已发布工作流")
    while True:
        definitions = await store.list(owner_id=None, limit=page_size, offset=offset)
        if not definitions:
            break
        for definition in definitions:
            if definition.id in seen:
                continue
            seen.add(definition.id)
            scanned += 1
            if definition.status != "published" or definition.published_version <= 0:
                continue
            if not definition.enabled:
                disabled += 1
                continue  # 已发布但开关关着：不登记、不跑（新发布默认就是这个状态）
            try:
                primed += await register_published_workflow(
                    definition.id,
                    definition.published_version,
                    store,
                    scheduler,
                    onebot=onebot,
                )
                registered += 1
            except Exception as exc:  # noqa: BLE001 — 单个坏工作流不能挡住启动
                log.error(
                    "启动载入已发布工作流失败",
                    workflow_id=definition.id,
                    owner_id=definition.owner_id,  # 谁的流没载进来，当场认得出
                    version=definition.published_version,
                    error=str(exc),
                )
        offset += len(definitions)
        if len(definitions) < page_size:
            break
    log.info(
        "已发布工作流启动载入完成",
        scanned=scanned,  # 扫过的定义
        registered=registered,  # 登记上的工作流
        triggers=primed,  # 登记到的开始节点
        disabled=disabled,  # 开关关着跳过的
    )
    return primed
