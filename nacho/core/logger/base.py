"""日志系统基类。

职责划分：

* :meth:`BaseLogger.write`：**写入方法**。默认把日志推到消息队列（非阻塞），
  业务线程永远不会因为落盘/落库而卡住；
* 内部分发器从队列批量取日志，扇出给所有已注入的日志处理机；
* :meth:`BaseLogger.flush`：**刷新缓冲区方法**，刷新所有处理机的缓冲区；
* :meth:`BaseLogger.search`：**检索方法**，聚合各处理机的检索结果。

输出通道（处理机）通过 :meth:`attach` 挂载，可以只挂一个（比如控制台），
也可以同时挂多个（控制台 + 本地文件 + 数据库）；**运行期也能挂载**，
挂载后该处理机会在下一次分发前自动启动，无需手动调它的 ``start()``。

模块路由由 :class:`RouteTable` 维护一棵**名字树**，对外只有一句「设置字符串名字」：

* 名字按 ``.`` 分层（``nacho.robot.arm``），设置**逐层继承**：子名字自动拿到父名字
  挂的输出设备与声明的级别，只想改一部分时在子层就地声明即可；
* :meth:`attach` 不带 ``name`` 时挂到**全量出口**（树的根），所有日志都会进；
* :meth:`attach` 带 ``name`` 挂到**该名字那一层**，该名字及其子名字都会收到；
* 同一个名字永远对应同一个节点（**有则载入**），重复设置不会重复建出口；
* 某个名字沿途一层都没挂出口时，日志**照旧按默认全量输出投递**，只警告一次。

这里的继承是**解析配置时**沿树向上取一份确定的结果、再投递一次，**不是**把记录往
父级冒泡：父层的设备之所以收到子层的日志，是因为子层继承了它，不会因此多写一遍。

**过滤**由分发器统一负责：:meth:`attach` 的 ``log_filter``
（:class:`~nacho.core.logger.filters.LogFilter`）挂在出口一侧、存在分发器里，
一条日志只有在**查找分发**时通过了某个出口的过滤器，才会被投递给它；被过滤掉的
日志连处理机的缓冲区都不进。过滤器因此不属于处理机——处理机只负责落地。

**两级缓冲，分工明确**（别把它们当成重复的攒批）：

* :class:`~nacho.core.logger.queue.AsyncLogQueue`（**交接**）：同步写入方与异步
  分发器之间的边界。``dispatch_timeout`` 只决定「一次最多等多久把手上这批取走」，
  是**交接窗口**而非攒批窗口，所以取小值，让日志尽快交到处理机；
* :class:`~nacho.core.logger.processors.base.BaseLogProcessor` 的缓冲区（**攒批**）：
  每个出口自己的批大小与刷盘周期（``buffer_size`` / ``flush_interval``），攒批窗口
  只由它决定——控制台可以 ``buffer_size=1`` 逐条直写，文件 / 数据库则成批落，
  互不牵制。队列若也用大窗口攒批，就会和这里形成两套互相打架的配方。
"""
from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import cast, override

from .filters import LogFilter
from .models import LogLevel, LogRecord, TimestampLike
from .processors.base import BaseLogProcessor
from .queue import AsyncLogQueue, OverflowPolicy

_fallback = logging.getLogger("nacho.core.logger")


def _dedupe(processors: Sequence[BaseLogProcessor]) -> list[BaseLogProcessor]:
    """按 ``id`` 去重保序。

    同一个处理机常常同时出现在多层上（本层挂的 + 从父层复制来的 + 全量出口），
    累加时只能投一次，绝不能因为「复制了一份」就变成重复输出。
    """
    unique: list[BaseLogProcessor] = []
    seen: set[int] = set()
    for processor in processors:
        if id(processor) not in seen:
            seen.add(id(processor))
            unique.append(processor)
    return unique


