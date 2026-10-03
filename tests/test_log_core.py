"""日志核心实例、子实例配置继承与进程门面的单元测试。

对应 ``tickneko/core/logger/core.py``、``manager.py`` 与新增的控制台处理机：

* 最小化启动：``LogCore()`` 天生带一路控制台输出，``start()`` 后立刻可见；
* 运行期挂载：``attach`` 之后不用手动启动处理机，下一批日志就会喂给它；
* 子实例 = 名字 + 一份落回配置 + 自己的出口：``child`` 派生时把父实例实际会投的
  处理机与过滤器复制成落回配置；子实例自己挂了出口就只投自层那些（自层覆盖），
  没挂才整份走落回配置。落回配置创建即冻结，父实例之后再挂 / 再摘都不回头影响
  已建好的子实例，也不沿名字逐层累加；
* 进程门面：``configure`` 重复调用不再丢参数，``child`` 挂到共享核心的命名层级。
"""
from __future__ import annotations

import asyncio
import io
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tickneko.core.logger import (
    BaseLogProcessor,
    BoundLogger,
    ChildLogger,
    ConsoleLogProcessor,
    LocalFileLogProcessor,
    LogCore,
    LogFilter,
    LogRecord,
    Target,
    configure,
    current_default_core,
    default_core,
    manager,
)
from tickneko.core.logger.models import LogLevel, TimestampLike


