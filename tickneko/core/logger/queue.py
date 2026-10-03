"""进程内异步消息队列。

日志系统默认把日志推到该队列，再由分发器取出交给各个日志处理机，
从而实现「写日志的一方」与「落日志的一方」解耦：

* 生产者非阻塞投递，队列满时按策略丢弃，日志永远不会阻塞或拖垮业务；
* 消费者批量取，批量交给处理机，减少 IO 次数。
"""
from __future__ import annotations

import asyncio
from enum import Enum
from .models import LogRecord


class OverflowPolicy(str, Enum):
    """队列写满时的处理策略。"""

    DROP_OLDEST = "drop_oldest"  # 丢弃最旧的一条，保证最新日志可见（默认）
    DROP_NEWEST = "drop_newest"  # 直接丢弃新日志
    BLOCK = "block"  # 阻塞等待（仅 async put 支持）


class AsyncLogQueue:
    """日志专用异步队列。"""

    def __init__(
        self,
        maxsize: int = 10000,
        overflow_policy: OverflowPolicy | str = OverflowPolicy.DROP_OLDEST,
    ) -> None:
        self._queue: asyncio.Queue[LogRecord] = asyncio.Queue(maxsize=maxsize)
        self._policy: OverflowPolicy = OverflowPolicy(overflow_policy)
        self._dropped: int = 0
        self._closed: bool = False

    # ------------------------------------------------------------------ 属性
    @property
    def maxsize(self) -> int:
        return self._queue.maxsize

    @property
    def overflow_policy(self) -> OverflowPolicy:
        return self._policy

    @property
    def dropped(self) -> int:
        """因队列满或已关闭而被丢弃的日志条数。"""
        return self._dropped

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def empty(self) -> bool:
        return self._queue.empty()

    def qsize(self) -> int:
        return self._queue.qsize()

    # ------------------------------------------------------------------ 投递
    def put_nowait(self, record: LogRecord) -> bool:
        """非阻塞投递，返回是否成功入队。"""
        if self._closed:
            self._dropped += 1
            return False
        try:
            self._queue.put_nowait(record)
            return True
        except asyncio.QueueFull:
            pass

        if self._policy is OverflowPolicy.DROP_NEWEST:
            self._dropped += 1
            return False

        # DROP_OLDEST：挤掉最旧的一条，把最新日志放进去
        try:
            _ = self._queue.get_nowait()
            self._dropped += 1
        except asyncio.QueueEmpty:
            pass

        try:
            self._queue.put_nowait(record)
            return True
        except asyncio.QueueFull:
            self._dropped += 1
            return False

    async def put(self, record: LogRecord) -> bool:
        """异步投递；仅 BLOCK 策略会真正等待。"""
        if self._policy is OverflowPolicy.BLOCK:
            if self._closed:
                self._dropped += 1
                return False
            await self._queue.put(record)
            return True
        return self.put_nowait(record)

    # ------------------------------------------------------------------ 消费
    async def get(self) -> LogRecord | None:
        """取一条日志；队列已关闭且为空时返回 ``None``。"""
        if self._closed and self._queue.empty():
            return None
        return await self._queue.get()

    async def get_batch(self, max_items: int = 200, timeout: float = 0.5) -> list[LogRecord]:
        """批量取日志；等待超时或队列关闭则返回已取到的部分。"""
        batch: list[LogRecord] = []
        try:
            first: LogRecord | None = await asyncio.wait_for(self.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return batch

        if first is None:
            return batch
        batch.append(first)

        while len(batch) < max_items:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch

    # ------------------------------------------------------------------ 生命周期
    def close(self) -> None:
        """关闭队列：不再接收新日志，已入队的日志仍可被消费。"""
        self._closed = True

    def __len__(self) -> int:
        return self._queue.qsize()