@dataclass
class RouteEntry:
    """名字树上的一个节点：本层声明 + 从父层**复制**下来的一份设备副本。

    「设置名字」与「给这个名字挂设备」是两件事：``attach(..., name="a.b")`` 或
    ``set_level`` 只把节点建出来，上面挂什么由后续的 ``attach`` 决定。

    **设备是复制下来的，级别才是继承的**（级别见 :meth:`RouteTable.level_for`）：

    * :attr:`processors`：本层**自己挂**的设备；
    * :attr:`inherited`：本层**第一次用到**（写日志 / 查路由）时，从父层
      那条链上复制一份下来的快照；``None`` 表示还没复制过；
    * 复制过一遍之后，父层再挂 / 再摘都**不回头影响**本层，各层之后各改各的——这就是
      「树形挂载」：父层挂一次，子层拿到一份属于自己的副本，而不是每次写日志都去
      父层现取一遍。
    """

    name: str
    #: 本层自己挂的输出设备（``attach`` 到本层的那几个）
    processors: list[BaseLogProcessor] = field(default_factory=list)
    #: 从父层复制来的设备副本；``None`` = 还没复制过（第一次用到时才复制）
    inherited: list[BaseLogProcessor] | None = None
    #: 本层的最低级别；``None`` 表示继承父层
    level: LogLevel | None = None


