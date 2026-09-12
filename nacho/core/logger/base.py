"""异步日志系统基类。

职责划分
========

* :meth:`BaseLogger.write`：**写入方法**。默认把日志推到消息队列（非阻塞），
  业务侧永远不会因为落盘 / 落库而卡住；
* 内部分发器从队列批量取日志，扇出给**这条日志所属实例**的处理机；
* :meth:`BaseLogger.flush`：**刷新缓冲区方法**，刷新所有处理机的缓冲区；
* :meth:`BaseLogger.search`：**检索方法**，聚合各处理机的检索结果。

子实例 = 一个名字 + 一份配置副本
================================

:meth:`BaseLogger.child` 派生一个子实例：换一个名字，把**当前实例的处理机列表与
过滤器复制一份**进去，之后两者各改各的：

* 子实例写日志时**只投给自己那份副本**，不会再去父实例现取一遍，也不会沿名字逐层
  累加——所以一条日志在同一个处理机上永远只投一次；
* 副本**创建即冻结**：父实例之后再 ``attach`` / ``detach`` 都不回头影响已经建好的
  子实例；子实例要变就自己 ``attach``；
* 名字按 ``.`` 分层，``child("a.b")`` 等价于 ``child("a").child("b")``：逐段复制，
  于是 ``a.b`` 拿到的是 ``a`` 那份配置的副本。

因此**顺序很重要**：副本在 ``child`` 创建（或第一次 ``get_logger``）时定格，
要先挂出口、再取子实例。

输出的挂载与过滤
================

* :meth:`attach` 把一个处理机挂到**本实例**（或用 ``name`` 指定某个派生实例）上；
* ``log_filter``（:class:`~nacho.core.logger.filters.LogFilter`）挂在出口一侧、由
  分发器持有：一条日志只有通过某个出口的过滤器才会被投递给它，被过滤掉的日志连
  处理机的缓冲区都不进。过滤器因此不属于处理机——处理机只负责落地。

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
from collections.abc import Sequence
from types import TracebackType
from typing import TypedDict, cast, override

from .filters import LogFilter
from .models import LogLevel, LogRecord, TimestampLike
from .processors.base import BaseLogProcessor, ProcessorStats
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
    queue: QueueStats
    processors: list[ProcessorStats]
    dropped: DroppedStats
    #: 「实例名字 -> 该实例那份配置里的处理机名」
    routes: dict[str, list[str]]


def _dedupe(processors: Sequence[BaseLogProcessor]) -> list[BaseLogProcessor]:
    """按 ``id`` 去重保序。

    一份配置里同一个处理机只能出现一次：复制父实例配置、重复挂载等都可能带来
    重复项，绝不能因为「多了一份」就变成对同一个出口重复输出。
    """
    unique: list[BaseLogProcessor] = []
    seen: set[int] = set()
    for processor in processors:
        if id(processor) not in seen:
            seen.add(id(processor))
            unique.append(processor)
    return unique


class _SharedState:
    """一个核心实例与它派生出的所有子实例共享的运行时状态。

    队列与分发器归**核心实例**（:attr:`owner`）所有，子实例只借用：
    :attr:`instances` 把「名字 -> 日志实例」登记成一张分发表，分发器据此找到
    一条日志该投给谁；:attr:`registry` 是所有已挂载处理机的去重清单，
    供生命周期（start/stop）、刷新、检索与统计使用。
    """

    def __init__(self, queue: AsyncLogQueue) -> None:
        self.queue: AsyncLogQueue = queue
        #: 拥有队列与分发器的核心实例（子实例不许自己 start）
        self.owner: BaseLogger | None = None
        #: 名字 -> 日志实例（子实例创建时登记；同名永远同一个实例）
        self.instances: dict[str, BaseLogger] = {}
        #: 所有已挂载处理机的去重清单（生命周期 / 刷新 / 检索 / 统计）
        self.registry: list[BaseLogProcessor] = []


def _require_processor(processor: object) -> None:
    """运行期校验：不是 :class:`BaseLogProcessor` 的实例时抛出 ``TypeError``。

    形参特意声明为宽类型 ``object`` 而非 ``BaseLogProcessor``：``attach`` 的形参类型
    本就该在静态检查阶段拦住传错的调用方，但配置解析 / 反射 / 无类型代码等**动态调用**
    会绕过静态检查，这道运行期防线必须真正可达（若按真实类型标注，静态分析会把
    ``isinstance`` 判为恒真、把 ``raise`` 当成不可达代码），才能给出明确报错
    （而不是把错误对象登记进分发表、直到分发时才崩）。

    只校验、不返回值：调用方原样使用自己的变量，避免「参数被同名赋值遮蔽」
    （``reportRedeclaration``）。
    """
    if not isinstance(processor, BaseLogProcessor):
        raise TypeError(f"处理机必须是 BaseLogProcessor 子类: {type(processor)!r}")


def _require_filter(log_filter: object) -> None:
    """运行期校验：不是 :class:`LogFilter` 的实例时抛出 ``TypeError``（理由同
    :func:`_require_processor`）。"""
    if not isinstance(log_filter, LogFilter):
        raise TypeError(f"过滤器必须是 LogFilter 子类: {type(log_filter)!r}")


class BaseLogger:
    """日志系统基类：队列 + 分发器 + 可注入的日志处理机。

    实例自己持有一份处理机配置（:attr:`processors`）与过滤器，
    :meth:`child` 派生出的子实例拿的是**这份配置的副本**，创建即冻结。
    """

    def __init__(
        self,
        name: str = "nacho",
        *,
        level: LogLevel | str = LogLevel.INFO,
        queue: AsyncLogQueue | None = None,
        processors: list[BaseLogProcessor] | None = None,
        filters: dict[str, LogFilter] | None = None,
        shared: _SharedState | None = None,
        overflow_policy: OverflowPolicy | str = OverflowPolicy.DROP_OLDEST,
        queue_maxsize: int = 10000,
        dispatch_batch_size: int = 200,
        dispatch_timeout: float = 0.2,
    ) -> None:
        """
        :param processors: 本实例的处理机列表。构造时**复制一份**，
            因此传进来的列表之后被改动不会影响本实例（子实例的配置副本也依赖这一点）。
        :param filters: 本实例的「处理机名 -> 过滤器」表；同样复制一份。
        :param shared: 多实例共享的运行时状态。只有核心实例才新建它，
            子实例一律传入父实例的 ``_shared``。
        :param dispatch_batch_size: 分发器一次最多从队列取多少条。这是**交接批量**，
            不是攒批水位线——攒批由各处理机的 ``buffer_size`` 决定。
        :param dispatch_timeout: 队列取不到新日志时，最多再等多久就把手上这批先交出去。
            这是**交接窗口**，不是攒批窗口：取小值（默认 0.2s）让日志尽快交到处理机，
            避免和处理器缓冲区的 ``buffer_size`` / ``flush_interval`` 形成两套互相
            打架的攒批配方。
        """
        self.name: str = name
        self._level: LogLevel = LogLevel.parse(level)
        if shared is None:
            # 注意：这里必须用 is not None 判断，AsyncLogQueue 实现了 __len__，
            # 空队列在布尔上下文中为 False，写成 ``queue or AsyncLogQueue(...)``
            # 会在队列恰好为空时错误地新建一个队列。
            shared = _SharedState(
                queue
                if queue is not None
                else AsyncLogQueue(
                    maxsize=queue_maxsize, overflow_policy=overflow_policy
                )
            )
            # 新建共享状态的那个实例就是核心：队列与分发器归它所有
            shared.owner = self
        #: 与子实例共享的运行时状态（队列 / 分发表 / 处理机清单）
        self._shared: _SharedState = shared
        self._queue: AsyncLogQueue = shared.queue
        # 本实例自己的配置：构造时复制，子实例的「配置副本」也由这里复制出来
        self._processors: list[BaseLogProcessor] = _dedupe(processors or [])
        self._filters: dict[str, LogFilter] = dict(filters or {})
        self._dispatch_batch_size: int = dispatch_batch_size
        self._dispatch_timeout: float = dispatch_timeout
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._running: bool = False
        self._shared.instances[name] = self
        self._sync_registry()

    # ------------------------------------------------------------------ 注册表
    def _sync_registry(self) -> None:
        """按 :attr:`_SharedState.instances` 重建处理机清单（去重保序、原地更新）。"""
        merged: list[BaseLogProcessor] = []
        for instance in self._shared.instances.values():
            for processor in instance._processors:
                if not any(existing is processor for existing in merged):
                    merged.append(processor)
        self._shared.registry[:] = merged

    def _find_own(self, name: str) -> BaseLogProcessor | None:
        """在本实例自己的配置里按名称找处理机。"""
        for processor in self._processors:
            if processor.name == name:
                return processor
        return None

    # ------------------------------------------------------------------ 输出通道挂载
    def attach(
        self,
        processor: BaseLogProcessor,
        *,
        name: str | None = None,
        log_filter: LogFilter | None = None,
        replace: bool = False,
    ) -> BaseLogProcessor:
        """挂载一个输出通道（处理机），**运行期挂载同样生效**。

        挂载本身是同步且廉价的（只做登记）；若日志系统已在运行，该通道会在
        下一次分发前被自动启动（打开文件、建表等），所以调用方不需要再手动
        ``await processor.start()``。

        :param name: 挂到哪一层的配置上：``None`` 表示**本实例**（推荐——先取实例
            再挂载，语义最直白）；给了名字则等价于挂到 ``self.child(name)`` 上，
            该名字的子实例之后派生时会复制到它。名字**相对本实例**（``"a.b"`` 即
            ``"<本实例名>.a.b"``，写全名也行）。
        :param log_filter: 这个通道的过滤器（:class:`~nacho.core.logger.filters.LogFilter`）。
            ``None`` 表示全收；给了过滤器则**由分发器在查找分发时**用它筛掉不
            该进本通道的日志——过滤器属于分发侧，与处理机无关。
        :param replace: 同名通道已存在时是否替换（换输出路径 / 模块热重载时用）。
        :raises TypeError: ``processor`` 不是 :class:`BaseLogProcessor` 子类，
            或 ``log_filter`` 不是 :class:`LogFilter` 子类。
        :raises ValueError: 目标实例上已有同名通道且 ``replace=False``。
        """
        _require_processor(processor)
        if log_filter is not None:
            _require_filter(log_filter)
        target: BaseLogger = self if name is None else self.child(name)
        existing: BaseLogProcessor | None = target._find_own(processor.name)
        if existing is not None:
            if not replace:
                raise ValueError(
                    f"处理机 {processor.name!r} 已挂载；"
                    + "要换输出路径请用 attach(processor, replace=True)"
                )
            target._processors.remove(existing)
            target._filters.pop(existing.name, None)
        target._processors.append(processor)
        if log_filter is None:
            target._filters.pop(processor.name, None)  # pyright: ignore[reportUnusedCallResult]
        else:
            target._filters[processor.name] = log_filter
        self._sync_registry()
        return processor

    def attach_many(self, *processors: BaseLogProcessor) -> list[BaseLogProcessor]:
        """批量挂载输出通道（全部挂到本实例）。"""
        return [self.attach(processor) for processor in processors]

    def detach(self, name: str) -> BaseLogProcessor | None:
        """按名称卸载输出通道，返回被卸载的处理机；不存在则返回 ``None``。

        卸载是**全局**的：该处理机会从所有实例的配置里摘掉（含子实例复制来的那份），
        否则被卸载的出口还会继续收到日志。冲刷余量与关闭后端由调用方决定
        （``await processor.stop()`` 会先刷完余量再关闭）。
        """
        found: BaseLogProcessor | None = None
        for instance in self._shared.instances.values():
            for processor in list(instance._processors):
                if processor.name == name:
                    instance._processors.remove(processor)
                    found = processor
            instance._filters.pop(name, None)  # pyright: ignore[reportUnusedCallResult]
        if found is not None:
            self._sync_registry()
        return found

    def get_processor(self, name: str) -> BaseLogProcessor | None:
        """按名称取已挂载的输出通道（在所有实例的配置里找）。"""
        for processor in self._shared.registry:
            if processor.name == name:
                return processor
        return None

    # 兼容旧名：``inject`` 系列即 ``attach`` 系列
    def inject(self, processor: BaseLogProcessor, *, replace: bool = False) -> BaseLogProcessor:
        """兼容旧名，等价于 :meth:`attach`。"""
        return self.attach(processor, replace=replace)

    def inject_many(self, *processors: BaseLogProcessor) -> list[BaseLogProcessor]:
        """兼容旧名，等价于 :meth:`attach_many`。"""
        return self.attach_many(*processors)

    def remove(self, name: str) -> bool:
        """兼容旧名，等价于 ``detach(name) is not None``。"""
        return self.detach(name) is not None

    @property
    def processors(self) -> list[BaseLogProcessor]:
        """本实例配置里的处理机快照副本。"""
        return list(self._processors)

    @property
    def processor_registry(self) -> list[BaseLogProcessor]:
        """全部实例已挂载处理机的清单本身（请勿直接修改）。

        与 :attr:`processors` 的区别：这是所有派生实例的并集，
        生命周期与刷新 / 检索都按它来。
        """
        return self._shared.registry

    @property
    def filters(self) -> dict[str, LogFilter]:
        """本实例的「处理机名 -> 过滤器」快照副本。"""
        return dict(self._filters)

    # ------------------------------------------------------------------ 派生实例
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

    def _derive(self, full_name: str) -> BaseLogger:
        """按完整名字派生**一层**子实例（已存在则直接返回）。

        子实例构造时把本实例的处理机列表与过滤器**复制一份**，副本一到手即冻结。
        """
        existing = self._shared.instances.get(full_name)
        if existing is not None:
            return existing
        return BaseLogger(
            full_name,
            level=self._level,
            processors=self._processors,
            filters=self._filters,
            shared=self._shared,
            dispatch_batch_size=self._dispatch_batch_size,
            dispatch_timeout=self._dispatch_timeout,
        )

    def child(self, name: str, *, level: LogLevel | str | None = None) -> BaseLogger:
        """派生一个子日志实例：共享队列与分发器，换一个名字 + 复制一份配置。

        名字**相对本实例**：``LogCore("nacho").child("a1")`` 得到的名字是 ``nacho.a1``——
        写相对的一段（``"a1"``、``"robot.arm"``）会自动补上父前缀；已经写全的名字
        （``"nacho.a1"``）原样使用，两种写法可以混用（见 :meth:`qualify`）。名字分段
        逐层复制：``child("robot.arm")`` 等价于 ``child("robot").child("arm")``，
        因此 ``arm`` 拿到的是 ``robot`` 那份配置的副本。

        子实例与父实例共享同一个队列与同一个分发器；**配置是复制的**：创建那一刻把父
        实例的处理机与过滤器复制一份进自己（副本随即冻结），此后父实例再 ``attach`` /
        ``detach`` 都不回头影响它；要改就自己 :meth:`attach`。同名实例只有一个
        （有则载入），重复调用返回同一个对象。

        :param level: 显式指定时只改**这个实例**的级别（新级别会成为它之后派生子实例的
            副本来源）；省略则沿用父实例的级别。级别同样复制不回溯，所以父实例之后
            :meth:`set_level` 不影响已经建好的子实例。
        :raises ValueError: ``name`` 为空。

        子实例**不要**自己 ``start()``：分发器与队列归核心实例所有，重复启动只会
        多出一个抢同一队列的分发器。
        """
        if not name:
            raise ValueError("child 名字不能为空")
        full: str = self.qualify(name)
        if full == self.name:
            if level is not None:
                self.set_level(level)
            return self

        # 逐段派生，保证每一层复制的是它**上一层**的配置
        remainder = full[len(self.name) + 1 :] if self.name else full
        node: BaseLogger = self
        prefix = self.name
        for part in remainder.split("."):
            prefix = f"{prefix}.{part}" if prefix else part
            node = node._derive(prefix)
        if level is not None:
            node.set_level(level)
        return node

    @property
    def routes(self) -> dict[str, list[BaseLogProcessor]]:
        """「实例名字 -> 该实例配置里的处理机」快照副本（改它不会影响路由）。"""
        return {
            name: list(instance._processors)
            for name, instance in self._shared.instances.items()
        }

    @property
    def queue(self) -> AsyncLogQueue:
        return self._queue

    @property
    def level(self) -> LogLevel:
        """本实例的级别。"""
        return self._level

    def set_level(self, level: LogLevel | str) -> None:
        """改**本实例**的级别。

        级别是配置的一部分，同样「复制不回溯」：之后的子实例会复制到新级别，
        已经建好的子实例维持自己那份不变。
        """
        self._level = LogLevel.parse(level)

    def effective_level(self, name: str | None = None) -> LogLevel:
        """某个名字的**生效级别**：该名字对应实例的级别，没有实例则用本实例的。

        ``name`` 默认取本实例的名字。注意级别是复制来的、不回溯——父实例之后
        :meth:`set_level` 不会改变已经建好的子实例。
        """
        if name is None:
            return self._level
        instance = self._shared.instances.get(self.qualify(name))
        return self._level if instance is None else instance._level

    def effective_outputs(self, name: str | None = None) -> list[BaseLogProcessor]:
        """某个名字**会收到的输出设备**（即该名字实例那份配置副本）。

        只读，不会顺带把实例建出来：实例还没派生过时，返回本实例的配置，
        因为「现在派生一个」拿到的就是这份。排查「这条日志到底进了哪几个出口」
        看这个；:attr:`routes` 则是所有实例配置的总览。
        """
        if name is None:
            return list(self._processors)
        instance = self._shared.instances.get(self.qualify(name))
        return list(self._processors if instance is None else instance._processors)

    @property
    def running(self) -> bool:
        """分发器是否在运行（即 :meth:`start` 是否已生效）。"""
        return self._running

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> BaseLogger:
        """启动所有处理机与内部分发器。

        :raises RuntimeError: 在子实例上调用。队列与分发器归核心实例所有，
            子实例重复启动只会多出一个抢同一队列的分发器。
        """
        owner = self._shared.owner
        if owner is not self:
            raise RuntimeError(
                f"子日志实例 {self.name!r} 不持有分发器，"
                f"请对核心实例 {(owner.name if owner is not None else '?')!r} 调用 start()"
            )
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
                self._dispatcher_task.cancel()  # pyright: ignore[reportUnusedCallResult]
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

    def _targets_for(self, record: LogRecord) -> list[BaseLogProcessor]:
        """一条日志该投给哪些处理机：只看**它所属实例**那份配置副本，再按过滤器筛。

        不会沿名字向上回溯：子实例写日志时用的就是它自己那份（从父实例复制来、
        创建即冻结）的配置，因此同一条日志在同一个处理机上永远只投一次。
        名字没登记过实例（例如两个核心共用一个队列）时退回本实例的配置。
        """
        instance = self._shared.instances.get(record.logger_name)
        processors = self._processors if instance is None else instance._processors
        filters = self._filters if instance is None else instance._filters
        accepted: list[BaseLogProcessor] = []
        for processor in processors:
            log_filter = filters.get(processor.name)
            if log_filter is None or log_filter.match(record):
                accepted.append(processor)
        return accepted

    async def _dispatch(self, records: list[LogRecord]) -> None:
        # 按记录所属实例解析去向，再按处理机归并成批，一次喂给同一个处理机
        batches: dict[int, tuple[BaseLogProcessor, list[LogRecord]]] = {}
        for record in records:
            for processor in self._targets_for(record):
                if not processor.healthy:
                    continue
                entry = batches.get(id(processor))
                if entry is None:
                    batches[id(processor)] = (processor, [record])
                else:
                    entry[1].append(record)

        for processor, batch in batches.values():
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
        """本条日志是否达到**本实例**的生效级别。"""
        return LogLevel.parse(level) >= self.effective_level()

    def write(self, record: LogRecord) -> bool:
        """写入方法：默认把日志推到消息队列（非阻塞），返回是否入队成功。"""
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
        exc_info: object = False,
        **extra: object,
    ) -> bool:
        """构造日志记录并写入队列。

        ``exc_info`` 收口为 ``object``：既允许 ``True``（用当前异常），也允许
        ``sys.exc_info()`` 那样的 ``(type, value, traceback)`` 三元组；层间用
        ``**extra`` 透传时也只有宽类型才放得下，运行期再按元组 / 布尔分支处理。
        """
        parsed_level = LogLevel.parse(level)
        if not self.is_enabled_for(parsed_level):
            return False

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

        record = LogRecord(
            message=str(message),
            level=parsed_level,
            logger_name=self.name,
            extra=extra,
            exc_text=exc_text,
        )
        return self.write(record)

    def debug(self, message: object, **extra: object) -> bool:
        return self.log(LogLevel.DEBUG, message, **extra)

    def info(self, message: object, **extra: object) -> bool:
        return self.log(LogLevel.INFO, message, **extra)

    def warning(self, message: object, **extra: object) -> bool:
        return self.log(LogLevel.WARNING, message, **extra)

    def error(self, message: object, *, exc_info: object = False, **extra: object) -> bool:
        return self.log(LogLevel.ERROR, message, exc_info=exc_info, **extra)

    def critical(self, message: object, *, exc_info: object = False, **extra: object) -> bool:
        return self.log(LogLevel.CRITICAL, message, exc_info=exc_info, **extra)

    def exception(self, message: object, **extra: object) -> bool:
        """记录一条 ERROR 日志并附带当前异常堆栈。"""
        return self.log(LogLevel.ERROR, message, exc_info=True, **extra)

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
        limit: int = 100,
        offset: int = 0,
        processors: Sequence[str] | None = None,
    ) -> list[LogRecord]:
        """检索方法：聚合所有（或指定）处理机的检索结果，按时间倒序返回。"""
        targets = self._select_processors(processors)
        if not targets:
            return []

        results = await asyncio.gather(
            *(
                processor.search(
                    query=query,
                    level=level,
                    start=start,
                    end=end,
                    logger_name=logger_name,
                    limit=limit + offset,
                    offset=0,
                )
                for processor in targets
            ),
            return_exceptions=True,
        )

        merged: list[LogRecord] = []
        for result in results:
            if isinstance(result, BaseException):
                _fallback.exception("检索处理机失败", exc_info=result)
                continue
            merged.extend(result)

        merged.sort(key=lambda item: item.timestamp, reverse=True)

        # 多个处理机可能存有同一条日志（同一 record_id），按 id 去重
        deduped: list[LogRecord] = []
        seen: set[str] = set()
        for record in merged:
            if record.record_id in seen:
                continue
            seen.add(record.record_id)
            deduped.append(record)
        return deduped[offset : offset + limit]

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
            "routes": {
                name: [processor.name for processor in instance._processors]
                for name, instance in sorted(self._shared.instances.items())
            },
        }

    @override
    def __repr__(self) -> str:  # pragma: no cover - 调试用
        names = ", ".join(p.name for p in self._processors)
        return f"<BaseLogger name={self.name!r} level={self._level.name} processors=[{names}]>"
