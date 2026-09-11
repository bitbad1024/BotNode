"""``nacho/core/logger/processors/base.py`` 单元测试。

命名约定：``tests/test_log_<模块>.py`` 对应 ``nacho/core/logger/<模块>.py``；
``log_`` 前缀用于指明是「日志框架」的哪个模块，避免与框架里其它 ``base``
模块的同名测试混淆。

对应提交 ``96a073a feat(logger): 新增日志处理机基类 BaseLogProcessor``。
覆盖有界缓冲区的容量硬保证、水位线分片刷盘、失败隔离与生命周期。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import pytest

from nacho.core.logger.models import LogLevel, LogRecord, TimestampLike
from nacho.core.logger.processors.base import BaseLogProcessor
#: ``_LogBuffer`` 是模块内部的辅助类，其容量保证与溢出取舍只能在此直接验证，
#: 因此有意越过私有可见性检查。
from nacho.core.logger.processors.base import _LogBuffer  # pyright: ignore[reportPrivateUsage]
from nacho.core.logger.queue import OverflowPolicy


def record(message: str) -> LogRecord:
    return LogRecord(message=message, logger_name="test")


def make(*messages: str) -> list[LogRecord]:
    return [record(message) for message in messages]


def flush_tasks() -> list[asyncio.Task[object]]:
    """当前存活的定时刷盘任务（按实现里的任务名识别）。"""
    return [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("nacho-log-flush-")
    ]


async def wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> bool:
    """轮询等待条件成立，用于验证定时刷盘这类异步副作用。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


class RecordingProcessor(BaseLogProcessor):
    """最小可用的处理机：把每批写入原样记下来，并可人为注入失败。"""

    name: str = "recording"

    def __init__(
        self,
        *,
        buffer_size: int = 200,
        flush_interval: float = 2.0,
        max_failures: int = 5,
        overflow_policy: OverflowPolicy | str = OverflowPolicy.DROP_OLDEST,
    ) -> None:
        super().__init__(
            buffer_size=buffer_size,
            flush_interval=flush_interval,
            max_failures=max_failures,
            overflow_policy=overflow_policy,
        )
        #: 已成功写入的分批内容，用于断言「写了什么、写了几批」
        self.batches: list[list[str]] = []
        #: 还需要失败几次；>0 时 ``write`` 抛异常
        self.fail_times: int = 0
        self.start_calls: int = 0
        self.stop_calls: int = 0

    async def write(self, records: list[LogRecord]) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("后端写入失败（测试构造）")
        self.batches.append([item.message for item in records])

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
    ) -> list[LogRecord]:
        # 基类不会调用 search，测试替身保持平凡实现即可
        return []

    async def _on_start(self) -> None:
        self.start_calls += 1

    async def _on_stop(self) -> None:
        self.stop_calls += 1


class TestConstruction:
    """构造：入参校验、溢出策略归一化与初始状态。"""

    @pytest.mark.parametrize("buffer_size", [0, -1, -200])
    def test_non_positive_buffer_size_raises(self, buffer_size: int) -> None:
        with pytest.raises(ValueError):
            RecordingProcessor(buffer_size=buffer_size)

    def test_defaults_are_reported_in_stats(self) -> None:
        processor = RecordingProcessor()
        assert processor.stats == {
            "name": "recording",
            "healthy": True,
            "running": False,
            "pending": 0,
            "written": 0,
            "failed": 0,
            "dropped": 0,
            "buffer_size": 200,
            "flush_interval": 2.0,
            "overflow_policy": "drop_oldest",
        }

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (OverflowPolicy.DROP_NEWEST, "drop_newest"),
            (OverflowPolicy.BLOCK, "block"),
            ("drop_oldest", "drop_oldest"),
        ],
    )
    def test_overflow_policy_accepts_enum_and_str(
        self, raw: OverflowPolicy | str, expected: str
    ) -> None:
        processor = RecordingProcessor(overflow_policy=raw)
        assert processor.stats["overflow_policy"] == expected

    def test_unknown_overflow_policy_raises(self) -> None:
        with pytest.raises(ValueError):
            RecordingProcessor(overflow_policy="whatever")

    def test_repr_contains_class_name_and_health(self) -> None:
        processor = RecordingProcessor()
        assert repr(processor) == "<RecordingProcessor name='recording' healthy=True>"