class RouteTable:
    """名字树：把「字符串名字」按 ``.`` 组织成父子关系，**设备复制、级别继承**。

    日志系统内部只维护这一张表：子模块给一个字符串名字，出口挂到哪条路径由名字
    决定；:meth:`entry` 保证同一个名字永远返回同一条节点（**有则载入**），
    所以模块初始化可以重复调用而不会重复建节点。

    **树形挂载：父层挂一次，子层复制一份。** 两条轴走法不同：

    * 设备（:meth:`targets`）：名字**第一次用到**时，把父层那条链的设备**复制一份**
      存进本节点的 :attr:`RouteEntry.inherited`。副本一到手就**冻结**：父层之后再挂 /
      再摘，都不回头改本层；本层要变就自己 ``attach``。
      复制发生在「第一次用到」而不是「建节点」，是为了让运行期挂载照旧生效——
      节点常常在 import 期就建好，出口往往之后才挂上；
    * 级别（:meth:`level_for`）：沿 ``a.b.c`` -> ``a.b`` -> ``a`` 逐段上升，就近取
      第一个显式声明。级别**不复制**：父层改了，已经在用的子实例立刻跟着变。

    两条轴都是**一次投递**：父层设备收到子层日志，是「子层复制了它」的结果，不存在
    「记录往父级冒泡、每层各写一遍」的重复输出。

    **没声明过的裸名字**没有副本可复制，仍然现场沿树向上取一份结果（父层改了立刻跟着
    变），一路落到 :attr:`default`（全量出口）为止。全量出口是**共享且实时**的：
    任何时候 ``attach`` 一路，所有名字的日志都会进——这条是硬保证，不受复制影响。
    """

    def __init__(self) -> None:
        self._entries: dict[str, RouteEntry] = {}
        self._default: list[BaseLogProcessor] = []
        #: 出口名 -> 过滤器；分发器在查找分发时用它决定「这条日志投不投该出口」
        self._filters: dict[str, LogFilter] = {}
        self._announced: set[str] = set()
        self._warned: set[str] = set()

    # ------------------------------------------------------------ 名字 -> 列表
    def entry(self, name: str) -> RouteEntry:
        """取（或建）名字对应的转发列表：有则载入，无则新建。

        登记名字即「声明」：它之后一条出口都没挂时，父模块就算没给它挂载。
        """
        existing = self._entries.get(name)
        if existing is not None:
            return existing
        self._announced.add(name)
        entry = RouteEntry(name=name)
        self._entries[name] = entry
        return entry

    def get(self, name: str) -> RouteEntry | None:
        """按名字取转发列表条目；没登记过则返回 ``None``。"""
        return self._entries.get(name)

    def names(self) -> list[str]:
        """已登记的名字（含只声明过、还没挂出口的）。"""
        return sorted(self._entries)

    def snapshot(self) -> dict[str, list[BaseLogProcessor]]:
        """名字 -> **本层**转发列表 的快照副本（不含继承来的，看 :meth:`resolve`）。"""
        return {name: list[BaseLogProcessor](entry.processors) for name, entry in self._entries.items()}

    def resolve(self, name: str) -> list[BaseLogProcessor]:
        """解析名字**实际**会收到的输出设备（去重保序，未过滤）。

        已声明的名字走它自己那份**副本**（第一次解析时复制下来，之后冻结）；没声明过的
        裸名字没有副本，现场沿树向上取一份（父层改了立刻跟着变）。
        """
        node = self._entries.get(name)
        if node is None:
            return self._collect(self._chain(name))
        return self._devices(node)

    def _devices(self, node: RouteEntry) -> list[BaseLogProcessor]:
        """一个节点**实际**会收到的设备：复制来的副本 + 本层自己挂的（+ 全量出口）。"""
        self._materialize(node)
        devices = _dedupe([*(node.inherited or []), *node.processors])
        return _dedupe([*devices, *self._default])

    def _materialize(self, node: RouteEntry) -> None:
        """本层**第一次用到**时，把父层那条链的设备复制一份下来（只复制这一次）。

        复制过一遍之后，父层再挂 / 再摘都不回头影响本层。放在「第一次用到」而不是
        「建节点」时，是为了让运行期挂载照旧生效：节点常在 import 期就建好，
        出口往往之后才挂上。
        """
        if node.inherited is not None:
            return
        parent = self._nearest(node.name)
        if parent is None:
            node.inherited = []
            return
        self._materialize(parent)
        node.inherited = _dedupe([*(parent.inherited or []), *parent.processors])

    def _nearest(self, name: str) -> RouteEntry | None:
        """沿 ``.`` 向上找**最近一个已声明**的节点（``a.b.c`` -> ``a.b`` -> ``a``）。

        逐段比对而不是 ``startswith``：``api`` 不会吃掉 ``apix``。
        """
        current = name
        while "." in current:
            current = current.rsplit(".", 1)[0]
            node = self._entries.get(current)
            if node is not None:
                return node
        return None

    # ------------------------------------------------------------ 级别
    def level_for(self, name: str) -> LogLevel | None:
        """沿途就近取第一个**显式声明**的级别；一层都没声明返回 ``None``。"""
        entries = self._chain(name)
        for entry in entries:
            if entry.level is not None:
                return entry.level
        return None

    def set_level(self, name: str, level: "LogLevel | str | None") -> None:
        """给名字对应的节点声明级别；``None`` 表示取消声明、回到向上继承。"""
        self.entry(name).level = None if level is None else LogLevel.parse(level)

    # ------------------------------------------------------------ 全量出口
    @property
    def default(self) -> list[BaseLogProcessor]:
        """全量出口列表本身（请勿直接修改）。"""
        return self._default

    def add_default(self, processor: BaseLogProcessor) -> None:
        """登记一个全量出口（不带名字挂载的处理机）。"""
        self._default.append(processor)

    def discard(self, processor: BaseLogProcessor) -> None:
        """把处理机从全量出口、各层清单（含**复制来的副本**）与其过滤器里摘掉。

        这是**全局卸载**：处理机既然摘了，任何副本里都不该再留着它，否则被卸载的出口
        还会继续收到日志。
        """
        if processor in self._default:
            self._default.remove(processor)
        for entry in self._entries.values():
            if processor in entry.processors:
                entry.processors.remove(processor)
            if entry.inherited and processor in entry.inherited:
                entry.inherited.remove(processor)
        self._filters.pop(processor.name, None)  # pyright: ignore[reportUnusedCallResult]

    # ------------------------------------------------------------ 过滤器
    def set_filter(
        self, processor: BaseLogProcessor, log_filter: LogFilter | None
    ) -> None:
        """给出口绑定（或清除）过滤器：由分发器在查找分发时调用。"""
        if log_filter is None:
            self._filters.pop(processor.name, None)  # pyright: ignore[reportUnusedCallResult]
        else:
            self._filters[processor.name] = log_filter

    def get_filter(self, processor: BaseLogProcessor) -> LogFilter | None:
        """取出口的过滤器；没挂则返回 ``None``（即全收）。"""
        return self._filters.get(processor.name)

    def unmounted(self) -> list[str]:
        """声明过名字、但**沿途一层都没挂输出设备**的名字（即「父模块没挂载」）。

        按树判定而不是只看自己那一层：父模块挂了设备的名字不算未挂载，因为它的日志
        会被父层的出口接走。这是**只读**接口，不会触发设备复制、不改变线路状态。
        """
        result: list[str] = []
        for name in sorted(self._announced):
            if not self._named_devices(name):
                result.append(name)
        return result

    def _named_devices(self, name: str) -> list[BaseLogProcessor]:
        """沿途最近一处「自己声明过的东西」：本层挂的 + 复制来的副本（**不触发复制**）。"""
        current = name
        while True:
            node = self._entries.get(current)
            if node is not None:
                if node.inherited is not None:
                    return [*node.inherited, *node.processors]
                if node.processors:
                    return list(node.processors)
                # 还没复制过：继续往上找（只读，不替它复制）
            if "." not in current:
                return []
            current = current.rsplit(".", 1)[0]

    # ------------------------------------------------------------ 声明与投递
    def announce(self, name: str) -> None:
        """声明「我是名字 name 的模块」，用于父级没挂出口时的告警。"""
        self._announced.add(name)

    def targets(self, record: LogRecord) -> list[BaseLogProcessor]:
        """一条日志该投给哪些处理机（**路由 + 复制 + 过滤**都在这里完成）。

        已声明的名字：**第一次投递**时把父层那条链的设备复制一份到本节点，之后这条
        线路就冻结——本层要变只认自己的 ``attach``。所以父层挂了设备，
        子模块不用自己再挂一遍就能收到（复制来的那句「父层挂一次，子层拿到一份」）；
        反过来父层之后再改，也不回头影响已经复制过的子层。

        没声明过的裸名字没有副本，现场沿树向上取一份（父层改了立刻跟着变），一路落到
        **默认全量输出**。

        沿途一层都没挂设备、且该名字被 ``announce`` 过时告警一次（父模块没给它挂载），
        日志照旧按全量输出投递，不静默丢弃——也绝不把别的模块的日志串进某条列表。

        定好候选出口后，再用每个出口的 :class:`~nacho.core.logger.filters.LogFilter`
        过滤一遍。整份结果仍是**一次投递**：不存在「记录往父级冒泡、每层各写一遍」的
        重复输出，父层设备之所以收到日志，是因为子层**复制了**它，不是因为又冒泡上去。
        """
        node = self._entries.get(record.logger_name)
        if node is None:
            entries = self._chain(record.logger_name)
            if not any(entry.processors for entry in entries):
                self._warn_unmounted(record.logger_name)
            return self._accept(self._collect(entries), record)
        devices = self._devices(node)
        if not (node.inherited or node.processors):
            self._warn_unmounted(record.logger_name)
        return self._accept(devices, record)

    def _accept(
        self, processors: list[BaseLogProcessor], record: LogRecord
    ) -> list[BaseLogProcessor]:
        """分发器侧过滤：未挂过滤器的出口全收，挂了过滤器的看它是否放行。"""
        accepted: list[BaseLogProcessor] = []
        for processor in processors:
            log_filter = self._filters.get(processor.name)
            if log_filter is None or log_filter.match(record):
                accepted.append(processor)
        return accepted

    def _chain(self, name: str) -> list[RouteEntry]:
        """从 ``name`` 沿 ``.`` 逐段上升，返回沿途已声明的节点（就近在前）。

        节点是「有则载入」建出来的，没声明的中间层自然被跳过（没声明就等于继承）。

        逐段比对而不是 ``startswith`` 匹配，顺带解决旧写法的两个问题：``api`` 不会吃掉
        ``apix``；已在树上的祖先**无条件**参与（旧写法要求祖先"挂了设备"才认，于是
        ``a`` 只声明级别时 ``a.b`` 就继承不到它）。
        """
        entries: list[RouteEntry] = []
        current = name
        while current:
            node = self._entries.get(current)
            if node is not None:
                entries.append(node)
            if "." not in current:
                break
            current = current.rsplit(".", 1)[0]
        return entries

    def _collect(self, entries: list[RouteEntry]) -> list[BaseLogProcessor]:
        """把沿途各层的设备与全量出口汇成一份**去重保序**的清单（裸名字的现场解析用）。"""
        collected: list[BaseLogProcessor] = []
        for entry in entries:
            collected.extend(entry.processors)
        collected.extend(self._default)
        return _dedupe(collected)

    def _warn_unmounted(self, name: str) -> None:
        """父模块没为该名字挂载出口：警告一次，之后不再刷屏。"""
        if name not in self._announced or name in self._warned:
            return
        self._warned.add(name)
        _fallback.warning(
            "父模块未为 %r 挂载输出设备，日志已按默认全量输出投递；"
            + "要给它专属出口请 attach(processor, name=%r)",
            name,
            name,
        )


