"""异步日志系统基类。

职责划分
========

* :meth:`BaseLogger.write`：**写入方法**。默认把日志推到消息队列（非阻塞），
  业务侧永远不会因为落盘 / 落库而卡住；
* :meth:`BaseLogger.bind`：**默认字段**。得到一份「每条日志都自动带上某几个键」的视图
  （:class:`BoundLogger`），适合给一段执行（一趟工作流、一次请求）统一打上下文标记，
  不用每个调用点手抄一遍；
* 内部分发器从队列批量取日志，扇出给**这条日志所属实例**的处理机；
* :meth:`BaseLogger.flush`：**刷新缓冲区方法**，刷新所有处理机的缓冲区；
* :meth:`BaseLogger.search`：**检索方法**，聚合各处理机的检索结果。

子实例 = 一个名字 + 一份「落回配置」+ 自己的出口
================================================

:meth:`BaseLogger.child` 派生一个子实例：换一个名字，并把**当前实例实际生效的
处理机列表与过滤器复制一份**作为自己的「落回配置」（``_inherited``），之后两者各改各的：

* **自层覆盖**：子实例一旦自己 ``attach`` 过出口，写日志就**只投自层那些**，不再带上
  父级 / 核心的文件出口（这就是「一个模块一个文件」）；标了
  :attr:`~nacho.core.logger.processors.base.BaseLogProcessor.inherit_on_override`
  的出口（控制台）例外，仍从落回配置里保留；
* **无自层出口就回落**：子实例没挂过任何出口时，整份走落回配置——像 ``arm`` 这种
  没单独挂文件的名字，照旧写进核心的 ``nacho.log``；
* 落回配置**创建即冻结**：父实例之后再 ``attach`` / ``detach`` 都不回头影响已经建好的
  子实例；子实例要变就自己 ``attach``；
* 名字按 ``.`` 分层，``child("a.b")`` 等价于 ``child("a").child("b")``：逐段派生，
  于是 ``a.b`` 的落回配置是 ``a`` 那一份。

因为「自层覆盖」，写日志**只按记录所属实例自己那份解析结果投递**，不会沿名字向上
回溯、也不会重复投给同一个处理机。

因此**顺序很重要**：落回配置在 ``child`` 创建（或第一次 ``get_logger``）时定格，
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
from collections.abc import Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType, TracebackType
from typing import TypedDict, cast, override

from .filters import DENY_ALL, LogFilter
from .models import LogLevel, LogRecord, LogSearchResult, TimestampLike
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
    #: 「实例名字 -> 该实例解析后会投的处理机名」
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

    实例自己持有一份「自层出口」（``_own``）与从父实例继承来的「落回配置」
    （``_inherited``）：:meth:`child` 派生出的子实例在**没挂自层出口**时整份走
    落回配置（创建即冻结）；一旦挂了自层出口就改为**只投自层那些**，仅
    ``inherit_on_override`` 的出口（控制台）仍保留。
    """

    def __init__(
        self,
        name: str = "nacho",
        *,
        level: LogLevel | str = LogLevel.INFO,
        queue: AsyncLogQueue | None = None,
        processors: list[BaseLogProcessor] | None = None,
        filters: dict[str, LogFilter] | None = None,
        inherit: list[BaseLogProcessor] | None = None,
        shared: _SharedState | None = None,
        overflow_policy: OverflowPolicy | str = OverflowPolicy.DROP_OLDEST,
        queue_maxsize: int = 10000,
        dispatch_batch_size: int = 200,
        dispatch_timeout: float = 0.2,
    ) -> None:
        """
        :param processors: 本实例**自层**的处理机列表。构造时复制一份，
            因此传进来的列表之后被改动不会影响本实例。自层列表非空即进入
            「自层覆盖」，只投自层这些。
        :param filters: 本实例的「处理机名 -> 过滤器」表；同样复制一份。
        :param inherit: 从父实例继承来的**落回配置**（父实例派生那一刻生效的处理机
            列表的副本）。只有 :meth:`child` 派生出的子实例才传；自层没挂出口时
            整份投给它，自层挂了出口时只保留其中 ``inherit_on_override`` 的出口。
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
        # 本实例**自层**挂载的出口；子实例派生时这份会被复制成对方的「落回配置」
        self._own: list[BaseLogProcessor] = _dedupe(processors or [])
        # 从父实例继承来的**落回配置**（自层没挂出口时整份投它）
        self._inherited: list[BaseLogProcessor] = _dedupe(inherit or [])
        self._filters: dict[str, LogFilter] = dict(filters or {})
        self._dispatch_batch_size: int = dispatch_batch_size
        self._dispatch_timeout: float = dispatch_timeout
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._running: bool = False
        self._shared.instances[name] = self
        self._sync_registry()

    # ------------------------------------------------------------------ 注册表
    def _sync_registry(self) -> None:
        """重建所有已挂载处理机的清单（去重保序、原地更新）。

        每个处理机都源自某个实例的**自层**出口，所以遍历各实例的 ``_own`` 求并集
        即可覆盖全部；落回配置里的那些对象也都来自某一层的 ``_own``。
        """
        merged: list[BaseLogProcessor] = []
        for instance in self._shared.instances.values():
            for processor in instance._own:
                if not any(existing is processor for existing in merged):
                    merged.append(processor)
        self._shared.registry[:] = merged

    def _resolved_outputs(self) -> list[BaseLogProcessor]:
        """本实例**实际会投递**的处理机（自层覆盖 + 无自层出口时回落父级）。

        * 自层挂过出口（``_own`` 非空）：只投自层那些，不再带上父级 / 核心的文件
          出口；标了 ``inherit_on_override`` 的出口（控制台）仍从落回配置里保留；
        * 自层一个出口都没挂：整份走落回配置（父实例派生那一刻生效的那份副本）。
        """
        if not self._own:
            return list(self._inherited)
        resolved: list[BaseLogProcessor] = []
        for processor in self._inherited:
            if processor.inherit_on_override:
                resolved.append(processor)
        for processor in self._own:
            if not any(existing is processor for existing in resolved):
                resolved.append(processor)
        return resolved

    def _find_channel(self, name: str) -> BaseLogProcessor | None:
        """在本实例**实际会投**的出口里按名称找（含保留的继承出口）。"""
        for processor in self._resolved_outputs():
            if processor.name == name:
                return processor
        return None

    def _remove_channel(self, processor: BaseLogProcessor) -> None:
        """从本实例的配置里摘掉一个出口（自层与落回配置里都摘）。"""
        for bucket in (self._own, self._inherited):
            for existing in list(bucket):
                if existing is processor:
                    bucket.remove(existing)

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
            再挂载，语义最直白）；给了名字则等价于挂到 ``self.child(name)`` 上，之后
            该名字实例写日志就只投这份自层出口（覆盖掉它从父级继承来的落回配置），
            它的子实例派生时复制的也是这份。名字**相对本实例**（``"a.b"`` 即
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
        existing: BaseLogProcessor | None = target._find_channel(processor.name)
        if existing is not None:
            if not replace:
                raise ValueError(
                    f"处理机 {processor.name!r} 已挂载；"
                    + "要换输出路径请用 attach(processor, replace=True)"
                )
            target._remove_channel(existing)
            target._filters.pop(existing.name, None)
        target._own.append(processor)
        if processor.inherit_on_override and self._shared.owner is target:
            # 全局留存出口（控制台 / 落库）允许运行期后挂：落回配置本来在子实例派生
            # 那一刻就冻结，不补这一下的话，晚于业务模块取 logger 才挂上的全局出口
            # （如数据库引擎就绪后才挂的落库出口）永远收不到那些模块的日志。
            # 仅当出口挂在**核心根实例**上时向全树补；挂到某个子实例（attach_mount
            # 的模块专属出口）不传播，保持「一个模块一个出口」的隔离语义。
            for instance in self._shared.instances.values():
                if instance is target:
                    continue
                if not any(existing is processor for existing in instance._inherited):
                    instance._inherited.append(processor)
        if log_filter is None:
            target._filters.pop(processor.name, None)
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
            for bucket in (instance._own, instance._inherited):
                for processor in list(bucket):
                    if processor.name == name:
                        bucket.remove(processor)
                        found = processor
            instance._filters.pop(name, None)
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
        """本实例**实际会投递**的处理机快照副本（自层 + 保留的继承出口）。"""
        return self._resolved_outputs()

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

    # ------------------------------------------------------------------ 通道静音
    def mute(self, processor_name: str) -> None:
        """让**本实例**的日志不再投给某个输出通道（含继承来的全局留存出口）。

        给该通道在这份实例的配置上挂一个「全拒」过滤器（:data:`~nacho.core.logger.filters.DENY_ALL`）：
        路由照常解析、出口照常在别处工作，只是本实例的每条日志都过不了这道闸。
        典型用途：落库出口是全局留存出口（``inherit_on_override``），会跟着落回配置进到
        每一路日志；某一路（如访问日志）只配给人翻文件，就用它把库通道堵上，
        「什么时间干了什么」的审计事件才不会被逐条请求的流水淹掉。

        与 :meth:`detach` 的区别：detach 把出口从**所有**实例上摘掉（全局下线）；
        mute 只关**本实例**这一路，别处照常收。重复调用无害；对该实例根本没有的通道
        调用也 harmless——将来就算这个通道经落回配置传进来，也会被这道闸拦住。
        """
        self._filters[processor_name] = DENY_ALL

    def unmute(self, processor_name: str) -> None:
        """解除 :meth:`mute`：恢复本实例对该出口的正常投递。"""
        self._filters.pop(processor_name, None)

    @property
    def muted(self) -> list[str]:
        """被 :meth:`mute` 静音的通道名（快照副本；状态可查，「这条日志怎么没进库」少翻一层）。"""
        return [name for name, log_filter in self._filters.items() if log_filter is DENY_ALL]

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

        子实例构造时把本实例**实际会投的处理机**与过滤器各复制一份，作为自己的
        「落回配置」（``_inherited``）：自层没挂出口时整份投它，挂了自层出口则只保留
        其中 ``inherit_on_override`` 的部分。副本一到手即冻结。
        """
        existing = self._shared.instances.get(full_name)
        if existing is not None:
            return existing
        return BaseLogger(
            full_name,
            level=self._level,
            filters=self._filters,
            inherit=self._resolved_outputs(),
            shared=self._shared,
            dispatch_batch_size=self._dispatch_batch_size,
            dispatch_timeout=self._dispatch_timeout,
        )

    def child(self, name: str, *, level: LogLevel | str | None = None) -> BaseLogger:
        """派生一个子日志实例：共享队列与分发器，换一个名字 + 复制一份落回配置。

        名字**相对本实例**：``LogCore("nacho").child("a1")`` 得到的名字是 ``nacho.a1``——
        写相对的一段（``"a1"``、``"robot.arm"``）会自动补上父前缀；已经写全的名字
        （``"nacho.a1"``）原样使用，两种写法可以混用（见 :meth:`qualify`）。名字分段
        逐层派生：``child("robot.arm")`` 等价于 ``child("robot").child("arm")``，
        因此 ``arm`` 的落回配置是 ``robot`` 那一份。

        子实例与父实例共享同一个队列与同一个分发器；**配置是派生那一刻的副本**：创建时
        把父实例**实际会投的处理机与过滤器**复制进自己的落回配置（副本随即冻结），此后
        父实例再 ``attach`` / ``detach`` 都不回头影响它。子实例自己 :meth:`attach` 了出口
        之后进入「自层覆盖」——只投自层那些，不再带上父级的文件出口（控制台等
        ``inherit_on_override`` 的出口除外）；没挂自层出口时才整份走落回配置。同名实例
        只有一个（有则载入），重复调用返回同一个对象。

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

    def bind(self, **defaults: object) -> BoundLogger:
        """派生一份**带默认字段**的视图：之后每条日志自动带上这几个键。

        与 :meth:`child` 的分工：``child`` 换的是**名字与出口**（派生一个新实例），
        ``bind`` 换的是**每条日志默认带什么**（一个轻视图，共享本实例的队列、出口与级别）。
        一段执行用它一次打上上下文标记就够了，后面每个调用点只管写自己那句话 —— 详见
        :class:`BoundLogger`。

        :param defaults: 默认字段（键值对）。``owner_id`` 也是可绑的一等字段，其余进
            ``extra``；当次调用传了同名键就按当次的。
        """
        return BoundLogger(self, **defaults)

    @property
    def routes(self) -> dict[str, list[BaseLogProcessor]]:
        """「实例名字 -> 该实例**实际会投**的处理机」快照副本（改它不会影响路由）。"""
        return {
            name: instance._resolved_outputs()
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
        """某个名字**会收到的输出设备**（该名字实例解析后的那份配置）。

        只读，不会顺带把实例建出来：实例还没派生过时，返回本实例的解析结果，
        因为「现在派生一个」自层没挂出口时拿到的就是这份落回配置。排查「这条日志
        到底进了哪几个出口」看这个；:attr:`routes` 则是所有实例的总览。
        """
        if name is None:
            return self._resolved_outputs()
        instance = self._shared.instances.get(self.qualify(name))
        if instance is None:
            return self._resolved_outputs()
        return instance._resolved_outputs()

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

    def _route_of(self, logger_name: str) -> tuple[list[BaseLogProcessor], dict[str, LogFilter]]:
        """某个名字**投给谁 + 各出口挂什么过滤器**：只由名字决定，与记录内容无关。

        名字没登记过实例（例如两个核心共用一个队列）时退回本实例的解析结果。
        正因为目标是「按名字」而不是「按记录」的，一批日志可以同名分组、
        每组只解析一次（见 :meth:`_dispatch`）。
        """
        instance: BaseLogger | None = self._shared.instances.get(logger_name)
        if instance is None:
            return self._resolved_outputs(), self._filters
        return instance._resolved_outputs(), instance._filters

    def _targets_for(self, record: LogRecord) -> list[BaseLogProcessor]:
        """一条日志该投给哪些处理机：先看它所属实例解析后的配置，再按过滤器筛。

        不会沿名字向上回溯：子实例写日志时用的是它自己那份（自层覆盖，或自层没挂
        出口时回落父级的落回配置），因此同一条日志在同一个处理机上永远只投一次。
        """
        processors, filters = self._route_of(record.logger_name)
        accepted: list[BaseLogProcessor] = []
        for processor in processors:
            log_filter = filters.get(processor.name)
            if log_filter is None or log_filter.match(record):
                accepted.append(processor)
        return accepted

    async def _dispatch(self, records: list[LogRecord]) -> None:
        """一批日志扇出给各处理机：**按名字分组**，每组只解析一次目标。

        同一批日志常常来自同一个名字（一次业务调用里连着写的那几条），而目标
        （``processors`` + ``filters``）只由名字决定 —— 所以先分组再解析，省掉
        每条一次「重建目标列表」；逐条做的只剩过滤器判定（那确实要看记录内容）。

        之后再按处理机归并成批，一次喂给同一个处理机。
        """
        grouped: dict[str, list[LogRecord]] = {}
        for record in records:
            bucket = grouped.get(record.logger_name)
            if bucket is None:
                grouped[record.logger_name] = [record]
            else:
                bucket.append(record)

        batches: dict[int, tuple[BaseLogProcessor, list[LogRecord]]] = {}
        for name, group in grouped.items():
            processors, filters = self._route_of(name)
            for record in group:
                for processor in processors:
                    if not processor.healthy:
                        continue
                    log_filter = filters.get(processor.name)
                    if log_filter is not None and not log_filter.match(record):
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
            owner_id=owner_id,
        )
        return self.write(record)

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

    def critical(
        self, message: object, *, owner_id: str = "", exc_info: object = False, **extra: object
    ) -> bool:
        return self.log(LogLevel.CRITICAL, message, owner_id=owner_id, exc_info=exc_info, **extra)

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
                name: [processor.name for processor in instance._resolved_outputs()]
                for name, instance in sorted(self._shared.instances.items())
            },
        }

    @override
    def __repr__(self) -> str:  # pragma: no cover - 调试用
        names = ", ".join(p.name for p in self._resolved_outputs())
        return f"<BaseLogger name={self.name!r} level={self._level.name} processors=[{names}]>"


class BoundLogger:
    """**带默认字段**的日志视图：每条日志自动并上构造时定的那几个键。

    由 :meth:`BaseLogger.bind` 得到。它**不是另一个通道**：与源实例共享同一个队列、
    同一份出口与同一个级别，也不进实例注册表 —— ``routes`` / ``stats`` 里看不到它，
    ``get_logger`` 也拿不到它（同名实例仍然只有一个）。多出来的只有「默认带什么」。

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

    __slots__: tuple[str, str] = ("_logger", "_defaults")

    def __init__(self, logger: BaseLogger, **defaults: object) -> None:
        self._logger: BaseLogger = logger
        #: 默认字段本身也是**只读视图**：视图之间可以纵向叠加、横向共享同一份字典，
        #: 谁都改不到别人的那一层 —— 多个协程共用一份（一趟工作流里大家拿同一个
        #: ``ctx.logger``）才安全。
        self._defaults: Mapping[str, object] = MappingProxyType(dict(defaults))

    @property
    def name(self) -> str:
        """底下的实例名（与源实例同名：视图不换名字）。"""
        return self._logger.name

    @property
    def defaults(self) -> Mapping[str, object]:
        """这份视图的默认字段（只读：改它不影响视图，要改就再 ``bind`` 一层）。"""
        return MappingProxyType(self._defaults)

    def bind(self, **defaults: object) -> BoundLogger:
        """在既有默认字段上**再叠一层**（同名按新的），返回一份新视图（本视图不变）。"""
        return BoundLogger(self._logger, **{**self._defaults, **defaults})

    def _merge(
        self, owner_id: str, extra: Mapping[str, object]
    ) -> tuple[str, dict[str, object]]:
        """默认字段并进当次字段：当次同名键压过默认的；``owner_id`` 单独拎出来。"""
        merged: dict[str, object] = {**self._defaults, **extra}
        bound_owner: object = merged.pop("owner_id", "")
        return str(owner_id or bound_owner), merged

    # ------------------------------------------------------------------ 写入
    def is_enabled_for(self, level: LogLevel | str) -> bool:
        """本条日志是否达到**源实例**的生效级别。"""
        return self._logger.is_enabled_for(level)

    def write(self, record: LogRecord) -> bool:
        """直接写一条记录（走的还是**源实例**的队列与出口）：默认字段并进 ``extra``。"""
        if not self._defaults:
            return self._logger.write(record)
        owner_id, extra = self._merge(record.owner_id, record.extra)
        return self._logger.write(replace(record, extra=extra, owner_id=owner_id))

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
        merged_owner, merged_extra = self._merge(owner_id, extra)
        return self._logger.log(
            level, message, owner_id=merged_owner, exc_info=exc_info, **merged_extra
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

    def critical(
        self, message: object, *, owner_id: str = "", exc_info: object = False, **extra: object
    ) -> bool:
        return self.log(LogLevel.CRITICAL, message, owner_id=owner_id, exc_info=exc_info, **extra)

    def exception(self, message: object, *, owner_id: str = "", **extra: object) -> bool:
        """记录一条 ERROR 日志并附带当前异常堆栈（默认字段照带）。"""
        return self.log(LogLevel.ERROR, message, owner_id=owner_id, exc_info=True, **extra)

    @override
    def __repr__(self) -> str:  # pragma: no cover - 调试用
        keys = ", ".join(self._defaults)
        return f"<BoundLogger name={self.name!r} defaults=[{keys}]>"