class TestAbstraction:
    """后端契约：``write`` 与 ``search`` 必须由子类实现。"""

    def test_base_class_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            BaseLogProcessor()  # pyright: ignore[reportAbstractUsage]

    def test_subclass_missing_search_is_still_abstract(self) -> None:
        class WriteOnly(BaseLogProcessor):
            async def write(self, records: list[LogRecord]) -> None:
                return None

        with pytest.raises(TypeError):
            WriteOnly()  # pyright: ignore[reportAbstractUsage]

    def test_name_defaults_to_base_and_can_be_overridden(self) -> None:
        assert BaseLogProcessor.name == "base"
        assert RecordingProcessor.name == "recording"


class TestWatermark:
    """水位线刷盘：攒到 ``buffer_size`` 就写，单次 ``write`` 不超过水位线。"""

    async def test_records_below_watermark_stay_pending(self) -> None:
        processor = RecordingProcessor(buffer_size=3)
        await processor.handle(record("a"))
        await processor.handle(record("b"))
        assert processor.pending == 2
        assert processor.batches == []

    async def test_reaching_watermark_flushes_immediately(self) -> None:
        processor = RecordingProcessor(buffer_size=3)
        for message in ("a", "b", "c"):
            await processor.handle(record(message))
        assert processor.pending == 0
        assert processor.batches == [["a", "b", "c"]]
        assert processor.stats["written"] == 3

    async def test_handle_many_slices_by_watermark_and_keeps_remainder(self) -> None:
        """不足一批的尾部余量不刷，留在缓冲区等定时刷盘或显式 flush。"""
        processor = RecordingProcessor(buffer_size=2)
        await processor.handle_many(make("0", "1", "2", "3", "4"))
        assert processor.batches == [["0", "1"], ["2", "3"]]
        assert processor.pending == 1
        assert processor.stats["written"] == 4

        await processor.flush()
        assert processor.batches == [["0", "1"], ["2", "3"], ["4"]]
        assert processor.pending == 0
        assert processor.stats["written"] == 5

    async def test_every_write_batch_respects_watermark(self) -> None:
        processor = RecordingProcessor(buffer_size=3)
        await processor.handle_many(make(*[str(index) for index in range(7)]))
        await processor.flush()  # 补刷余量，让每批都可见
        assert [len(batch) for batch in processor.batches] == [3, 3, 1]

    async def test_empty_input_writes_nothing(self) -> None:
        processor = RecordingProcessor(buffer_size=2)
        await processor.handle_many([])
        assert processor.batches == []
        assert processor.stats["written"] == 0

    async def test_flush_drains_remaining_partial_batch(self) -> None:
        processor = RecordingProcessor(buffer_size=3)
        await processor.handle_many(make("a", "b"))
        await processor.flush()
        assert processor.batches == [["a", "b"]]
        assert processor.pending == 0

    async def test_flush_on_empty_buffer_is_noop(self) -> None:
        processor = RecordingProcessor(buffer_size=3)
        await processor.flush()
        await processor.flush()
        assert processor.batches == []

    async def test_backpressure_flushes_before_staging(self) -> None:
        """缓冲区装不下时先刷盘腾空间（背压），既不丢日志也不超容量。"""
        processor = RecordingProcessor(buffer_size=3)
        await processor.handle_many(make("a", "b"))
        assert processor.pending == 2

        await processor.handle_many(make("c", "d", "e"))
        assert processor.batches == [["a", "b"], ["c", "d", "e"]]
        assert processor.pending == 0
        assert processor.stats["written"] == 5
        assert processor.stats["dropped"] == 0

    async def test_pending_never_exceeds_buffer_size(self) -> None:
        processor = RecordingProcessor(buffer_size=4)
        await processor.handle_many(make(*[str(index) for index in range(10)]))
        assert processor.pending < 4
        assert all(len(batch) <= 4 for batch in processor.batches)


