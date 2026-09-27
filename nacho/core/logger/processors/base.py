"""日志处理机基类。

日志处理机是日志系统真正的「落地方」。分发器从消息队列批量取出日志后扇出给
所有已注册的处理机；至于怎么缓冲、什么时候落盘，由各处理机自己说了算——
本地日志与数据库日志各有各的实现，互不影响。

本模块由两个类组成，职责分离：

* :class:`_LogBuffer`：**有界缓冲区**。只管「存得下 / 取出来 / 放不下怎么丢」，
  容量即硬上限，超限时按溢出策略取舍并计数，保证内存不会无界增长；
* :class:`BaseLogProcessor`：**处理机本体**。负责生命周期、定时刷盘、健康状态
  与指标统计；子类只需实现 :meth:`~BaseLogProcessor.write` 与
  :meth:`~BaseLogProcessor.search` 两个「后端契约」。

处理机之间以及与业务之间互相隔离：

* 单个处理机抛异常只会被记录并计数，不会中断分发器，也不影响其它处理机；
* 连续失败超过阈值后自动标记为不健康，分发器跳过它，防止持续崩溃；
* 缓冲**到水位线（``buffer_size``）就刷写**，而不是攒到「满了」再一次性写，
  单次 ``write`` 的条数也不超过水位线，防止把超大的一批日志压给后端；
* 处理机侧缓冲**不是**「和队列重复攒批」：队列只负责同步→异步的**交接**（窗口取小），
  攒批窗口与刷盘节奏完全由这里的 ``buffer_size`` / ``flush_interval`` 决定，因此控制台
  能逐条直写、文件 / 数据库能成批落，互不牵制。

处理机**不做任何路由与过滤**：它收到什么就写什么。日志该不该进某个出口，由分发器
在查找分发时用 :class:`~nacho.core.logger.filters.LogFilter` 判断（见
:meth:`~nacho.core.logger.base.BaseLogger.attach` 的 ``log_filter`` 参数），
被过滤掉的日志根本不会走到这里。
"""
from __future__ import annotations

import abc
import asyncio
import logging
from contextlib import suppress
from logging import Logger
from typing import TypedDict, final, override

from ..models import LogLevel, LogRecord, LogSearchResult, TimestampLike
from ..queue import OverflowPolicy

#: 处理机内部异常的统一兜底出口，避免异常处理本身再触发日志递归。
_fallback: Logger = logging.getLogger("nacho.core.logger.processor")


class ProcessorStats(TypedDict):
    """一个处理机的运行统计（:attr:`BaseLogProcessor.stats` 的返回结构）。"""

    name: str
    healthy: bool
    running: bool
    pending: int
    written: int
    failed: int
    dropped: int
    buffer_size: int
    flush_interval: float
    overflow_policy: str


@final
class _LogBuffer:
    """有界缓冲区：待落盘日志的暂存区。

    一个纯粹的容器，不认识「刷盘」这个概念：谁来取、取多少、什么时候取，
    全由 :class:`BaseLogProcessor` 决定。容量是对外的硬保证。
    """

    def __init__(self, capacity: int, policy: OverflowPolicy) -> None:
        """
        :param capacity: 缓冲区硬上限，任何时刻都不会超过它。
        :param policy: 容量腾不出时的取舍策略。
        """
        self._capacity: int = capacity
        self._policy: OverflowPolicy = policy
        self._lock: asyncio.Lock = asyncio.Lock()
        self._records: list[LogRecord] = []
        self._dropped: int = 0

    @property
    def pending(self) -> int:
        """当前缓存的条数。"""
        return len(self._records)

    @property
    def dropped(self) -> int:
        """因容量不足被丢弃的条数。"""
        return self._dropped

    @property
    def policy_value(self) -> str:
        """当前溢出策略的值，供统计上报。"""
        return self._policy.value

    async def extend_if_room(self, records: list[LogRecord]) -> bool:
        """容量够就并入并返回 ``True``；不够则什么也不做并返回 ``False``。"""
        async with self._lock:
            if len(self._records) + len(records) > self._capacity:
                return False
            self._records.extend(records)
            return True

    async def take(self, limit: int) -> list[LogRecord]:
        """取出并移除最多 ``limit`` 条，缓冲区为空时返回空列表。

        ``limit`` 不小于当前条数时是**整体取空**：直接把内部列表交出去、换一个
        新列表，``O(1)``，既不做切片拷贝也不搬移元素。只有真要「取一部分」时才走
        切片 + 前缀删除。生产路径上 :meth:`BaseLogProcessor.flush` 传的 ``limit``
        恰好是 ``buffer_size``，而缓冲区永不超过 ``buffer_size``，所以实际总是走前者。
        """
        async with self._lock:
            if limit >= len(self._records):
                batch, self._records = self._records, []
                return batch
            batch = self._records[:limit]
            del self._records[:limit]
            return batch

    async def force_extend(self, records: list[LogRecord]) -> None:
        """刷盘也腾不出空间时的兜底：按溢出策略取舍，保证缓冲区始终有界。"""
        if self._policy is OverflowPolicy.DROP_NEWEST:
            self._dropped += len(records)
            return
        # DROP_OLDEST / BLOCK（刷盘仍腾不出空间时退化为丢弃最旧）
        async with self._lock:
            overflow = len(self._records) + len(records) - self._capacity
            if overflow > 0:
                del self._records[:overflow]
                self._dropped += overflow
            self._records.extend(records)


