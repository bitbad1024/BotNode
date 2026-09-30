"""异步日志系统：一根 root + 若干绑定视图。

职责划分
========

* :meth:`BaseLogger.write`：**写入方法**。默认把日志推到消息队列（非阻塞），
  业务侧永远不会因为落盘 / 落库而卡住；
* :meth:`BaseLogger.bind`：**绑定**。得到一份 :class:`BoundLogger` 视图：名字、出口、
  级别、默认字段四样都能换，适合给一段执行（一趟工作流、一次请求）统一打上下文标记，
  不用每个调用点手抄一遍；
* 内部分发器从队列批量取日志，**照着每条记录自带的目标**扇出给各处理机；
* :meth:`BaseLogger.flush`：**刷新缓冲区方法**，刷新所有处理机的缓冲区；
* :meth:`BaseLogger.search`：**检索方法**，聚合各处理机的检索结果。

没有派生实例，只有扁平化视图
============================

曾经有一套「名字 -> 派生实例」的树：``get_logger("a.b")`` 会派生一个子实例，把父级
的出口复制成「落回配置」（创建即冻结），于是**取实例的先后顺序会影响它能收到什么**
—— 那个坑连同 ``attach`` / 落回配置一起删了，``child`` 以**扁平化视图**的形态回来：
:class:`ChildLogger` 只记「父是谁 + 自己的覆盖项」，**不缓存、不复制**，写日志时沿父链
**现解析**出口与级别。现在：

* **出口长在 root 身上**：构造给（``processors=``）或后来 :meth:`mount` 上去；
* **要命名层级就用 :meth:`BaseLogger.child`**：父级后挂出口、改级别对已派生节点
  **立即生效**，「先取实例再挂出口」的顺序坑不复现；
* **一个模块另一处去处**就 :meth:`route` 发布一条具名路由（一个名字 = 一份绑定好的
  视图）；不发布就跟着 root 那份走；
* **目标随记录走**：写入那一刻目标就钉死在 :attr:`~nacho.core.logger.models
  .LogRecord.targets` 上，分发只是照着投，不再查任何名字表。

过滤器
======

``log_filter``（:class:`~nacho.core.logger.filters.LogFilter`）挂在**目标**一侧
（:class:`~nacho.core.logger.models.Target`）：一条日志只有通过某个目标的过滤器才会
被投递给它，被过滤掉的日志连处理机的缓冲区都不进。过滤器因此不属于处理机——处理机
只负责落地。

**两级缓冲，分工明确**：

* :class:`~nacho.core.logger.queue.AsyncLogQueue`（**交接**）：同步写入方与异步
  分发器之间的边界。``dispatch_timeout`` 只决定「一次最多等多久把手上这批取走」，
  是**交接窗口**而非攒批窗口，所以取小值，让日志尽快交到处理机；
* :class:`~nacho.core.logger.processors.base.BaseLogProcessor` 的缓冲区（**攒批**）：
  每个出口自己的批大小与刷盘周期（``buffer_size`` / ``flush_interval``），攒批窗口
  只由它决定——控制台可以 ``buffer_size=1`` 逐条直写，文件 / 数据库则成批落，
  互不牵制。
"""
from __future__ import annotations

import asyncio
import logging
import traceback
from contextlib import suppress
from collections.abc import Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType, TracebackType
from typing import TextIO, TypedDict, cast, override

from .filters import DENY_ALL, LogFilter
from .models import LogLevel, LogRecord, LogSearchResult, Target, TimestampLike
from .processors.base import BaseLogProcessor, ProcessorStats
from .processors.console import ConsoleLogProcessor
from .queue import AsyncLogQueue, OverflowPolicy

_fallback = logging.getLogger("nacho.core.logger")


class QueueStats(TypedDict):
    """队列侧统计（同步写入方与异步分发器之间的交接队列）。"""

    size: int
    maxsize: int
    dropped: int
    closed: bool


class DroppedStats(TypedDict):
    """两级缓冲的丢弃汇总（队列满 + 处理机缓冲满）。"""

    queue: int
    buffers: int
    total: int


class LoggerStats(TypedDict):
    """一个日志实例的运行状态快照（:attr:`BaseLogger.stats` 的返回结构）。"""

    name: str
    level: str
    running: bool
    #: 构造时是否要求了默认控制台输出（运行时也能看出来「有没有那一路」）
    console: bool
    queue: QueueStats
    processors: list[ProcessorStats]
    dropped: DroppedStats
    #: 「实例名字 -> 该实例解析后会投的处理机名」
    routes: dict[str, list[str]]




class _SharedState:
    """运行时状态：队列与**已接纳的处理机清单**。

    曾经这里还放着一张「名字 -> 日志实例」的分发表，分发器靠它查一条日志该投给谁；
    现在目标随记录走（见 :attr:`~nacho.core.logger.models.LogRecord.targets`），
    这张表连同派生实例一起没了 —— 剩下的只有生命周期要用到的清单：
    ``start`` / ``stop`` / ``flush`` / ``search`` / ``stats`` 都按它来。
    """

    def __init__(self, queue: AsyncLogQueue) -> None:
        self.queue: AsyncLogQueue = queue
        #: 所有已接纳处理机的去重清单（生命周期 / 刷新 / 检索 / 统计）
        self.registry: list[BaseLogProcessor] = []