class TestFailureIsolation:
    """失败隔离：写失败不上抛、只记账，连续失败到阈值自动停用。"""

    async def test_write_failure_is_swallowed_and_counted(self) -> None:
        processor = RecordingProcessor(buffer_size=2, max_failures=5)
        processor.fail_times = 1
        await processor.handle_many(make("a", "b"))
        assert processor.healthy is True
        assert processor.stats["failed"] == 2  # 按条计，不按批计
        assert processor.batches == []
        assert processor.pending == 0  # 失败的那批不会退回缓冲区

    async def test_single_record_failure_does_not_raise(self) -> None:
        processor = RecordingProcessor(buffer_size=1, max_failures=3)
        processor.fail_times = 1
        await processor.handle(record("a"))
        assert processor.stats["failed"] == 1
        assert processor.healthy is True

    async def test_repeated_failures_disable_processor(self) -> None:
        processor = RecordingProcessor(buffer_size=1, max_failures=3)
        processor.fail_times = 99
        for message in ("a", "b", "c"):
            await processor.handle(record(message))
        assert processor.healthy is False
        assert processor.stats["failed"] == 3

    async def test_disabled_processor_drops_new_records(self) -> None:
        processor = RecordingProcessor(buffer_size=1, max_failures=1)
        processor.fail_times = 99
        await processor.handle(record("a"))  # 连续失败达阈值，停用
        assert processor.healthy is False

        await processor.handle_many(make("b", "c"))
        assert processor.stats["failed"] == 1  # 停用后不再尝试写入
        assert processor.pending == 0

    async def test_success_resets_consecutive_failure_counter(self) -> None:
        processor = RecordingProcessor(buffer_size=1, max_failures=2)
        processor.fail_times = 1
        await processor.handle(record("a"))  # 失败 1 次，未到阈值
        await processor.handle(record("b"))  # 成功，连续失败清零
        processor.fail_times = 1
        await processor.handle(record("c"))  # 再失败 1 次也不会被停用

        assert processor.healthy is True
        assert processor.batches == [["b"]]
        assert processor.stats["failed"] == 2
        assert processor.stats["written"] == 1

    async def test_disabling_is_logged_as_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        processor = RecordingProcessor(buffer_size=1, max_failures=1)
        processor.fail_times = 99
        with caplog.at_level(logging.ERROR, logger="nacho.core.logger.processor"):
            await processor.handle(record("a"))
        assert any(item.levelno == logging.ERROR for item in caplog.records)


class TestLifecycle:
    """生命周期：启动 / 停止、定时刷盘、停机刷余量。"""

    async def test_start_and_stop_call_hooks_once(self) -> None:
        processor = RecordingProcessor(buffer_size=3, flush_interval=0)
        await processor.start()
        await processor.start()  # 重复启动是幂等的
        assert processor.running is True
        assert processor.start_calls == 1

        await processor.stop()
        await processor.stop()  # 重复停止也是幂等的
        assert processor.running is False
        assert processor.stop_calls == 1

    async def test_stop_without_start_is_noop(self) -> None:
        processor = RecordingProcessor(buffer_size=3)
        await processor.stop()
        assert processor.running is False
        assert processor.stop_calls == 0

    async def test_flush_task_created_only_when_interval_positive(self) -> None:
        periodic = RecordingProcessor(buffer_size=3, flush_interval=0.01)
        manual = RecordingProcessor(buffer_size=3, flush_interval=0)
        await periodic.start()
        await manual.start()
        assert len(flush_tasks()) == 1
        await periodic.stop()
        await manual.stop()
        assert flush_tasks() == []

    async def test_periodic_flush_writes_pending_records(self) -> None:
        processor = RecordingProcessor(buffer_size=10, flush_interval=0.01)
        await processor.start()
        await processor.handle(record("a"))
        assert processor.pending == 1  # 未到水位线，只能等定时刷盘
        try:
            assert await wait_until(lambda: bool(processor.batches)) is True
        finally:
            await processor.stop()
        assert processor.batches == [["a"]]

    async def test_stop_flushes_remaining_records(self) -> None:
        processor = RecordingProcessor(buffer_size=10, flush_interval=0)
        await processor.start()
        await processor.handle(record("a"))
        assert processor.batches == []

        await processor.stop()
        assert processor.batches == [["a"]]
        assert processor.stats["written"] == 1

    async def test_stop_survives_flush_failure(self) -> None:
        processor = RecordingProcessor(buffer_size=10, flush_interval=0, max_failures=5)
        await processor.start()
        processor.fail_times = 1
        await processor.handle(record("a"))

        await processor.stop()  # 刷余量失败也绝不上抛
        assert processor.running is False
        assert processor.stats["failed"] == 1
        assert processor.stop_calls == 1

    async def test_restart_resets_health(self) -> None:
        processor = RecordingProcessor(buffer_size=1, flush_interval=0, max_failures=1)
        processor.fail_times = 99
        await processor.start()
        await processor.handle(record("a"))
        assert processor.healthy is False

        await processor.stop()
        await processor.start()
        assert processor.healthy is True
        await processor.stop()