async def wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> bool:
    """轮询等待条件成立，用于验证异步分发这类副作用。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


def shards_text(outlet: LocalFileLogProcessor) -> str:
    """把某个文件出口**所有片**读成一段文本（按天分片，一份日志可能不止一个片文件）。"""
    return "".join(
        path.read_text(encoding="utf-8") for path in outlet.shards() if path.exists()
    )


class CollectingProcessor(BaseLogProcessor):
    """把收到的日志原样记下来，便于断言「谁收到了什么」。"""

    name: str = "collecting"

    def __init__(
        self,
        *,
        name: str | None = None,
        buffer_size: int = 1,
        flush_interval: float = 0,
    ) -> None:
        super().__init__(
            name=name,
            buffer_size=buffer_size,
            flush_interval=flush_interval,
        )
        self.received: list[str] = []
        self.start_calls: int = 0
        self.stop_calls: int = 0

    async def write(self, records: list[LogRecord]) -> None:
        self.received.extend(record.message for record in records)

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
    ) -> list[LogRecord]:
        return []

    async def _on_start(self) -> None:
        self.start_calls += 1

    async def _on_stop(self) -> None:
        self.stop_calls += 1


class BrokenStartProcessor(CollectingProcessor):
    """启动就失败的处理机，用于验证「单个通道启动失败不影响其它通道」。"""

    name: str = "broken-start"

    async def _on_start(self) -> None:
        raise RuntimeError("启动失败（测试构造）")


@pytest.fixture(autouse=True)
def _clean_default_core() -> Iterator[None]:
    """每个用例前后都清掉进程默认核心，避免全局状态串味。"""
    manager.reset()
    yield
    manager.reset()


class TestMinimalStartup:
    """最小化启动：实例化即带控制台输出，start 之后立刻可见。"""

    def test_core_comes_with_console_channel(self) -> None:
        logger = LogCore()
        assert isinstance(logger.get_processor("console"), ConsoleLogProcessor)
        assert logger.console_enabled is True

    def test_console_can_be_disabled(self) -> None:
        logger = LogCore(console=False)
        assert logger.processors == []
        assert logger.console_enabled is False

    def test_user_console_channel_is_kept(self) -> None:
        mine = ConsoleLogProcessor(stream=io.StringIO())
        logger = LogCore(processors=[mine])
        assert logger.get_processor("console") is mine
        assert logger.processors == [mine]

    def test_console_channel_does_not_batch(self) -> None:
        """控制台逐条直写：不攒批，避免问题现场被压在缓冲区里。"""
        stats = ConsoleLogProcessor().stats
        assert stats["buffer_size"] == 1
        assert stats["flush_interval"] == 0

    async def test_log_before_start_waits_in_queue(self) -> None:
        stream = io.StringIO()
        logger = LogCore(console_stream=stream, dispatch_timeout=0.01)
        assert logger.info("排队中") is True
        assert stream.getvalue() == ""  # 还没启动分发器，一条都不落

        await logger.start()
        try:
            assert await wait_until(lambda: "排队中" in stream.getvalue()) is True
        finally:
            await logger.stop()
        assert "排队中" in stream.getvalue()

    async def test_console_output_contains_level_logger_and_extra(self) -> None:
        stream = io.StringIO()
        logger = LogCore("tickneko.robot", console_stream=stream, dispatch_timeout=0.01)
        await logger.start()
        try:
            logger.warning("机器人电压偏低", battery=12.5)
            assert await wait_until(lambda: "机器人电压偏低" in stream.getvalue()) is True
        finally:
            await logger.stop()

        text = stream.getvalue()
        assert "WARNING" in text
        assert "tickneko.robot" in text
        assert "battery" in text

    async def test_console_renders_owner_only_when_given(self) -> None:
        """有所有者的日志在实例名后带上 ``(谁的)``；公共日志（空串）不加那对括号。"""
        stream = io.StringIO()
        logger = LogCore("tickneko.api", console_stream=stream, dispatch_timeout=0.01)
        await logger.start()
        try:
            logger.info("请求完成", owner_id="u-admin")
            logger.info("框架启动")
            assert await wait_until(lambda: "框架启动" in stream.getvalue()) is True
        finally:
            await logger.stop()

        text = stream.getvalue()
        assert "tickneko.api(u-admin) 请求完成" in text
        assert "tickneko.api 框架启动" in text  # 没归属就不挂括号

    async def test_console_filters_below_its_level(self) -> None:
        stream = io.StringIO()
        logger = LogCore(
            console_stream=stream, console_level="WARNING", dispatch_timeout=0.01
        )
        await logger.start()
        try:
            logger.info("不该出现")
            logger.error("该出现")
            assert await wait_until(lambda: "该出现" in stream.getvalue()) is True
        finally:
            await logger.stop()

        text = stream.getvalue()
        assert "该出现" in text
        assert "不该出现" not in text

    async def test_console_color_is_opt_in(self) -> None:
        stream = io.StringIO()
        logger = LogCore(
            console_stream=stream, console_color=True, dispatch_timeout=0.01
        )
        await logger.start()
        try:
            logger.info("带颜色")
            assert await wait_until(lambda: "带颜色" in stream.getvalue()) is True
        finally:
            await logger.stop()
        assert "\033[" in stream.getvalue()

    async def test_console_keeps_nothing_for_search(self) -> None:
        logger = LogCore(console=False, dispatch_timeout=0.01)
        await logger.start()
        try:
            logger.info("检索不到")
            found = await logger.search()
            assert (found.records, found.total) == ([], 0)
        finally:
            await logger.stop()

    async def test_console_search_returns_unsupported_hint(self) -> None:
        """控制台不留存日志：检索返回一条提示记录，而不是静默空列表；总数是 0。"""
        logger = LogCore(dispatch_timeout=0.01)
        await logger.start()
        try:
            logger.info("检索不到")
            found = await logger.search()
        finally:
            await logger.stop()

        assert len(found.records) == 1
        assert found.total == 0  # 控制台确实没东西可数
        hint = found.records[0]
        assert hint.level is LogLevel.WARNING
        assert hint.logger_name == "console"
        assert hint.extra["search_supported"] is False


class TestDynamicAttach:
    """运行期挂载：attach 即生效，且自动启动被挂载的处理机。"""

    async def test_channel_attached_before_start_is_started_with_core(self) -> None:
        processor = CollectingProcessor()
        logger = LogCore(console=False, processors=[processor])
        assert processor.running is False

        await logger.start()
        try:
            assert processor.running is True
            assert processor.start_calls == 1
        finally:
            await logger.stop()

    async def test_channel_attached_after_start_is_auto_started(self) -> None:
        processor = CollectingProcessor()
        logger = LogCore(console=False, dispatch_timeout=0.01)
        await logger.start()
        try:
            logger.mount(processor)
            assert processor.running is False  # 挂载只是登记

            logger.info("动态挂载的通道")
            assert await wait_until(lambda: processor.received == ["动态挂载的通道"]) is True
            assert processor.running is True  # 由分发器补启动，无需手动 start
            assert processor.start_calls == 1
        finally:
            await logger.stop()

    async def test_start_failure_of_new_channel_does_not_affect_others(self) -> None:
        good = CollectingProcessor()
        broken = BrokenStartProcessor()
        logger = LogCore(console=False, processors=[good], dispatch_timeout=0.01)
        await logger.start()
        try:
            logger.mount(broken)
            logger.info("仍然要落盘")
            assert await wait_until(lambda: good.received == ["仍然要落盘"]) is True

            # 启动失败的通道不会被当成已就绪，也不会带着半初始化状态收日志
            assert broken.running is False
            assert broken.received == []
        finally:
            await logger.stop()








class TestDispatcherFiltering:
    """过滤器属于分发器：挂载出口时用 ``log_filter`` 交给它，在查找分发时生效。"""

    def test_processor_base_has_no_filter(self) -> None:
        """处理机只管落地，不再自带任何过滤钩子。"""
        assert not hasattr(BaseLogProcessor, "accept")


    async def test_log_filter_is_applied_by_dispatcher(self) -> None:
        """过滤器把不该进该出口的日志挡在处理机之外，连它的缓冲区都不进。"""

        class KeepOnlyFilter(LogFilter):
            def match(self, record: LogRecord) -> bool:
                return "keep" in record.message

        kept = CollectingProcessor(name="kept")
        everything = CollectingProcessor(name="local-all")
        core = LogCore(console=False, processors=[everything], dispatch_timeout=0.01)
        core.mount(kept, log_filter=KeepOnlyFilter())

        await core.start()
        try:
            core.info("keep-1")
            core.info("drop-1")
            assert await wait_until(lambda: len(everything.received) == 2) is True
        finally:
            await core.stop()

        assert kept.received == ["keep-1"]  # 被过滤的日志没进这个出口
        assert kept.pending == 0  # 也没进它的缓冲区

    async def test_level_filter_only_lets_high_levels_through(self) -> None:
        """显式给出口挂 level 门槛：分发器在路由时按级别筛选。"""
        low = CollectingProcessor(name="low")
        high = CollectingProcessor(name="high")
        core = LogCore(console=False, level="DEBUG", dispatch_timeout=0.01)
        core.mount(low)
        core.mount(high, level="ERROR")

        await core.start()
        try:
            core.info("info")
            core.error("error")
            assert await wait_until(lambda: len(low.received) == 2) is True
            assert await wait_until(lambda: high.received == ["error"]) is True
        finally:
            await core.stop()

        assert low.received == ["info", "error"]
        assert high.received == ["error"]

    async def test_replace_without_filter_clears_old_filter(self) -> None:
        """换出口时不带过滤器 -> 旧过滤器一并清掉，新出口全收。"""
        core = LogCore(console=False, dispatch_timeout=0.01)
        first = CollectingProcessor(name="file")
        core.mount(first, level="ERROR")

        second = CollectingProcessor(name="file")
        core.mount(second, replace=True)

        await core.start()
        try:
            core.info("info")
            assert await wait_until(lambda: second.received == ["info"]) is True
        finally:
            await core.stop()

        assert first.received == []



    async def test_mixed_children_in_one_batch_each_to_its_own_outlet(self) -> None:
        """同一批里混着不同名字的子节点：各自投自己的出口，不串。

        目标现在是**随记录走**的（写的时候就钉死在这条记录上），所以分发只是照着投；
        这条是那条主路的回归闸：不同名字的记录不能被并错地方。
        """
        first = CollectingProcessor(name="first")
        second = CollectingProcessor(name="second")
        core = LogCore(console=False, dispatch_timeout=0.01)
        core.child("a", targets=[first])   # tickneko.a -> 只投 first
        core.child("b", targets=[second])  # tickneko.b -> 只投 second

        await core.start()
        try:
            core.child("a").info("一号的事")
            core.child("b").info("二号的事")
            assert await wait_until(lambda: first.received == ["一号的事"]) is True
            assert await wait_until(lambda: second.received == ["二号的事"]) is True
        finally:
            await core.stop()


class TestManagerFacade:
    """进程门面：configure 增量挂载，child 挂到共享核心的命名层级。"""

    def test_configure_returns_core_with_console(self) -> None:
        core = configure("tickneko", console=False)
        assert isinstance(core, LogCore)
        assert default_core() is core
        assert manager.core is core
        assert manager.root is core

    def test_configure_twice_attaches_incrementally(self) -> None:
        """二次 configure 不再静默丢弃参数，而是增量挂载。"""
        core = configure("tickneko", console=False)
        assert core.processors == []

        processor = CollectingProcessor()
        assert configure("tickneko", processors=[processor]) is core
        assert core.get_processor("collecting") is processor

    def test_configure_replaces_same_name_channel(self, tmp_path: Path) -> None:
        """换落点：同名通道被替换（换输出目录 / 换前缀就是这条路）。"""
        first = LocalFileLogProcessor(tmp_path, prefix="old")
        core = configure("tickneko", console=False, processors=[first])

        second = LocalFileLogProcessor(tmp_path, prefix="new")
        configure("tickneko", processors=[second])

        assert core.processors == [second]
        assert core.get_processor("local") is second

    async def test_manager_start_stop_delegates_to_core(self) -> None:
        core = configure("tickneko", console=False)
        await manager.start()
        try:
            assert core.running is True
        finally:
            await manager.stop()
        assert core.running is False

    def test_reset_detaches_default_core(self) -> None:
        core = configure("tickneko", console=False)
        manager.reset()
        assert manager.core is None
        assert manager.root is None
        assert current_default_core() is None
        assert core.running is False  # reset 只解除引用，不停机


class RecordingProcessor(CollectingProcessor):
    """把**整条记录**记下来：断言默认字段时只看消息不够。"""

    name: str = "recording"

    def __init__(self) -> None:
        super().__init__()
        self.records: list[LogRecord] = []

    async def write(self, records: list[LogRecord]) -> None:
        self.records.extend(records)
        await super().write(records)


class TestBoundDefaults:
    """默认字段（``bind``）：一段执行打一次标记，之后每条日志自己带着走。

    ``bind`` 得到的是**视图**而不是新实例：共享队列与出口，不进实例注册表。
    """

    async def test_bound_fields_ride_along_every_record(self) -> None:
        """每条日志自动带上默认字段；当次传的同名键压过默认的。"""
        core = LogCore(console=False)
        processor = RecordingProcessor()
        core.mount(processor)
        await core.start()
        try:
            log = core.bind(workflow_id="w1", user_id="10001")
            log.info("开始")
            log.info("换人", user_id="10002")

            assert await wait_until(lambda: len(processor.records) >= 2)
            assert [(r.message, r.extra) for r in processor.records] == [
                ("开始", {"workflow_id": "w1", "user_id": "10001"}),
                ("换人", {"workflow_id": "w1", "user_id": "10002"}),
            ]
        finally:
            await core.stop()

    async def test_bound_owner_id_is_the_record_owner(self) -> None:
        """``owner_id`` 也能绑：它是日志的一等字段（进 ``owner_id``，不塞 extra）。"""
        core = LogCore(console=False)
        processor = RecordingProcessor()
        core.mount(processor)
        await core.start()
        try:
            log = core.bind(owner_id="u-admin", workflow_id="w1")
            log.info("记在归属名下")
            log.info("临时换个归属", owner_id="u-robot")

            assert await wait_until(lambda: len(processor.records) >= 2)
            assert [(r.owner_id, r.extra) for r in processor.records] == [
                ("u-admin", {"workflow_id": "w1"}),
                ("u-robot", {"workflow_id": "w1"}),
            ]
        finally:
            await core.stop()

    def test_bind_is_a_view_not_another_instance(self) -> None:
        """视图共享源实例的队列与出口，本身不登记任何东西。"""
        core = LogCore(console=False)
        processor = RecordingProcessor()
        core.mount(processor)

        log = core.bind(workflow_id="w1")
        assert log.name == core.name  # 不换名字
        assert dict(log.defaults) == {"workflow_id": "w1"}
        assert core.get_processor("recording") is processor  # 出口还是那一份
        assert [target.processor for target in core.targets] == [processor]
        # 叠一层：同名按新的，原视图不变
        assert dict(log.bind(workflow_id="w2").defaults) == {"workflow_id": "w2"}
        assert dict(log.defaults) == {"workflow_id": "w1"}

    async def test_write_merges_defaults_into_the_record(self) -> None:
        """直接 ``write`` 一条记录时同样并上默认字段（当次的优先）。"""
        core = LogCore(console=False)
        processor = RecordingProcessor()
        core.mount(processor)
        await core.start()
        try:
            core.bind(workflow_id="w1", user_id="10001").write(
                LogRecord(message="手工构造的一条", extra={"user_id": "10002"})
            )
            assert await wait_until(lambda: len(processor.records) >= 1)
            assert processor.records[0].extra == {"workflow_id": "w1", "user_id": "10002"}
        finally:
            await core.stop()

    async def test_no_defaults_write_is_passthrough(self) -> None:
        """一个字段都没绑的视图：``write`` 原样投递，不重建记录。"""
        core = LogCore(console=False)
        processor = RecordingProcessor()
        core.mount(processor)
        await core.start()
        try:
            record = LogRecord(message="原样", extra={"k": "v"})
            core.bind().write(record)

            assert await wait_until(lambda: len(processor.records) >= 1)
            assert processor.records[0] is record  # 没被 replace 过
            assert processor.records[0].extra == {"k": "v"}
        finally:
            await core.stop()

    async def test_one_view_is_safe_to_share_across_tasks(self) -> None:
        """一份视图被多个协程同时拿着写：默认字段只读，当次字段互不污染。"""

        async def emit(log: BoundLogger, user_id: str) -> None:
            log.info("忙活", user_id=user_id)

        core = LogCore(console=False)
        processor = RecordingProcessor()
        core.mount(processor)
        await core.start()
        try:
            log = core.bind(workflow_id="w1")
            await asyncio.gather(*(emit(log, f"u-{i}") for i in range(8)))

            assert await wait_until(lambda: len(processor.records) >= 8)
            assert {r.extra["workflow_id"] for r in processor.records} == {"w1"}
            assert {r.extra["user_id"] for r in processor.records} == {f"u-{i}" for i in range(8)}
        finally:
            await core.stop()

    def test_bind_never_grows_the_registry(self) -> None:
        """``bind`` 视图不进注册表：叠多少层、绑多少个 id，注册表都不变长。

        这条是「请求级数据不许拿 ``bind`` 当实例用」的保险丝：哪天有人为了省参数去按
        用户 / 按请求绑定出名字（``tickneko.api.user-42`` 那种），注册表就成了只增不减的
        字典 —— 那是 :meth:`BaseLogger.child` 该操心的事。
        """
        core = LogCore(console=False)
        registry_before = len(core.processor_registry)

        view = core.bind(trace_id="t1")
        for index in range(50):
            _ = view.bind(step=index)
            _ = core.bind(user_id=f"u-{index}")

        assert len(core.processor_registry) == registry_before
        assert dict(view.defaults) == {"trace_id": "t1"}  # 原视图没被叠坏


class LabelledProcessor(CollectingProcessor):
    """把「谁收到了」按投递顺序记进一份共享列表（验收投放优先级）。"""

    def __init__(self, label: str, sink: list[str]) -> None:
        super().__init__(name=label, buffer_size=1, flush_interval=0)
        self.label: str = label
        self.sink: list[str] = sink

    async def write(self, records: list[LogRecord]) -> None:
        self.sink.extend([self.label] * len(records))


class TestBoundTargets:
    """``bind(targets=...)``：**目标随记录走**，不再经过名字表。

    这是替代 child / attach 的那条路：一条日志投给谁在写入那一刻就定了，
    名字退回来只当标签用。
    """

    async def test_every_record_carries_its_targets(self) -> None:
        """普通入口写出的记录也带目标：目标在写入那一刻定死，分发只剩照着投。"""
        outlet = RecordingProcessor()
        core = LogCore(console=False, processors=[outlet])
        await core.start()
        try:
            core.info("一条")
            assert await wait_until(lambda: len(outlet.records) >= 1)

            targets = outlet.records[0].targets
            assert targets is not None
            assert [target.processor for target in targets] == [outlet]
        finally:
            await core.stop()

    async def test_target_snapshot_is_invalidated_on_mount(self) -> None:
        """目标有缓存，但挂载 / 静音会把它作废旧重算 —— 缓存不能骗自己人。"""
        first = CollectingProcessor(name="first")
        second = CollectingProcessor(name="second")
        core = LogCore(console=False, processors=[first])
        await core.start()
        try:
            core.info("挂载前")
            assert await wait_until(lambda: first.received == ["挂载前"]) is True

            core.mount(second)
            core.info("挂载后")
            assert await wait_until(lambda: second.received == ["挂载后"]) is True
            # 同一个核心，挂载前后目标不同：第二次投递要带上新出口
            assert await wait_until(lambda: len(first.received) == 2) is True
        finally:
            await core.stop()

    async def test_target_child_and_mute(self) -> None:
        """名字就是一个挂目标的子节点：堵掉某个出口后，这条路上的日志不再进它。"""
        db = CollectingProcessor(name="database")
        file = CollectingProcessor(name="file")
        core = LogCore(console=False)
        access = core.child("api.access", targets=[Target(db, priority=-1), Target(file)])
        quiet = access.bind().mute("database")

        assert access.name == "tickneko.api.access"
        assert [target.processor for target in quiet.targets or ()] == [db, file]

        await core.start()
        try:
            quiet.info("一条访问流水")
            assert await wait_until(lambda: file.received == ["一条访问流水"]) is True
        finally:
            await core.stop()

        assert db.received == []  # 被堵的那一路一条都没收

    async def test_bound_targets_override_name_based_routing(self) -> None:
        """绑了目标就只投绑定的那些：核心那份收不到（哪怕名字还是核心那条）。"""
        core_file = CollectingProcessor(name="core-file")
        module_file = RecordingProcessor()
        core = LogCore(console=False, processors=[core_file])
        await core.start()
        try:
            core.info("走核心那份")
            module = core.bind(name="tickneko.module", targets=[module_file])
            module.info("走模块那份")

            assert await wait_until(lambda: core_file.received == ["走核心那份"]) is True
            assert await wait_until(lambda: module_file.received == ["走模块那份"]) is True
        finally:
            await core.stop()

        assert module_file.records[-1].logger_name == "tickneko.module"  # 名字只是标签

    async def test_target_filter_applies_per_target(self) -> None:
        """过滤器跟着目标走：同一个出口 Filter 只在它那一份上生效。"""

        class KeepFilter(LogFilter):
            def match(self, record: LogRecord) -> bool:
                return "keep" in record.message

        kept = CollectingProcessor(name="kept")
        everything = CollectingProcessor(name="everything")
        core = LogCore(console=False)
        await core.start()
        try:
            log = core.bind(targets=[Target(kept, KeepFilter()), everything])
            log.info("keep-1")
            log.info("drop-1")

            assert await wait_until(lambda: len(everything.received) >= 2) is True
        finally:
            await core.stop()

        assert kept.received == ["keep-1"]
        assert everything.received == ["keep-1", "drop-1"]

    async def test_priority_decides_delivery_order(self) -> None:
        """投放顺序按目标的 ``priority``（小的先投），与写在列表里的先后无关。"""
        sink: list[str] = []
        late = LabelledProcessor("late", sink)
        early = LabelledProcessor("early", sink)
        core = LogCore(console=False)
        await core.start()
        try:
            log = core.bind(targets=[Target(late, priority=10), Target(early, priority=-1)])
            log.info("一条")

            assert await wait_until(lambda: len(sink) >= 2) is True
        finally:
            await core.stop()

        assert sink == ["early", "late"]

    async def test_processor_bound_only_is_adopted_and_flushed(self) -> None:
        """只出现在某个 bind 里的处理机：被接纳进清单，停机照样 flush 而不是丢。"""
        outlet = CollectingProcessor(name="bound-only", buffer_size=100)
        core = LogCore(console=False)
        log = core.bind(targets=[outlet])

        assert core.get_processor("bound-only") is outlet  # 已被接纳

        await core.start()
        try:
            log.info("一条")  # 攒在缓冲区里（buffer_size=100，不会自动刷）
        finally:
            await core.stop()  # 停机 flush 所有已接纳的出口

        assert outlet.received == ["一条"]


class TestChildLogger:
    """child 命名层级：缓存树 + 创建时固化，与 bind 的类型分工。"""

    def test_child_name_is_qualified_hierarchically(self) -> None:
        """相对名补全为完整层级名；再 child 一层继续拼；全名写法等价。"""
        core = LogCore(console=False)
        a = core.child("a")
        assert isinstance(a, ChildLogger)
        assert a.name == "tickneko.a"
        assert a.child("b").name == "tickneko.a.b"
        # root.child("a.b") 与 root.child("a").child("b") 名字一致、行为等价
        assert core.child("a.b").name == "tickneko.a.b"
        assert core.child("tickneko.a.b").name == "tickneko.a.b"  # 全名原样返回

    def test_child_cache_returns_same_object(self) -> None:
        """同名 child 命中缓存：返回同一对象，不重复固化。"""
        core = LogCore(console=False)
        assert core.child("vision") is core.child("vision")
        a = core.child("a")
        assert a.child("b") is a.child("b")
        # 路径一致的两条调用链，逐层都命中缓存
        assert core.child("a").child("b") is core.child("a").child("b")

    def test_child_snapshots_level_and_targets_at_creation(self) -> None:
        """创建时固化：父后改级别 / 挂出口不影响已派生节点。"""
        core = LogCore(console=False)
        first = CollectingProcessor(name="first")
        core.mount(first)
        child = core.child("a")
        frozen_level = child.level
        frozen_targets = child.targets

        core.set_level("ERROR")
        core.mount(CollectingProcessor(name="later"))

        assert child.level is frozen_level  # 级别仍是创建时那份
        assert child.targets is frozen_targets  # 目标仍是创建时那份
        assert child.is_enabled_for(LogLevel.INFO) is True  # 不受 root 改级别影响
        assert core.is_enabled_for(LogLevel.INFO) is False  # root 自己确实改了
        assert [target.processor for target in child.targets] == [first]  # 后挂的出口不在其中

    async def test_child_does_not_repeat_delivery_to_root(self) -> None:
        """无父级重复投递：挂了自己目标的子节点只投那一份，root 收不到。"""
        root_outlet = CollectingProcessor(name="root")
        child_outlet = CollectingProcessor(name="child")
        core = LogCore(console=False, processors=[root_outlet])
        child = core.child("robot", targets=[child_outlet])

        await core.start()
        try:
            child.info("机器人专属")
            assert await wait_until(lambda: child_outlet.received == ["机器人专属"]) is True
        finally:
            await core.stop()

        assert root_outlet.received == []  # 不沿父链重复投递（无 propagate）

    def test_bind_product_has_no_child(self) -> None:
        """bind() 产物是上下文视图：不能再 child 生长（类型分离）。"""
        core = LogCore(console=False)
        assert not hasattr(core.bind(workflow_id="w1"), "child")
        assert not hasattr(core.child("a").bind(workflow_id="w1"), "child")

    async def test_child_then_bind_combines_semantics(self) -> None:
        """child().bind() 组合：名字取子节点、目标取固化份、默认字段随记录走。"""
        outlet = RecordingProcessor()
        core = LogCore(console=False)
        child = core.child("api.access", targets=[outlet])
        log = child.bind(owner_id="u-admin", trace_id="t1")
        assert log.name == "tickneko.api.access"

        await core.start()
        try:
            log.info("一条访问")
            log.info("换个归属", owner_id="u-robot")
            assert await wait_until(lambda: len(outlet.records) >= 2) is True
        finally:
            await core.stop()

        assert [(r.logger_name, r.owner_id, r.extra) for r in outlet.records] == [
            ("tickneko.api.access", "u-admin", {"trace_id": "t1"}),
            ("tickneko.api.access", "u-robot", {"trace_id": "t1"}),
        ]