class BaseLogger:
    """日志系统本体（一根 root）：队列 + 分发器 + 一份目标清单。

    出口就长在它身上：构造给的那些、后来 :meth:`mount` 上去的那些。业务侧拿到的要么
    是 :meth:`bind` 出来的上下文视图（:class:`BoundLogger`），要么是 :meth:`child`
    出来的**扁平化命名视图**（:class:`ChildLogger`）—— 没有冻结、没有落回配置：
    一条日志投给谁，写的时候按解析出的那份目标定了。

    想要「某个模块另一个去处」， :meth:`route` 发布一条具名路由就够了：一个名字 =
    一份绑定好的视图，不发布就跟着 root 那份走。
    """

    def __init__(
        self,
        name: str = "nacho",
        *,
        level: LogLevel | str = LogLevel.INFO,
        queue: AsyncLogQueue | None = None,
        processors: "Sequence[Target | BaseLogProcessor] | None" = None,
        console: bool = True,
        console_stream: TextIO | None = None,
        console_level: "LogLevel | str | None" = None,
        console_color: bool = True,
        overflow_policy: OverflowPolicy | str = OverflowPolicy.DROP_OLDEST,
        queue_maxsize: int = 10000,
        dispatch_batch_size: int = 200,
        dispatch_timeout: float = 0.2,
    ) -> None:
        """
        :param processors: 默认目标。元素可以是 :class:`Target`（带过滤器与优先级），
            也可以直接是处理机（等价于 ``Target(processor)``：全收、优先级 0）。
        :param console: 是否默认挂一路控制台输出（默认 ``True``）：库 / 服务端不想
            要任何标准输出就传 ``False``。已经挂过控制台就不重复挂。
        :param console_level: 控制台最低级别，默认与 ``level`` 一致（控制台通常只给
            人看，可以比文件出口更粗）。它作为**出口级 level 门槛**挂在那个目标上 ——
            控制台处理机自己不做过滤。
        :param dispatch_batch_size: 分发器一次最多从队列取多少条。这是**交接批量**，
            不是攒批水位线——攒批由各处理机的 ``buffer_size`` 决定。
        :param dispatch_timeout: 队列取不到新日志时，最多再等多久就把手上这批先交出去。
            这是**交接窗口**，不是攒批窗口：取小值（默认 0.2s）让日志尽快交到处理机，
            避免和处理器缓冲区的 ``buffer_size`` / ``flush_interval`` 形成两套互相
            打架的攒批配方。
        """
        self.name: str = name
        self._level: LogLevel = LogLevel.parse(level)
        # 注意：这里必须用 is not None 判断，AsyncLogQueue 实现了 __len__，
        # 空队列在布尔上下文中为 False，写成 ``queue or AsyncLogQueue(...)``
        # 会在队列恰好为空时错误地新建一个队列。
        shared = _SharedState(
            queue
            if queue is not None
            else AsyncLogQueue(maxsize=queue_maxsize, overflow_policy=overflow_policy)
        )
        #: 运行时状态（队列 / 已接纳的处理机清单）
        self._shared: _SharedState = shared
        self._queue: AsyncLogQueue = shared.queue
        self._dispatch_batch_size: int = dispatch_batch_size
        self._dispatch_timeout: float = dispatch_timeout
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._running: bool = False
        #: **默认目标**（没给自己的目标时都用它），按优先级排好
        self._targets: tuple[Target, ...] = ()
        #: **显式发布的具名路由**：只有 :meth:`publish` / :meth:`route` 过才在里面
        self._routes: dict[str, BoundLogger] = {}
        if processors:
            self.mount(*processors)
        #: 构造时是否要求了默认控制台输出（看得出「有没有那一路」）
        self._console_enabled: bool = console
        if console and self.get_processor(ConsoleLogProcessor.name) is None:
            # 控制台也是一个普通目标：级别门槛就是出口级 level（给出口设门槛，
            # 而不是让处理机自己认级别）
            self.mount(
                ConsoleLogProcessor(stream=console_stream, color=console_color),
                level=level if console_level is None else console_level,
            )

    @property
    def console_enabled(self) -> bool:
        """是否挂了默认的控制台输出。"""
        return self._console_enabled

    # ------------------------------------------------------------------ 目标
    @property
    def targets(self) -> tuple[Target, ...]:
        """默认目标（:class:`Target` 元组，按优先级排好）。"""
        return self._targets

    @property
    def level(self) -> LogLevel:
        """本实例的级别。"""
        return self._level

    @property
    def processors(self) -> list[BaseLogProcessor]:
        """默认目标里的处理机（按优先级排好）。"""
        return [target.processor for target in self._targets]

    def set_level(self, level: LogLevel | str) -> None:
        """改全局级别（没有 per-instance 级别这回事了：要别的粒度就 ``bind(level=...)``）。"""
        self._level = LogLevel.parse(level)

    def _retire(self, *processors: BaseLogProcessor) -> None:
        """让出口下线：从共享清单里摘掉，正在跑的安排它自己收尾（余量先刷完）。

        换通道时旧的那份不该再出现在 ``search`` / ``stats`` 里，也不能占着一个永远不
        关闭的句柄。停机是异步的，这里没法 await，所以交给事件循环自己去跑。
        """
        registry: list[BaseLogProcessor] = self._shared.registry
        for processor in processors:
            with suppress(ValueError):
                registry.remove(processor)
            if not processor.running:
                continue
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # pragma: no cover - 还没起事件循环（纯同步场景）
                continue
            _ = loop.create_task(processor.stop())

    def mount(
        self,
        *processors: "Target | BaseLogProcessor",
        log_filter: LogFilter | None = None,
        level: "LogLevel | str | None" = None,
        priority: int = 0,
        replace: bool = False,
    ) -> BaseLogger:
        """把出口挂到 root 上作为默认目标（没自己 bind 目标的视图都走这份）。

        处理机会被 :meth:`adopt` 接纳，于是停机照样 flush、``search`` / ``stats`` 照样
        看得到。重复挂**同一个对象**无害。

        :param log_filter: 给这批目标挂的过滤器（只放行通过它的记录）。
        :param level: 出口级最低级别，低于它的记录直接跳过；``None`` = 全收。只作用于
            直接传入的处理机，已包装成 :class:`Target` 的用自己的 ``level``。
        :param priority: 投放优先级，小的先投。
        :param replace: 换通道：先把**同名**的那几个目标摘掉再挂（换输出路径 / 热重载）。
        """
        added: list[Target] = [
            item
            if isinstance(item, Target)
            else Target(item, log_filter=log_filter, level=level, priority=priority)
            for item in processors
        ]
        self.adopt(*(target.processor for target in added))
        names = {target.processor.name for target in added}
        if replace:
            self._retire(*(item.processor for item in self._targets if item.processor.name in names))
        kept: tuple[Target, ...] = (
            tuple(item for item in self._targets if item.processor.name not in names)
            if replace
            else self._targets
        )
        for target in added:
            if not any(existing.processor is target.processor for existing in kept):
                kept = (*kept, target)
        self._targets = tuple(sorted(kept, key=lambda target: target.priority))
        return self

    # ------------------------------------------------------------------ 具名路由
    def publish(self, view: BoundLogger) -> BoundLogger:
        """把一份视图**发布**成这个名字的路由：之后 :func:`get_logger` 取到的就是它。

        取代派生实例树的那张表：**不发布就没有**，不再有「派生出来就自动有身份」这种
        隐式规则。典型用法是某一路要换个目标 / 堵个通道（``api.access`` 不进审计库），
        改完再发布回去，这条路上所有人都拿到改过的那份。
        """
        self._routes[view.name] = view
        return view

    def route(
        self,
        name: str,
        *,
        targets: "Sequence[Target | BaseLogProcessor] | None" = None,
        level: "LogLevel | str | None" = None,
        **defaults: object,
    ) -> BoundLogger:
        """取（必要时先建并发布）一条**具名路由**：一个名字 = 一份绑定好的视图。

        名字相对本实例（``"api.robot"`` -> ``"nacho.api.robot"``，写全名也认）；已经
        发布过就直接返回那份，不会覆盖。没给 ``targets`` 就沿用本实例的默认目标 ——
        所以「某个模块只要和别人一样」是不用发布任何东西的::

            access = core.route("api.access", trace_id="t-1")
            robot = core.route("api.robot", targets=[Target(file_outlet, priority=-1)])
        """
        full: str = self.qualify(name)
        existing: BoundLogger | None = self._routes.get(full)
        if existing is not None:
            return existing
        return self.publish(self.bind(name=full, targets=targets, level=level, **defaults))

    @property
    def named_routes(self) -> dict[str, BoundLogger]:
        """已发布的具名路由（名字 -> 视图）快照副本。"""
        return dict(self._routes)


    # ------------------------------------------------------------------ 注册表




    # ------------------------------------------------------------------ 输出通道挂载



    def get_processor(self, name: str) -> BaseLogProcessor | None:
        """按名称取已接纳的输出通道（在共享清单里找）。"""
        for processor in self._shared.registry:
            if processor.name == name:
                return processor
        return None

    @property
    def processor_registry(self) -> list[BaseLogProcessor]:
        """所有已接纳处理机的清单本身（请勿直接修改）。

        与 :attr:`processors` 的区别：那是 root 的**默认目标**，这里还包含只在某个
        ``bind`` 里出现过的出口 —— 生命周期与刷新 / 检索都按这份来。
        """
        return self._shared.registry

    # ------------------------------------------------------------------ 名字
    def qualify(self, name: str) -> str:
        """把**相对名字**补全成本实例名下的完整名字：``LogCore("nacho").qualify("a1")`` ->
        ``"nacho.a1"``。

        已经带前缀的完整名字原样返回（``"nacho.a1"`` / ``"nacho"``），因此「写相对一段」与
        「写全名」两种写法可以混用；本实例名字为空时没有前缀可补，名字原样返回。
        """
        if not name or not self.name:
            return name
        if name == self.name or name.startswith(self.name + "."):
            return name
        return f"{self.name}.{name}"



    def bind(
        self,
        *,
        name: str | None = None,
        targets: "Sequence[Target | BaseLogProcessor] | None" = None,
        level: "LogLevel | str | None" = None,
        **defaults: object,
    ) -> BoundLogger:
        """绑一份**视图**：默认字段、名字、出口、级别都可以换，视图本身不登记。

        四个维度随用随给，每一项都是「给了就覆盖、没给就沿用 root 那份」：

        :param name: 写进 ``record.logger_name`` 的名字（不给就用 root 的名字）。它是
            **标签**，不是身份 —— 分发照着目标投，不查名字；
        :param targets: 这条路上每条日志投给哪些出口。元素可以是
            :class:`~nacho.core.logger.models.Target`（能带过滤器与优先级），也可以直接
            是处理机（等价于 ``Target(processor)``，全收、优先级 0）。给了的话，里面的
            处理机会被**接纳进共享清单**，停机照样 flush、``search`` / ``stats`` 照样
            看得到 —— 不给就沿用 root 的默认目标；
        :param level: 本视图的最低级别（不给就用 root 的）；
        :param defaults: 默认字段。``owner_id`` 也是可绑的一等字段，其余进 ``extra``；
            当次调用传了同名键就按当次的。

        详见 :class:`BoundLogger`。
        """
        return BoundLogger(self, name=name, targets=targets, level=level, **defaults)

    # ------------------------------------------------------------------ 命名层级
    def child(
        self,
        name: str,
        *,
        level: "LogLevel | str | None" = None,
        targets: "Sequence[Target | BaseLogProcessor] | None" = None,
    ) -> ChildLogger:
        """沿名字生长一层**扁平化视图**：名字经 :meth:`qualify` 拼接，不缓存、不复制。

        返回的 :class:`ChildLogger` 只记「父是谁 + 自己的覆盖项」（``_level`` /
        ``_targets`` 为 ``None`` 就表示继承父级），写日志时沿父链**现解析**出口与级别
        —— 所以父级后挂出口、改级别对已派生节点**立即生效**；每次调用都返回一份新
        视图对象，行为一致，没有「先取实例再挂出口」的顺序坑。

        :param name: 相对名字（``"api"``）或完整名字（``"nacho.api"``），经
            :meth:`qualify` 拼接成完整层级名；
        :param level: 本节点自己的最低级别；``None`` = 继承父级（写时解析）；
        :param targets: 本节点自己的出口；``None`` = 继承父级（写时解析），都没有则
            跟随 root 当前默认目标。

        child 与 bind 的分工：``child`` 换的是**名字与出口**（名字即身份，可继续
        ``child`` 生长）；``bind`` 换的是**默认字段**（给一段执行打上下文标记，产物
        只能 ``bind`` / ``log`` / ``mute``，不能再 ``child``）。
        """
        return ChildLogger(self, name=self.qualify(name), level=level, targets=targets)



    @property
    def running(self) -> bool:
        """分发器是否在运行（即 :meth:`start` 是否已生效）。"""
        return self._running

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> BaseLogger:
        """启动所有已接纳的处理机，并把分发器跑起来。"""
        if self._running:
            return self
        self._running = True
        for processor in list(self._shared.registry):
            try:
                await processor.start()
            except Exception:  # noqa: BLE001 - 单个处理机启动失败不影响整体
                _fallback.exception("处理机 %s 启动失败，已跳过", processor.name)
        self._dispatcher_task = asyncio.create_task(
            self._dispatch_loop(), name=f"nacho-log-dispatch-{self.name}"
        )
        return self

    async def stop(self, *, timeout: float = 5.0) -> None:
        """停止：关闭队列 -> 等待日志排空分发 -> 刷新并停止处理机。"""
        if not self._running:
            return
        self._running = False
        self._queue.close()

        if self._dispatcher_task is not None:
            try:
                await asyncio.wait_for(self._dispatcher_task, timeout=timeout)
            except asyncio.TimeoutError:
                _fallback.warning("日志分发器 %s 排空超时，强制取消", self.name)
                self._dispatcher_task.cancel()
                try:
                    await self._dispatcher_task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:  # pragma: no cover
                pass
            self._dispatcher_task = None

        for processor in list(self._shared.registry):
            try:
                await processor.stop()
            except Exception:  # noqa: BLE001 - 停止流程不抛出
                _fallback.exception("处理机 %s 停止失败", processor.name)

    async def _dispatch_loop(self) -> None:
        """分发器：批量取队列日志并扇出给各处理机。"""
        while True:
            batch = await self._queue.get_batch(
                self._dispatch_batch_size, self._dispatch_timeout
            )
            if batch:
                await self._dispatch(batch)
                continue
            if self._queue.closed and self._queue.empty:
                break


    def adopt(self, *processors: BaseLogProcessor) -> None:
        """把处理机接进共享清单（幂等：已经在里面就不动）。

        ``stop`` / ``flush`` / ``search`` / ``stats`` 都只认这张清单 —— 只出现在某个
        ``bind`` 的目标里却没被接纳进来的处理机，停机时不会 flush，日志会在缓冲区里丢。
        """
        registry: list[BaseLogProcessor] = self._shared.registry
        for processor in processors:
            if not any(existing is processor for existing in registry):
                registry.append(processor)

    async def _dispatch(self, records: list[LogRecord]) -> None:
        """一批日志扇出给各处理机：**照着每条记录自己的目标投**。

        目标在写入那一刻就写进了 :attr:`~nacho.core.logger.models.LogRecord.targets`
        （见 :meth:`write`），这里不做任何路由判断——分发只是把它归并成批、一次喂给
        同一个处理机，顺序按目标的 ``priority``（小的先投）。
        """
        batches: dict[int, tuple[BaseLogProcessor, list[LogRecord]]] = {}
        priorities: dict[int, int] = {}
        for record in records:
            for target in record.targets or ():
                processor = target.processor
                if not processor.healthy:
                    continue
                if target.level is not None and record.level < target.level:
                    continue
                if target.log_filter is not None and not target.log_filter.match(record):
                    continue
                key: int = id(processor)
                entry = batches.get(key)
                if entry is None:
                    batches[key] = (processor, [record])
                    priorities[key] = target.priority
                else:
                    entry[1].append(record)

        for key in sorted(batches, key=lambda item: priorities[item]):
            processor, batch = batches[key]
            if not processor.running:
                # 运行期动态挂载的输出通道：先启动它（打开文件 / 建表等）再喂日志，
                # 否则它的 _on_start 永远不会被调用
                try:
                    await processor.start()
                except Exception:  # noqa: BLE001 - 单个通道启动失败不影响其它通道
                    _fallback.exception("处理机 %s 启动失败，已跳过", processor.name)
                    continue
            try:
                await processor.handle_many(batch)
            except Exception:  # noqa: BLE001 - 处理机异常隔离，绝不影响分发器
                _fallback.exception("处理机 %s 处理日志失败", processor.name)

    # ------------------------------------------------------------------ 写入
    def is_enabled_for(self, level: LogLevel | str) -> bool:
        """本条日志是否达到本实例的级别。"""
        return LogLevel.parse(level) >= self._level

    def write(self, record: LogRecord) -> bool:
        """写入方法：默认把日志推到消息队列（非阻塞），返回是否入队成功。

        没带目标的记录在这里补上 root 的默认目标：目标在写入那一刻定死，分发就只剩
        「照着投」，不用再按名字反查任何表。
        """
        if record.targets is None:
            record.targets = self._targets
        try:
            return self._queue.put_nowait(record)
        except Exception:  # noqa: BLE001 - 写入永不抛出，避免拖垮业务
            _fallback.exception("日志入队失败")
            return False

    def log(
        self,
        level: LogLevel | str,
        message: object,
        *,
        owner_id: str = "",
        exc_info: object = False,
        **extra: object,
    ) -> bool:
        """构造日志记录并写入队列。

        ``owner_id`` 是这条日志的**所有者**：谁的操作就填谁（api 层填登录用户 id、ws 层填
        那条连接的归属），不填就是**空串 = 公共所有者**（启动、框架自身这类没归属的日志）。
        它单独成一等字段而不塞进 ``extra``：所有者要能渲染、能落库、能按它检索。

        ``exc_info`` 收口为 ``object``：既允许 ``True``（用当前异常），也允许
        ``sys.exc_info()`` 那样的 ``(type, value, traceback)`` 三元组；层间用
        ``**extra`` 透传时也只有宽类型才放得下，运行期再按元组 / 布尔分支处理。
        """
        parsed_level = LogLevel.parse(level)
        if not self.is_enabled_for(parsed_level):
            return False

        record = self.new_record(
            parsed_level,
            message,
            owner_id=owner_id,
            exc_info=exc_info,
            extra=extra,
            logger_name=self.name,
        )
        return self.write(record)

    def new_record(
        self,
        level: LogLevel,
        message: object,
        *,
        owner_id: str,
        exc_info: object,
        extra: Mapping[str, object],
        logger_name: str,
        targets: "tuple[Target, ...] | None" = None,
    ) -> LogRecord:
        """把一条日志要装的东西装成 :class:`LogRecord`（级别判定在这一步之前做）。

        :param logger_name: 记录归属的名字；``bind`` 出来的视图写自己的名字时用得上。
        :param targets: 这条记录的目标；给了就在写入时定死，不必分发时再查名字表。
        """
        exc_text: str | None = None
        if isinstance(exc_info, tuple):
            items = cast("tuple[object, ...]", exc_info)
            if len(items) == 3:
                exc_type: object = items[0]
                exc_value: object = items[1]
                exc_traceback: object = items[2]
                if (
                    (exc_type is None or isinstance(exc_type, type))
                    and (exc_value is None or isinstance(exc_value, BaseException))
                    and (
                        exc_traceback is None
                        or isinstance(exc_traceback, TracebackType)
                    )
                ):
                    exc_text = "".join(
                        traceback.format_exception(exc_type, exc_value, exc_traceback)
                    )
        elif exc_info:
            exc_text = traceback.format_exc()

        # 调用点传进来的 ``**extra`` 本身就是一个新鲜字典，直接交出去，不必再拷一份
        payload: dict[str, object] = extra if isinstance(extra, dict) else dict(extra)
        return LogRecord(
            message=str(message),
            level=level,
            logger_name=logger_name,
            extra=payload,
            exc_text=exc_text,
            owner_id=owner_id,
            targets=targets,
        )

    def debug(self, message: object, *, owner_id: str = "", **extra: object) -> bool:
        return self.log(LogLevel.DEBUG, message, owner_id=owner_id, **extra)

    def info(self, message: object, *, owner_id: str = "", **extra: object) -> bool:
        return self.log(LogLevel.INFO, message, owner_id=owner_id, **extra)

    def warning(self, message: object, *, owner_id: str = "", **extra: object) -> bool:
        return self.log(LogLevel.WARNING, message, owner_id=owner_id, **extra)

    def error(
        self, message: object, *, owner_id: str = "", exc_info: object = False, **extra: object
    ) -> bool:
        return self.log(LogLevel.ERROR, message, owner_id=owner_id, exc_info=exc_info, **extra)

    def exception(self, message: object, *, owner_id: str = "", **extra: object) -> bool:
        """记录一条 ERROR 日志并附带当前异常堆栈。"""
        return self.log(LogLevel.ERROR, message, owner_id=owner_id, exc_info=True, **extra)

    # ------------------------------------------------------------------ 刷新与检索
    async def flush(self) -> None:
        """刷新缓冲区方法：刷新所有处理机的缓冲区。"""
        processors = list(self._shared.registry)
        if not processors:
            return
        results = await asyncio.gather(
            *(processor.flush() for processor in processors),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):  # pragma: no cover - 兜底
                _fallback.exception("刷新处理机失败", exc_info=result)

    async def search(
        self,
        *,
        query: str | None = None,
        level: LogLevel | str | None = None,
        start: TimestampLike = None,
        end: TimestampLike = None,
        logger_name: str | None = None,
        owner_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        processors: Sequence[str] | None = None,
    ) -> LogSearchResult:
        """检索方法：聚合所有（或指定）处理机的检索结果，按**插入顺序**倒序返回，并带上总数。

        排序键是 ``(timestamp, seq)``：落库那份按自增序号定序（同一毫秒的几条也有先后），
        没有序号的出口（控制台 / 文件）退回时间戳；跨出口比较时时间戳是唯一共同尺度，序号
        只用来把同一时刻的顺序钉死。

        ``records`` 是翻页后的这一页（跨出口按 ``record_id`` 去重）；
        ``total`` 是各出口同一条件下的命中数**相加、不去重** —— 默认只查单个出口（如落库
        那份）时即为精确总数，多出口时同一条日志落了两份就会数两次（与去重后的条目口径
        不完全一致，这是刻意的：多出口本来就没人保证是同一批数据）。
        """
        targets = self._select_processors(processors)
        if not targets:
            return LogSearchResult()

        results = await asyncio.gather(
            *(
                processor.search(
                    query=query,
                    level=level,
                    start=start,
                    end=end,
                    logger_name=logger_name,
                    owner_id=owner_id,
                    # 各出口多要 offset + limit 条：合并去重后才好在本地切这一页
                    limit=limit + offset,
                    offset=0,
                )
                for processor in targets
            ),
            return_exceptions=True,
        )

        merged: list[LogRecord] = []
        total = 0
        for result in results:
            if isinstance(result, BaseException):
                _fallback.exception("检索处理机失败", exc_info=result)
                continue
            merged.extend(result.records)
            total += result.total

        # 时间倒序打底，同一时刻（时间戳粒度不够时，见 LogRecord.seq）用自增序号定序
        merged.sort(key=lambda item: (item.timestamp, item.seq), reverse=True)

        # 多个处理机可能存有同一条日志（同一 record_id），按 id 去重
        deduped: list[LogRecord] = []
        seen: set[str] = set()
        for record in merged:
            if record.record_id in seen:
                continue
            seen.add(record.record_id)
            deduped.append(record)
        return LogSearchResult(records=deduped[offset : offset + limit], total=total)

    def _select_processors(
        self, names: Sequence[str] | None = None
    ) -> list[BaseLogProcessor]:
        registered = list(self._shared.registry)
        if names is None:
            return registered
        wanted = set(names)
        return [p for p in registered if p.name in wanted]

    # ------------------------------------------------------------------ 状态
    @property
    def stats(self) -> LoggerStats:
        queue_dropped = self._queue.dropped
        registry = self._shared.registry
        buffer_dropped = sum(processor.dropped for processor in registry)
        processors = [processor.stats for processor in registry]
        return {
            "name": self.name,
            "level": self._level.name,
            "running": self._running,
            "console": self._console_enabled,
            "queue": {
                "size": self._queue.qsize(),
                "maxsize": self._queue.maxsize,
                "dropped": queue_dropped,
                "closed": self._queue.closed,
            },
            "processors": processors,
            # 两级缓冲都可能丢日志（队列满 + 处理机缓冲满），这里汇成一个视图，
            # 排查「日志怎么少了」只看一处，不必在队列与各处理机之间来回翻
            "dropped": {
                "queue": queue_dropped,
                "buffers": buffer_dropped,
                "total": queue_dropped + buffer_dropped,
            },
            # 发布过的具名路由：谁有自己的去处，一眼看得到（没发布的不在里面）
            "routes": {
                name: [target.processor.name for target in view.targets or ()]
                for name, view in sorted(self._routes.items())
            },
        }

    @override
    def __repr__(self) -> str:  # pragma: no cover - 调试用
        names = ", ".join(processor.name for processor in self.processors)
        return f"<BaseLogger name={self.name!r} level={self._level.name} processors=[{names}]>"


