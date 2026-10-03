"""``tickneko/core/logger/queue.py`` 单元测试。

对应提交 ``4e333eb feat(logger): 新增进程内异步日志队列 AsyncLogQueue``。
覆盖三种溢出策略、关闭语义与批量取用。
"""
from __future__ import annotations

import asyncio

import pytest

from tickneko.core.logger.models import LogRecord
from tickneko.core.logger.queue import AsyncLogQueue, OverflowPolicy


def make(message: str) -> LogRecord:
    return LogRecord(message=message, logger_name="test")


async def drain(queue: AsyncLogQueue) -> list[LogRecord]:
    """把队列里剩余的日志按 FIFO 顺序全部取出。"""
    records: list[LogRecord] = []
    while not queue.empty:
        record = await queue.get()
        assert record is not None
        records.append(record)
    return records


async def messages(queue: AsyncLogQueue) -> list[str]:
    return [record.message for record in await drain(queue)]


class TestOverflowPolicy:
    def test_is_str_enum_with_stable_values(self) -> None:
        assert OverflowPolicy("drop_oldest") is OverflowPolicy.DROP_OLDEST
        assert OverflowPolicy.DROP_NEWEST == "drop_newest"
        assert OverflowPolicy.BLOCK == "block"

    def test_invalid_policy_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            AsyncLogQueue(overflow_policy="whatever")


class TestPutAndSize:
    async def test_put_nowait_fills_queue(self) -> None:
        queue = AsyncLogQueue(maxsize=3)
        assert queue.put_nowait(make("a")) is True
        assert queue.put_nowait(make("b")) is True
        assert queue.qsize() == 2
        assert len(queue) == 2
        assert queue.empty is False
        assert queue.closed is False
        assert queue.dropped == 0
        assert queue.maxsize == 3
        assert queue.overflow_policy is OverflowPolicy.DROP_OLDEST

    def test_empty_queue(self) -> None:
        queue = AsyncLogQueue()
        assert queue.empty is True
        assert queue.qsize() == 0

    async def test_records_are_returned_in_fifo_order(self) -> None:
        queue = AsyncLogQueue()
        queue.put_nowait(make("a"))
        queue.put_nowait(make("b"))
        assert await messages(queue) == ["a", "b"]


class TestOverflowPolicies:
    async def test_drop_oldest_keeps_newest(self) -> None:
        queue = AsyncLogQueue(maxsize=2)
        queue.put_nowait(make("a"))
        queue.put_nowait(make("b"))
        assert queue.put_nowait(make("c")) is True
        assert queue.dropped == 1
        assert queue.qsize() == 2
        assert await messages(queue) == ["b", "c"]

    async def test_drop_newest_keeps_oldest(self) -> None:
        queue = AsyncLogQueue(maxsize=2, overflow_policy=OverflowPolicy.DROP_NEWEST)
        queue.put_nowait(make("a"))
        queue.put_nowait(make("b"))
        assert queue.put_nowait(make("c")) is False
        assert queue.dropped == 1
        assert await messages(queue) == ["a", "b"]

    async def test_block_policy_put_nowait_falls_back_to_drop_oldest(self) -> None:
        """``put_nowait`` 无法等待，BLOCK 策略下退化为丢弃最旧，保证不阻塞业务。"""
        queue = AsyncLogQueue(maxsize=1, overflow_policy=OverflowPolicy.BLOCK)
        queue.put_nowait(make("a"))
        assert queue.put_nowait(make("b")) is True
        assert queue.dropped == 1
        assert await messages(queue) == ["b"]

    async def test_non_block_policies_async_put_delegates(self) -> None:
        queue = AsyncLogQueue(maxsize=1)
        queue.put_nowait(make("a"))
        assert await queue.put(make("b")) is True
        assert queue.dropped == 1
        assert await messages(queue) == ["b"]

    async def test_block_policy_async_put_waits_for_space(self) -> None:
        queue = AsyncLogQueue(maxsize=1, overflow_policy=OverflowPolicy.BLOCK)
        queue.put_nowait(make("a"))

        pending = asyncio.create_task(queue.put(make("b")))
        await asyncio.sleep(0)
        assert pending.done() is False  # 队列满，put 正在等空位

        first = await queue.get()
        assert first is not None and first.message == "a"
        assert await asyncio.wait_for(pending, timeout=1) is True
        assert await messages(queue) == ["b"]

    async def test_block_policy_put_on_closed_queue_is_dropped(self) -> None:
        queue = AsyncLogQueue(maxsize=1, overflow_policy=OverflowPolicy.BLOCK)
        queue.close()
        assert await queue.put(make("a")) is False
        assert queue.dropped == 1


class TestCloseAndGet:
    async def test_close_marks_queue_closed(self) -> None:
        queue = AsyncLogQueue()
        queue.close()
        assert queue.closed is True

    async def test_get_returns_none_when_closed_and_empty(self) -> None:
        queue = AsyncLogQueue()
        queue.close()
        assert await queue.get() is None

    async def test_close_rejects_new_records_but_keeps_buffered(self) -> None:
        queue = AsyncLogQueue(maxsize=5)
        queue.put_nowait(make("a"))
        queue.close()

        assert queue.put_nowait(make("b")) is False
        assert queue.dropped == 1
        assert queue.qsize() == 1  # 已入队的日志不会丢

        assert await messages(queue) == ["a"]
        assert await queue.get() is None


class TestGetBatch:
    async def test_returns_at_most_max_items(self) -> None:
        queue = AsyncLogQueue()
        for index in range(5):
            queue.put_nowait(make(str(index)))

        batch = await queue.get_batch(max_items=2, timeout=1)
        assert [record.message for record in batch] == ["0", "1"]
        assert queue.qsize() == 3

        rest = await queue.get_batch(max_items=10, timeout=1)
        assert [record.message for record in rest] == ["2", "3", "4"]

    async def test_returns_empty_list_on_timeout(self) -> None:
        queue = AsyncLogQueue()
        assert await queue.get_batch(max_items=10, timeout=0.01) == []

    async def test_returns_empty_list_when_closed_and_empty(self) -> None:
        queue = AsyncLogQueue()
        queue.close()
        assert await queue.get_batch(max_items=10, timeout=0.01) == []

    async def test_returns_partial_batch_when_closed_with_leftovers(self) -> None:
        queue = AsyncLogQueue()
        queue.put_nowait(make("a"))
        queue.close()

        batch = await queue.get_batch(max_items=10, timeout=0.01)
        assert [record.message for record in batch] == ["a"]
        assert await queue.get_batch(max_items=10, timeout=0.01) == []