class TestLogBuffer:
    """有界缓冲区：容量是硬保证，装不下时按策略取舍并计数。"""

    async def test_extend_if_room_accepts_within_capacity(self) -> None:
        buffer = _LogBuffer(capacity=3, policy=OverflowPolicy.DROP_OLDEST)
        assert await buffer.extend_if_room(make("a", "b")) is True
        assert buffer.pending == 2

    async def test_extend_if_room_fills_exactly_to_capacity(self) -> None:
        buffer = _LogBuffer(capacity=3, policy=OverflowPolicy.DROP_OLDEST)
        await buffer.extend_if_room(make("a"))
        assert await buffer.extend_if_room(make("b", "c")) is True
        assert buffer.pending == 3

    async def test_extend_if_room_rejects_and_leaves_buffer_untouched(self) -> None:
        buffer = _LogBuffer(capacity=3, policy=OverflowPolicy.DROP_OLDEST)
        await buffer.extend_if_room(make("a", "b"))
        assert await buffer.extend_if_room(make("c", "d")) is False
        assert buffer.pending == 2
        assert buffer.dropped == 0

    async def test_take_returns_fifo_batch_and_removes_it(self) -> None:
        buffer = _LogBuffer(capacity=5, policy=OverflowPolicy.DROP_OLDEST)
        await buffer.extend_if_room(make("a", "b", "c"))
        batch = await buffer.take(2)
        assert [item.message for item in batch] == ["a", "b"]
        assert buffer.pending == 1

    async def test_take_more_than_available_returns_everything(self) -> None:
        buffer = _LogBuffer(capacity=5, policy=OverflowPolicy.DROP_OLDEST)
        await buffer.extend_if_room(make("a"))
        assert [item.message for item in await buffer.take(10)] == ["a"]
        assert buffer.pending == 0

    async def test_take_on_empty_buffer_returns_empty_list(self) -> None:
        buffer = _LogBuffer(capacity=5, policy=OverflowPolicy.DROP_OLDEST)
        assert await buffer.take(3) == []

    async def test_initial_state_is_empty_and_clean(self) -> None:
        buffer = _LogBuffer(capacity=2, policy=OverflowPolicy.DROP_OLDEST)
        assert buffer.pending == 0
        assert buffer.dropped == 0

    async def test_policy_value_exposes_enum_value(self) -> None:
        buffer = _LogBuffer(capacity=1, policy=OverflowPolicy.DROP_NEWEST)
        assert buffer.policy_value == "drop_newest"

    async def test_force_extend_drop_newest_discards_incoming(self) -> None:
        buffer = _LogBuffer(capacity=2, policy=OverflowPolicy.DROP_NEWEST)
        await buffer.extend_if_room(make("a", "b"))

        await buffer.force_extend(make("c", "d"))
        assert buffer.pending == 2
        assert buffer.dropped == 2
        assert [item.message for item in await buffer.take(2)] == ["a", "b"]

    async def test_force_extend_drop_oldest_keeps_newest_within_capacity(self) -> None:
        buffer = _LogBuffer(capacity=2, policy=OverflowPolicy.DROP_OLDEST)
        await buffer.extend_if_room(make("a", "b"))

        await buffer.force_extend(make("c", "d"))
        assert buffer.pending == 2  # 容量是硬上限
        assert buffer.dropped == 2
        assert [item.message for item in await buffer.take(2)] == ["c", "d"]

    async def test_force_extend_block_degrades_to_drop_oldest(self) -> None:
        """刷盘也腾不出空间时 BLOCK 无法等待，退化为丢弃最旧，保证不阻塞。"""
        buffer = _LogBuffer(capacity=1, policy=OverflowPolicy.BLOCK)
        await buffer.extend_if_room(make("a"))

        await buffer.force_extend(make("b"))
        assert buffer.pending == 1
        assert buffer.dropped == 1
        assert [item.message for item in await buffer.take(1)] == ["b"]