class BaseLogProcessor(abc.ABC):
    """日志处理机基类。

    子类必须实现 :meth:`write`（写入方法）与 :meth:`search`（检索方法），
    其余生命周期、缓冲、刷盘、健康检查与统计均由基类统一提供。
    """

    #: 处理机名称，注入日志系统时作为唯一标识
    name: str = "base"

    #: 子实例「自层覆盖」时是否仍被带上。
    #:
    #: 子实例一旦自己挂过出口，就只投自己那份、不再带上父级 / 核心的文件出口；
    #: 但控制台这类全局观感出口应当保留，把它置为 ``True`` 即可在覆盖时继续继承。
    inherit_on_override: bool = False

    def __init__(
        self,
        *,
        name: str | None = None,
        buffer_size: int = 200,
        flush_interval: float = 2.0,
        max_failures: int = 5,
        overflow_policy: OverflowPolicy | str = OverflowPolicy.DROP_OLDEST,
    ) -> None:
        """
        :param name: 处理机名称。默认取类属性 ``name``；同一种处理机挂多个实例时
            （例如两个不同路径的本地文件处理机）必须显式指定，名称即唯一标识。
        :param buffer_size: 该出口的**攒批水位线**，同时也是缓冲区硬上限：待写条数
            达到它即刻刷写，且缓冲区绝不会超过它，因此不会溢出。攒批窗口只由它
            （与 ``flush_interval``）决定，**与分发器一次送来多少条无关**；当它小于
            等于分发批量（``dispatch_batch_size``）时，满载下缓冲区近似直通，真正起
            攒批作用的主要是中低负载，参数需按此理解。
        :param flush_interval: 定时刷盘间隔（秒），``<= 0`` 表示不定期刷。
        :param max_failures: 连续失败多少次后自动停用本处理机。
        :param overflow_policy: 缓冲区已达上限且刷盘腾不出空间时（并发写入争抢）
            的取舍策略，见 :class:`~nacho.core.logger.queue.OverflowPolicy`。
        """
        if buffer_size <= 0:
            raise ValueError("buffer_size 必须大于 0")
        if name is not None:
            # 实例属性覆盖类属性：同一种处理机允许挂多个实例，各用各的名字
            self.name = name

        self._buffer_size: int = buffer_size
        self._flush_interval: float = flush_interval
        self._max_failures: int = max_failures
        self._buffer: _LogBuffer = _LogBuffer(
            buffer_size, OverflowPolicy(overflow_policy)
        )
        #: 保证同一时刻只有一个 write 在飞，避免后端被并发压垮 / 顺序错乱
        self._write_lock: asyncio.Lock = asyncio.Lock()

        self._flush_task: asyncio.Task[None] | None = None
        self._running: bool = False

        self._healthy: bool = True
        self._consecutive_failures: int = 0
        self._written: int = 0
        self._failed: int = 0

    # ------------------------------------------------------------------ 后端契约
    @abc.abstractmethod
    async def write(self, records: list[LogRecord]) -> None:
        """写入方法：把一批日志真正落地（文件 / 数据库 / 远端等）。"""

    @abc.abstractmethod
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
    ) -> LogSearchResult:
        """检索方法：按条件查询该处理机中已落地的日志，并给出命中总数（翻页用）。

        ``limit`` / ``offset`` 只裁 ``records``；``total`` 是这批条件一共命中多少条，
        与翻到第几页无关。不留存历史的出口（如控制台）把 ``total`` 给 0 就行。

        **顺序**：按自增序号倒序（插入顺序，见 :attr:`LogRecord.seq`）；不留存序号的出口
        （控制台 / 文件）退回时间戳倒序，同一时刻的几条并列。

        ``owner_id`` 精确匹配所有者：``None`` 不限（默认），空串 = 只要公共的。
        """

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        """启动处理机：打开后端（:meth:`_on_start`）并开启定时刷盘。

        只有 ``_on_start`` 成功后才置为运行中，因此启动失败的处理机不会被当成
        已就绪（也不会调用 :meth:`stop` 去关一个没打开的后端），
        日志系统在下一次分发时会再试一次。
        """
        if self._running:
            return
        await self._on_start()
        self._running = True
        self._healthy = True
        self._consecutive_failures = 0
        if self._flush_interval > 0:
            self._flush_task = asyncio.create_task(
                self._flush_loop(), name=f"nacho-log-flush-{self.name}"
            )

    async def stop(self) -> None:
        """停止处理机：停定时刷盘 -> 刷完余量 -> 关闭后端（:meth:`_on_stop`）。"""
        if not self._running:
            return
        self._running = False
        await self._cancel_flush_task()
        await self._flush_remaining()
        await self._on_stop()

    async def _cancel_flush_task(self) -> None:
        """取消定时刷盘任务并等它退出，保证不会留下孤儿任务。"""
        if self._flush_task is None:
            return
        self._flush_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._flush_task
        self._flush_task = None

    async def _flush_remaining(self) -> None:
        """停机前把余量刷完；失败只记录，绝不向上抛（停机流程必须能走完）。"""
        try:
            await self.flush()
        except Exception:  # noqa: BLE001 - 停止流程不应抛出
            _fallback.exception("处理机 %s 停止时刷盘失败", self.name)

    async def _flush_loop(self) -> None:
        """定时刷盘：按 ``flush_interval`` 周期检查，有余量就落盘。"""
        while self._running:
            await asyncio.sleep(self._flush_interval)
            if not self._buffer.pending:
                continue
            try:
                await self.flush()
            except Exception:  # noqa: BLE001 - 定时刷盘失败不影响循环
                _fallback.exception("处理机 %s 定时刷盘失败", self.name)

    async def _on_start(self) -> None:
        """子类可覆写：建立连接、打开文件等。"""

    async def _on_stop(self) -> None:
        """子类可覆写：关闭连接、关闭文件等。"""

    # ------------------------------------------------------------------ 接收
    async def handle(self, record: LogRecord) -> None:
        """处理单条日志：进缓冲区，到水位线则刷盘。"""
        await self.handle_many([record])

    async def handle_many(self, records: list[LogRecord]) -> None:
        """批量处理日志：按水位线分片入缓冲，到量即刷，缓冲区始终有界。

        分发器一次可能送来很多条（例如 200 条），这里不是「攒满再一次性写」，
        而是每积累到 ``buffer_size`` 条就刷一次；单次 ``write`` 的条数也被
        ``buffer_size`` 卡住，避免把超大的一批日志压给后端（防止溢出）。

        传进来的 ``records`` 已经由分发器完成路由与过滤，处理机照单接收即可，
        自己不再做任何筛选。
        """
        if not records or not self._healthy:
            return
        for start in range(0, len(records), self._buffer_size):
            await self._stage(records[start : start + self._buffer_size])
            if self._buffer.pending >= self._buffer_size:
                await self.flush()

    async def _stage(self, records: list[LogRecord]) -> None:
        """并入缓冲区：容量够直接放，不够先刷盘腾空间，仍不够再按策略取舍。"""
        if await self._buffer.extend_if_room(records):
            return
        await self.flush()  # 背压：先把手里的刷掉腾出空间
        if not await self._buffer.extend_if_room(records):
            await self._buffer.force_extend(records)  # 兜底，保证缓冲区有界

    # ------------------------------------------------------------------ 落盘
    async def flush(self) -> None:
        """刷新缓冲区：把余量**分片**交给 :meth:`write` 写入。

        每次最多提交 ``buffer_size`` 条，直到把手里的排空为止，
        这样显式刷盘 / 停止时既不会丢日志，也不会把整批一次性压给后端。
        """
        while True:
            batch = await self._buffer.take(self._buffer_size)
            if not batch:
                return
            await self._write_guarded(batch)
            if len(batch) < self._buffer_size:
                return

    async def _write_guarded(self, records: list[LogRecord]) -> None:
        """带容错的写入：异常一律吞掉并记账，绝不向上传播到分发器。"""
        async with self._write_lock:
            try:
                await self.write(records)
            except Exception:  # noqa: BLE001 - 处理机异常必须隔离
                self._account_failure(len(records))
            else:
                self._account_success(len(records))

    # ------------------------------------------------------------------ 健康
    def _account_failure(self, count: int) -> None:
        """写入失败记账：累计失败数，连续失败到阈值则自动停用本处理机。"""
        self._failed += count
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._max_failures:
            self._healthy = False
            _fallback.error(
                "处理机 %s 连续失败 %d 次，已自动停用",
                self.name,
                self._consecutive_failures,
            )
            return
        _fallback.exception("处理机 %s 写入失败", self.name)

    def _account_success(self, count: int) -> None:
        """写入成功记账：累计写入数并清空连续失败计数。"""
        self._written += count
        self._consecutive_failures = 0

    # ------------------------------------------------------------------ 状态
    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def running(self) -> bool:
        return self._running

    @property
    def pending(self) -> int:
        """缓冲区中尚未刷盘的日志条数（不会超过 ``buffer_size``）。"""
        return self._buffer.pending

    @property
    def dropped(self) -> int:
        """因缓冲区容量不足被丢弃的日志条数。"""
        return self._buffer.dropped

    @property
    def stats(self) -> ProcessorStats:
        return {
            "name": self.name,
            "healthy": self._healthy,
            "running": self._running,
            "pending": self._buffer.pending,
            "written": self._written,
            "failed": self._failed,
            "dropped": self._buffer.dropped,
            "buffer_size": self._buffer_size,
            "flush_interval": self._flush_interval,
            "overflow_policy": self._buffer.policy_value,
        }

    @override
    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} healthy={self._healthy}>"