def _require_processor(processor: object) -> None:
    """运行期校验：不是 :class:`BaseLogProcessor` 的实例时抛出 ``TypeError``。

    形参特意声明为宽类型 ``object`` 而非 ``BaseLogProcessor``：``attach`` 的形参类型
    本就该在静态检查阶段拦住传错的调用方，但配置解析 / 反射 / 无类型代码等**动态调用**
    会绕过静态检查，这道运行期防线必须真正可达（若按真实类型标注，静态分析会把
    ``isinstance`` 判为恒真、把 ``raise`` 当成不可达代码），才能给出明确报错
    （而不是把错误对象登记进路由表、直到分发时才崩）。

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
    """日志系统基类：队列 + 分发器 + 可注入的日志处理机。"""

    def __init__(
        self,
        name: str = "nacho",
        *,
        level: LogLevel | str = LogLevel.INFO,
        queue: AsyncLogQueue | None = None,
        processors: list[BaseLogProcessor] | None = None,
        routes: RouteTable | None = None,
        overflow_policy: OverflowPolicy | str = OverflowPolicy.DROP_OLDEST,
        queue_maxsize: int = 10000,
        dispatch_batch_size: int = 200,
        dispatch_timeout: float = 0.2,
    ) -> None:
        """
        :param dispatch_batch_size: 分发器一次最多从队列取多少条。这是**交接批量**，
            不是攒批水位线——攒批由各处理机的 ``buffer_size`` 决定。
        :param dispatch_timeout: 队列取不到新日志时，最多再等多久就把手上这批先交出去。
            这是**交接窗口**，不是攒批窗口：取小值（默认 0.2s）让日志尽快交到处理机，
            避免和处理器缓冲区的 ``buffer_size`` / ``flush_interval`` 形成两套互相
            打架的攒批配方。
        """
        self.name: str = name
        self._level: LogLevel = LogLevel.parse(level)
        #: 名字 -> 转发列表 的路由表，由本实例、子实例与模块入口共享
        self._routes: RouteTable = routes if routes is not None else RouteTable()
        # 注意：必须用 is not None 判断，AsyncLogQueue 实现了 __len__，
        # 空队列在布尔上下文中为 False，用 or 会错误地新建队列。
        self._queue: AsyncLogQueue = (
            queue
            if queue is not None
            else AsyncLogQueue(maxsize=queue_maxsize, overflow_policy=overflow_policy)
        )
        # 传入列表时按共享注册表处理，便于多个 logger 共用同一批处理机
        self._processors: list[BaseLogProcessor] = (
            processors if processors is not None else []
        )
        if routes is None:
            # 自己新建的路由表：构造时传入的处理机就是这个实例的全量出口。
            # 共享路由表（child）时绝不能再登记一遍——那些处理机可能已按
            # 名字路由到某条转发列表，补进全量出口会让它们收到全部模块的日志。
            for processor in self._processors:
                self._routes.add_default(processor)
        self._dispatch_batch_size: int = dispatch_batch_size
        self._dispatch_timeout: float = dispatch_timeout
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._running: bool = False

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

        :param name: 这个通道接管的**字符串名字**：``None`` 表示全量出口（所有日志
            都进），否则只接收该名字（及其 ``.`` 前缀子名字）的日志。同一个名字
            永远对应同一条转发列表（**有则载入**）。
        :param log_filter: 这个通道的过滤器（:class:`~nacho.core.logger.filters.LogFilter`）。
            ``None`` 表示全收；给了过滤器则**由分发器在查找分发时**用它筛掉不
            该进本通道的日志——过滤器属于分发侧，与处理机无关。
        :param replace: 同名通道已存在时是否替换（换输出路径 / 模块热重载时用）。
        :raises TypeError: ``processor`` 不是 :class:`BaseLogProcessor` 子类，
            或 ``log_filter`` 不是 :class:`LogFilter` 子类。
        :raises ValueError: 同名通道已存在且 ``replace=False``。
        """
        _require_processor(processor)
        if log_filter is not None:
            _require_filter(log_filter)
        target: str | None = name
        existing: BaseLogProcessor | None = self.get_processor(processor.name)
        if existing is not None:
            if not replace:
                raise ValueError(
                    f"处理机 {processor.name!r} 已挂载；"
                    + "要换输出路径请用 attach(processor, replace=True)"
                )
            # 换出口：旧实例既要从注册表摘掉，也要从它原来所在的那条转发列表里摘掉
            self._processors.remove(existing)
            self._routes.discard(existing)
        self._processors.append(processor)
        if target is None:
            self._routes.add_default(processor)
        else:
            self._routes.entry(target).processors.append(processor)
        # 过滤器登记在分发器一侧，由它在「查找分发」时应用
        self._routes.set_filter(processor, log_filter)
        return processor

    def attach_many(self, *processors: BaseLogProcessor) -> list[BaseLogProcessor]:
        """批量挂载输出通道（全部挂到全量出口）。"""
        return [self.attach(processor) for processor in processors]

    def detach(self, name: str) -> BaseLogProcessor | None:
        """按名称卸载输出通道，返回被卸载的处理机；不存在则返回 ``None``。

        卸载只是让日志系统不再给它送日志（会同时从全量出口与各层清单——含**复制来的
        副本**——里摘掉）。冲刷余量与关闭后端由调用方决定
        （``await processor.stop()`` 会先刷完余量再关闭）。
        """
        for index, processor in enumerate(self._processors):
            if processor.name == name:
                self._processors.pop(index)  # pyright: ignore[reportUnusedCallResult]
                self._routes.discard(processor)
                return processor
        return None

    def get_processor(self, name: str) -> BaseLogProcessor | None:
        """按名称取已挂载的输出通道。"""
        for processor in self._processors:
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
        """已注入处理机的快照副本。"""
        return list(self._processors)

    @property
    def processor_registry(self) -> list[BaseLogProcessor]:
        """处理机注册表本身，供多个日志实例共享（请勿直接修改）。"""
        return self._processors

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

    def child(self, name: str, *, level: LogLevel | str | None = None) -> BaseLogger:
        """派生一个子日志实例：共享本实例的队列、名字树与处理机注册表，只换名字与级别。

        名字**相对本实例**：``LogCore("nacho").child("a1")`` 得到的名字是 ``nacho.a1``——
        写相对的一段（``"a1"``、``"robot.arm"``）会自动补上父前缀；已经写全的名字
        （``"nacho.a1"``）原样使用，两种写法可以混用（见 :meth:`qualify`）。记录的
        ``logger_name`` 就是这个完整名字，路由据此在名字树上解析这条日志的去向。要给某个
        名字挂专属出口，用 :meth:`attach` 的 ``name`` 参数（或便捷函数 ``attach_mount``）。

        子实例与父实例共享同一棵名字树：第一次给它（或其子名字）写日志时，会把父层那条
        链上的输出设备**复制一份**存进自己的节点——副本一到手就冻结，父层之后再挂 / 再摘
        都不回头影响它；级别则逐层**继承**（父层改了立刻跟着变）。

        :param level: 显式指定时会**同时在名字树上声明**，因此该名字的子名字也继承得到它；
            省略则只把本实例的默认级别沿用作兜底，不向树上写声明。
            要「继承了再改一部分」，用 :meth:`child` + :meth:`set_level`。
        :raises ValueError: ``name`` 为空。

        子实例**不要**自己 ``start()``：分发器与队列归父实例所有，重复启动只会
        多出一个抢同一队列的分发器。
        """
        if not name:
            raise ValueError("child 名字不能为空")
        full: str = self.qualify(name)
        self._routes.announce(full)
        if level is not None:
            self._routes.set_level(full, level)
        return BaseLogger(
            full,
            level=self._level if level is None else level,
            queue=self._queue,
            processors=self._processors,
            routes=self._routes,
            dispatch_batch_size=self._dispatch_batch_size,
            dispatch_timeout=self._dispatch_timeout,
        )

    @property
    def routes(self) -> dict[str, list[BaseLogProcessor]]:
        """「字符串名字 -> 转发列表」快照副本（改它不会影响路由）。"""
        return self._routes.snapshot()

    @property
    def outputs(self) -> list[BaseLogProcessor]:
        """全量出口快照：不带名字挂载的处理机，即「默认全量输出」的去处。"""
        return list(self._routes.default)

    @property
    def unmounted_modules(self) -> list[str]:
        """声明过名字、却没挂任何出口的名字（父模块没挂载，日志走全量输出）。"""
        return self._routes.unmounted()

    @property
    def queue(self) -> AsyncLogQueue:
        return self._queue

    @property
    def level(self) -> LogLevel:
        """本实例的**默认**级别（名字树上没声明时用它兜底）。"""
        return self._level

    def set_level(self, level: "LogLevel | str | None") -> None:
        """给**本实例的名字**在名字树上声明级别，子名字自动继承。

        这是「层层继承 + 就地覆盖」的入口：父层声明一次，其下所有子名字跟着生效；
        子层再 :meth:`set_level` 只覆盖自己这一支。传 ``None`` 取消声明、回到继承。
        """
        self._routes.set_level(self.name, level)

    def effective_level(self, name: str | None = None) -> LogLevel:
        """某个名字的**生效级别**：名字树上就近声明的优先，否则本实例默认。

        ``name`` 默认取本实例的名字。树上的声明是**动态解析**的，所以父层改了级别，
        已经在用的子实例立刻跟着变，不需要重建实例。
        """
        declared = self._routes.level_for(self.name if name is None else name)
        return self._level if declared is None else declared

    def effective_outputs(self, name: str | None = None) -> list[BaseLogProcessor]:
        """某个名字**实际**会收到的输出设备（复制来的副本 + 本层挂的 + 全量出口，不含过滤）。

        已声明的名字走它自己那份**副本**：第一次查询（或第一次写日志）时从父层复制一份，
        之后就冻结——父层再挂 / 再摘都不影响它。没建过节点的裸名字则现场沿树取，
        父层改了立刻跟着变。排查「这条日志到底进了哪几个出口」看这个；
        :attr:`outputs` 只是全量出口，:attr:`routes` 只是各层**本层**的列表。
        """
        return self._routes.resolve(self.name if name is None else name)

    @property
    def running(self) -> bool:
        """分发器是否在运行（即 :meth:`start` 是否已生效）。"""
        return self._running

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> BaseLogger:
        """启动所有处理机与内部分发器。"""
        if self._running:
            return self
        self._running = True
        for processor in list(self._processors):
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

        for processor in list(self._processors):
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

    async def _dispatch(self, records: list[LogRecord]) -> None:
        # 查找分发：按「字符串名字 -> 转发列表」定去向，并用各出口的过滤器筛一遍；
        # 父模块没挂载的名字会在这里被警告一次，并按默认全量输出投递
        routed: list[tuple[LogRecord, list[BaseLogProcessor]]] = [
            (record, self._routes.targets(record)) for record in records
        ]
        for processor in list(self._processors):
            if not processor.healthy:
                continue
            batch = [record for record, targets in routed if processor in targets]
            if not batch:
                continue
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
    def is_enabled_for(self, level: "LogLevel | str") -> bool:
        """本条日志是否达到**本名字**的生效级别（名字树上的声明优先于实例默认）。"""
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
        if not self._processors:
            return
        results = await asyncio.gather(
            *(processor.flush() for processor in list(self._processors)),
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
        processors: "Sequence[str] | None" = None,
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
        if names is None:
            return list(self._processors)
        wanted = set(names)
        return [p for p in self._processors if p.name in wanted]

    # ------------------------------------------------------------------ 状态
    @property
    def stats(self) -> dict[str, object]:
        queue_dropped = self._queue.dropped
        buffer_dropped = sum(processor.dropped for processor in self._processors)
        processors = [processor.stats for processor in self._processors]
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
                name: [processor.name for processor in processors]
                for name, processors in sorted(self._routes.snapshot().items())
            },
            "unmounted_modules": self._routes.unmounted(),
        }

    @override
    def __repr__(self) -> str:  # pragma: no cover - 调试用
        names = ", ".join(p.name for p in self._processors)
        return f"<BaseLogger name={self.name!r} level={self._level.name} processors=[{names}]>"