class BoundLogger:
    """**绑定过的日志视图**：默认字段 / 名字 / 出口 / 级别，四项都能换，视图本身不登记。

    四项每一项都随用随给：给了就覆盖，没给就沿用来源那份::

        log = core.bind(name="nacho.api.access", targets=[Target(file), Target(db, priority=-1)])
        log.info("一条")   # 投给 file 与 db（db 先投），record.logger_name = nacho.api.access

    目标随**记录**走（写进 :attr:`~nacho.core.logger.models.LogRecord.targets`），
    所以这条路上不再需要「名字 -> 实例」那张表：谁写、给谁、什么顺序，在 ``bind``
    那一刻就定了，分发只是照着做。

    只有 targets 里的处理机会被 :meth:`BaseLogger.adopt` 接纳进共享清单，停机照样
    flush、``search`` / ``stats`` 照样看得到 —— 换出口不再需要改实例那张表。

    适合给**一段执行**统一打上下文标记：一趟工作流带上 ``workflow_id`` / ``owner_id`` /
    ``user_id``，一次请求带上 ``trace_id``，这条路上之后每条日志自己就认得出是谁的，
    调用点不用一遍遍手抄::

        log = get_logger("workflow").bind(workflow_id="w1", user_id="10001")
        log.info("开始")                    # extra: workflow_id=w1, user_id=10001
        log.info("换人", user_id="10002")    # extra: workflow_id=w1, user_id=10002

    合并规则：

    * **当次传的同名键压过默认的** —— 那一次说的更准；
    * ``owner_id`` 是日志的一等字段（不塞 ``extra``）：``bind(owner_id="u-admin")`` 之后
      不显式传就按绑定的归属记，显式传了按那次的；
    * :meth:`write` 直接收记录时也走同一套合并，默认字段并进 ``record.extra``。

    **只读 + 不登记**，这两条决定了它能不能长期用得下去：

    * 默认字段是只读视图，``bind`` 只产出新视图（原视图不变），所以一份视图可以被
      多个协程同时拿着写，谁也不污染谁；
    * 它不进实例注册表，``bind`` 多少次都不会让注册表变长 —— 反过来正说明**请求级的
      东西不该靠它来"造实例"**：``user_id`` 这类每请求都变的值要么当次传参
      （``log.info("...", user_id=uid)``，多一个关键字参数的成本），要么一层 ``bind``
      一个**随请求生命周期一起丢弃**的视图（用完即扔，也别指望之后还能按名字找回来）。
      拿它去按用户/按请求登记名字（``nacho.api.user-42`` 那种），注册表就成了只增不减
      的字典 —— 那是 :meth:`BaseLogger.child` 该操心的事，不是 ``bind`` 的。

    一句话分工：**``child`` 换的是出口与名字（名字即身份），``bind`` 换的只有默认字段。**
    """

    __slots__: tuple[str, ...] = ("_logger", "_defaults", "_name", "_targets", "_level")

    def __init__(
        self,
        logger: BaseLogger,
        *,
        name: str | None = None,
        targets: "Sequence[Target | BaseLogProcessor] | None" = None,
        level: "LogLevel | str | None" = None,
        **defaults: object,
    ) -> None:
        self._logger: BaseLogger = logger
        #: 默认字段本身也是**只读视图**：视图之间可以纵向叠加、横向共享同一份字典，
        #: 谁都改不到别人的那一层 —— 多个协程共用一份（一趟工作流里大家拿同一个
        #: ``ctx.logger``）才安全。
        self._defaults: Mapping[str, object] = MappingProxyType(dict(defaults))
        #: 名字只是一条**标签**（写进 ``record.logger_name`` 给人 / 给检索看），不是身份：
        #: 为 ``None`` 时沿用源实例的名字。
        self._name: str | None = name
        #: 目标快照：``None`` = 不指定，沿用源实例那份（分发时按名字解析）
        self._targets: tuple[Target, ...] | None = None
        if targets is not None:
            normalized: list[Target] = [
                item if isinstance(item, Target) else Target(item) for item in targets
            ]
            normalized.sort(key=lambda target: target.priority)
            self._targets = tuple(normalized)
            # 只在这里出现的处理机也要能被停机 flush、被检索、被统计
            logger.adopt(*(target.processor for target in self._targets))
        self._level: LogLevel | None = None if level is None else LogLevel.parse(level)

    @property
    def name(self) -> str:
        """写进记录的名字：自己绑了就用绑的，否则沿用源实例的名字。"""
        return self._name if self._name is not None else self._logger.name

    @property
    def level(self) -> LogLevel:
        """本视图的生效级别（没单独绑就是源实例那份）。"""
        return self._level if self._level is not None else self._logger.level

    @property
    def targets(self) -> "tuple[Target, ...] | None":
        """本视图指定的目标快照；``None`` = 没指定，由分发按名字解析。"""
        return self._targets

    @property
    def defaults(self) -> Mapping[str, object]:
        """这份视图的默认字段（只读：改它不影响视图，要改就再 ``bind`` 一层）。"""
        return self._defaults

    def bind(
        self,
        *,
        name: str | None = None,
        targets: "Sequence[Target | BaseLogProcessor] | None" = None,
        level: "LogLevel | str | None" = None,
        **defaults: object,
    ) -> BoundLogger:
        """在既有绑定上**再叠一层**（同名默认字段按新的），返回一份新视图（本视图不变）。

        名字 / 目标 / 级别同样随给随覆盖：给了就换，没给就沿用**本视图**那一份。
        """
        return BoundLogger(
            self._logger,
            name=name if name is not None else self._name,
            targets=targets if targets is not None else self._targets,
            level=level if level is not None else self._level,
            **{**self._defaults, **defaults},
        )

    def mute(self, channel: str) -> BoundLogger:
        """给某个出口挂「全拒」：本视图的日志不再投给它（返回新视图，本视图不变）。

        典型用途：访问日志一次请求一条，只配给人翻文件 —— 逐条流水进了库会把「谁在
        什么时候干了什么」的审计时间线淹掉，于是把落库那条路在这份视图上堵住。
        """
        current: tuple[Target, ...] = (
            self._targets if self._targets is not None else self._logger.targets
        )
        return BoundLogger(
            self._logger,
            name=self._name,
            level=self._level,
            targets=[
                replace(target, log_filter=DENY_ALL)
                if target.processor.name == channel
                else target
                for target in current
            ],
            **dict(self._defaults),
        )

    # ------------------------------------------------------------------ 读（一律走源实例）
    @property
    def processor_registry(self) -> list[BaseLogProcessor]:
        """所有已接纳的处理机（视图不持有自己的清单，看的是源实例那份）。"""
        return self._logger.processor_registry

    @property
    def named_routes(self) -> dict[str, BoundLogger]:
        """已发布的具名路由（视图不持有自己的表，看的是源实例那份）。"""
        return self._logger.named_routes

    async def flush(self) -> None:
        """刷新所有出口的缓冲区（视图不持有任何自己的状态，交给源实例做）。"""
        await self._logger.flush()

    async def search(
        self,
        *,
        query: str | None = None,
        level: "LogLevel | str | None" = None,
        start: "TimestampLike" = None,
        end: "TimestampLike" = None,
        logger_name: str | None = None,
        owner_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        processors: "Sequence[str] | None" = None,
    ) -> LogSearchResult:
        """检索：走源实例那份（视图不持有任何自己的状态，查的东西与它无异）。"""
        return await self._logger.search(
            query=query,
            level=level,
            start=start,
            end=end,
            logger_name=logger_name,
            owner_id=owner_id,
            limit=limit,
            offset=offset,
            processors=processors,
        )

    def _merge(
        self, owner_id: str, extra: Mapping[str, object]
    ) -> tuple[str, dict[str, object]]:
        """默认字段并进当次字段：当次同名键压过默认的；``owner_id`` 单独拎出来。"""
        merged: dict[str, object] = {**self._defaults, **extra}
        bound_owner: object = merged.pop("owner_id", "")
        return str(owner_id or bound_owner), merged

    # ------------------------------------------------------------------ 写入
    def is_enabled_for(self, level: LogLevel | str) -> bool:
        """本条日志是否达到本视图的生效级别（没单独绑就看源实例）。"""
        return LogLevel.parse(level) >= self.level

    def write(self, record: LogRecord) -> bool:
        """直接写一条记录：默认字段并进 ``extra``，名字与目标按本视图那份盖上。"""
        patched: LogRecord = record
        if self._name is not None:
            patched = replace(patched, logger_name=self.name)
        if self._targets is not None:
            patched = replace(patched, targets=self._targets)
        if not self._defaults:
            return self._logger.write(patched)
        owner_id, extra = self._merge(patched.owner_id, patched.extra)
        return self._logger.write(replace(patched, extra=extra, owner_id=owner_id))

    def log(
        self,
        level: LogLevel | str,
        message: object,
        *,
        owner_id: str = "",
        exc_info: object = False,
        **extra: object,
    ) -> bool:
        """写一条日志：``extra`` = 默认字段 + 当次字段（当次同名键优先）。"""
        parsed_level = LogLevel.parse(level)
        if not self.is_enabled_for(parsed_level):
            return False
        merged_owner, merged_extra = self._merge(owner_id, extra)
        return             self._logger.write(
            self._logger.new_record(
                parsed_level,
                message,
                owner_id=merged_owner,
                exc_info=exc_info,
                extra=merged_extra,
                logger_name=self.name,
                targets=self._targets,
            )
        )

    def debug(self, message: object, *, owner_id: str = "", **extra: object) -> bool:
        return self.log(LogLevel.DEBUG, message, owner_id=owner_id, **extra)

    def info(self, message: object, *, owner_id: str = "", **extra: object) -> bool:
        return self.log(LogLevel.INFO, message, owner_id=owner_id, **extra)

    def warning(self, message: object, *, owner_id: str = "", **extra: object) -> bool:
        return self.log(LogLevel.WARNING, message, owner_id=owner_id, **extra)

    def error(
        self, message: object, *, owner_id: str = "", exc_info: object = False, **extra: object
    ) -> bool:
        return self.log(LogLevel.ERROR, message, owner_id=owner_id, exc_info=exc_info, **extra)

    def exception(self, message: object, *, owner_id: str = "", **extra: object) -> bool:
        """记录一条 ERROR 日志并附带当前异常堆栈（默认字段照带）。"""
        return self.log(LogLevel.ERROR, message, owner_id=owner_id, exc_info=True, **extra)

    @override
    def __repr__(self) -> str:  # pragma: no cover - 调试用
        keys = ", ".join(self._defaults)
        return f"<BoundLogger name={self.name!r} defaults=[{keys}]>"


class ChildLogger:
    """**扁平化的命名层级视图**：只记「父是谁 + 自己的覆盖项」，写时沿父链现解析。

    与旧版「派生实例」的根本区别：**不缓存、不复制** —— 没有 ``children`` 缓存字典，
    也不把父级的出口 / 级别拷进自己（``_level`` / ``_targets`` 为 ``None`` = 继承）。
    于是：

    * **父级后挂出口、改级别对已派生节点立即生效**：``root.child("a")`` 之后
      ``root.mount(...)`` / ``root.set_level("DEBUG")``，``a`` 写日志照样按新状态解析，
      不存在「先取实例再挂出口」的顺序坑；
    * 每次 :meth:`child` 都返回一份新视图，行为一致，没有缓存要不要失效的烦恼；
    * 写日志只投**解析出的那一份目标**，不沿父链重复投递（无 propagate）。

    child 与 bind 的分工：``child`` 换的是**名字与出口**（名字即身份，可继续
    ``child`` 生长）；``bind`` 换的是**默认字段**（给一段执行打上下文标记，产物
    只能 ``bind`` / ``log`` / ``mute``，不能再 ``child``）。
    """

    __slots__: tuple[str, ...] = ("_root", "_parent", "_name", "_level", "_targets")

    def __init__(
        self,
        root: BaseLogger,
        *,
        name: str,
        parent: "ChildLogger | None" = None,
        level: "LogLevel | str | None" = None,
        targets: "Sequence[Target | BaseLogProcessor] | None" = None,
    ) -> None:
        #: 共享状态的源：队列 / 接纳清单 / root 默认目标，都在这一份上
        self._root: BaseLogger = root
        #: 父节点引用；``None`` = 直接挂在 root 下的第一层
        self._parent: ChildLogger | None = parent
        #: 完整名字（已含 root 前缀），写进 ``record.logger_name``
        self._name: str = name
        #: 自己的级别覆盖；``None`` = 沿父链继承
        self._level: LogLevel | None = None if level is None else LogLevel.parse(level)
        #: 自己的目标覆盖；``None`` = 沿父链继承
        self._targets: tuple[Target, ...] | None = None
        if targets is not None:
            normalized: list[Target] = [
                item if isinstance(item, Target) else Target(item) for item in targets
            ]
            normalized.sort(key=lambda target: target.priority)
            self._targets = tuple(normalized)
            # 只在这份覆盖里出现的处理机也要能被停机 flush、被检索、被统计
            root.adopt(*(target.processor for target in self._targets))

    @property
    def name(self) -> str:
        """完整名字（写进 ``record.logger_name`` 的标签，不是身份）。"""
        return self._name

    @property
    def level(self) -> LogLevel:
        """写时沿父链解析出的生效级别：最近一个显式设置，没有就 root 那份。"""
        node: ChildLogger | None = self
        while node is not None:
            if node._level is not None:
                return node._level
            node = node._parent
        return self._root.level

    @property
    def targets(self) -> tuple[Target, ...]:
        """写时沿父链解析出的目标：最近一份显式指定，没有就 root 当前默认目标。"""
        node: ChildLogger | None = self
        while node is not None:
            if node._targets is not None:
                return node._targets
            node = node._parent
        return self._root.targets

    def child(
        self,
        name: str,
        *,
        level: "LogLevel | str | None" = None,
        targets: "Sequence[Target | BaseLogProcessor] | None" = None,
    ) -> "ChildLogger":
        """继续沿名字生长一层：名字相对本视图拼接，返回**新视图**（无缓存）。

        ``root.child("api").child("robot")`` 与 ``root.child("api.robot")`` 行为等价
        —— 都是写时解析到 root 的当前状态，路径深浅不影响结果。
        """
        full: str = name
        if self._name and name and name != self._name and not name.startswith(self._name + "."):
            full = f"{self._name}.{name}"
        return ChildLogger(self._root, name=full, parent=self, level=level, targets=targets)

    def bind(
        self,
        *,
        name: str | None = None,
        targets: "Sequence[Target | BaseLogProcessor] | None" = None,
        level: "LogLevel | str | None" = None,
        **defaults: object,
    ) -> BoundLogger:
        """产一份 :class:`BoundLogger`：名字 / 出口 / 级别默认沿用本视图**解析值**。

        从这里起是**上下文视图**：只能 ``bind`` / ``log`` / ``mute``，不能再 ``child``
        生长（类型分离，见类 docstring）。
        """
        return BoundLogger(
            self._root,
            name=name if name is not None else self._name,
            targets=targets if targets is not None else self.targets,
            level=level if level is not None else self.level,
            **defaults,
        )

    def set_level(self, level: "LogLevel | str | None") -> None:
        """设自己的级别门槛；传 ``None`` 恢复「沿父链继承」。"""
        self._level = None if level is None else LogLevel.parse(level)

    def is_enabled_for(self, level: LogLevel | str) -> bool:
        """本条日志是否达到本视图的生效级别（写时沿父链解析）。"""
        return LogLevel.parse(level) >= self.level

    def write(self, record: LogRecord) -> bool:
        """直接写一条记录：名字与目标按本视图**解析出的那一份**盖上。"""
        patched: LogRecord = record
        if self._name:
            patched = replace(patched, logger_name=self._name)
        return self._root.write(replace(patched, targets=self.targets))

    def log(
        self,
        level: LogLevel | str,
        message: object,
        *,
        owner_id: str = "",
        exc_info: object = False,
        **extra: object,
    ) -> bool:
        """写一条日志：级别 / 目标在**写入这一刻**沿父链现解析，只投那一份。"""
        parsed_level = LogLevel.parse(level)
        if not self.is_enabled_for(parsed_level):
            return False
        return self._root.write(
            self._root.new_record(
                parsed_level,
                message,
                owner_id=owner_id,
                exc_info=exc_info,
                extra=extra,
                logger_name=self._name,
                targets=self.targets,
            )
        )

    def debug(self, message: object, *, owner_id: str = "", **extra: object) -> bool:
        return self.log(LogLevel.DEBUG, message, owner_id=owner_id, **extra)

    def info(self, message: object, *, owner_id: str = "", **extra: object) -> bool:
        return self.log(LogLevel.INFO, message, owner_id=owner_id, **extra)

    def warning(self, message: object, *, owner_id: str = "", **extra: object) -> bool:
        return self.log(LogLevel.WARNING, message, owner_id=owner_id, **extra)

    def error(
        self, message: object, *, owner_id: str = "", exc_info: object = False, **extra: object
    ) -> bool:
        return self.log(LogLevel.ERROR, message, owner_id=owner_id, exc_info=exc_info, **extra)

    def exception(self, message: object, *, owner_id: str = "", **extra: object) -> bool:
        """记录一条 ERROR 日志并附带当前异常堆栈。"""
        return self.log(LogLevel.ERROR, message, owner_id=owner_id, exc_info=True, **extra)

    async def flush(self) -> None:
        """刷新所有出口的缓冲区（视图不持有状态，交给 root 做）。"""
        await self._root.flush()

    @override
    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ChildLogger {self._name} level={self.level.name}>"
