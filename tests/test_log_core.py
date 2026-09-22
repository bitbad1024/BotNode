"""日志核心实例、子实例配置继承与进程门面的单元测试。

对应 ``nacho/core/logger/core.py``、``manager.py`` 与新增的控制台处理机：

* 最小化启动：``LogCore()`` 天生带一路控制台输出，``start()`` 后立刻可见；
* 运行期挂载：``attach`` 之后不用手动启动处理机，下一批日志就会喂给它；
* 子实例 = 名字 + 一份落回配置 + 自己的出口：``child`` 派生时把父实例实际会投的
  处理机与过滤器复制成落回配置；子实例自己挂了出口就只投自层那些（自层覆盖），
  没挂才整份走落回配置。落回配置创建即冻结，父实例之后再挂 / 再摘都不回头影响
  已建好的子实例，也不沿名字逐层累加；
* 进程门面：``configure`` 重复调用不再丢参数，``get_logger`` 返回共享核心的子实例。
"""
from __future__ import annotations

import asyncio
import io
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from nacho.core.logger import (
    BaseLogProcessor,
    ConsoleLogProcessor,
    LevelFilter,
    LocalFileLogProcessor,
    LogCore,
    LogFilter,
    LogRecord,
    attach_mount,
    configure,
    current_default_core,
    default_core,
    get_logger,
    manager,
)
from nacho.core.logger.models import LogLevel, TimestampLike


async def wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> bool:
    """轮询等待条件成立，用于验证异步分发这类副作用。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


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
        logger = LogCore("nacho.robot", console_stream=stream, dispatch_timeout=0.01)
        await logger.start()
        try:
            logger.warning("机器人电压偏低", battery=12.5)
            assert await wait_until(lambda: "机器人电压偏低" in stream.getvalue()) is True
        finally:
            await logger.stop()

        text = stream.getvalue()
        assert "WARNING" in text
        assert "nacho.robot" in text
        assert "battery" in text

    async def test_console_renders_owner_only_when_given(self) -> None:
        """有所有者的日志在实例名后带上 ``(谁的)``；公共日志（空串）不加那对括号。"""
        stream = io.StringIO()
        logger = LogCore("nacho.api", console_stream=stream, dispatch_timeout=0.01)
        await logger.start()
        try:
            logger.info("请求完成", owner_id="u-admin")
            logger.info("框架启动")
            assert await wait_until(lambda: "框架启动" in stream.getvalue()) is True
        finally:
            await logger.stop()

        text = stream.getvalue()
        assert "nacho.api(u-admin) 请求完成" in text
        assert "nacho.api 框架启动" in text  # 没归属就不挂括号

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
            assert await logger.search() == []
        finally:
            await logger.stop()

    async def test_console_search_returns_unsupported_hint(self) -> None:
        """控制台不留存日志：检索返回一条提示记录，而不是静默空列表。"""
        logger = LogCore(dispatch_timeout=0.01)
        await logger.start()
        try:
            logger.info("检索不到")
            results = await logger.search()
        finally:
            await logger.stop()

        assert len(results) == 1
        assert results[0].level is LogLevel.WARNING
        assert results[0].logger_name == "console"
        assert results[0].extra["search_supported"] is False


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
            logger.attach(processor)
            assert processor.running is False  # 挂载只是登记

            logger.info("动态挂载的通道")
            assert await wait_until(lambda: processor.received == ["动态挂载的通道"]) is True
            assert processor.running is True  # 由分发器补启动，无需手动 start
            assert processor.start_calls == 1
        finally:
            await logger.stop()

    async def test_detach_stops_delivery(self) -> None:
        processor = CollectingProcessor()
        logger = LogCore(console=False, processors=[processor], dispatch_timeout=0.01)
        await logger.start()
        try:
            logger.info("第一条")
            assert await wait_until(lambda: processor.received == ["第一条"]) is True

            assert logger.detach("collecting") is processor
            assert logger.get_processor("collecting") is None

            watcher = CollectingProcessor(name="watcher")
            logger.attach(watcher)
            logger.info("第二条")
            # watcher 收到第二条，说明这一批已经分发完毕，处理机确实被摘掉了
            assert await wait_until(lambda: watcher.received == ["第二条"]) is True
            assert processor.received == ["第一条"]
            assert logger.detach("collecting") is None
        finally:
            await logger.stop()

    async def test_same_name_needs_replace(self) -> None:
        first = CollectingProcessor()
        logger = LogCore(console=False, processors=[first])
        second = CollectingProcessor()

        with pytest.raises(ValueError):
            logger.attach(second)
        logger.attach(second, replace=True)
        assert logger.processors == [second]

    def test_non_processor_is_rejected(self) -> None:
        logger = LogCore(console=False)
        with pytest.raises(TypeError):
            logger.attach(object())  # pyright: ignore[reportArgumentType]

    async def test_start_failure_of_new_channel_does_not_affect_others(self) -> None:
        good = CollectingProcessor()
        broken = BrokenStartProcessor()
        logger = LogCore(console=False, processors=[good], dispatch_timeout=0.01)
        await logger.start()
        try:
            logger.attach(broken)
            logger.info("仍然要落盘")
            assert await wait_until(lambda: good.received == ["仍然要落盘"]) is True

            # 启动失败的通道不会被当成已就绪，也不会带着半初始化状态收日志
            assert broken.running is False
            assert broken.received == []
        finally:
            await logger.stop()


class TestModuleConfigCopy:
    """子实例配置：一个名字对应一个实例，自层挂了出口就覆盖，没挂才回落父级。"""

    async def test_module_name_maps_to_one_shared_forward_list(self) -> None:
        """有则载入：同一个名字永远同一条转发列表，重复设置不会重复建出口。"""
        core = LogCore(console=False, dispatch_timeout=0.01)
        first_file = CollectingProcessor(name="a")
        second_file = CollectingProcessor(name="b")

        first = core.child("module_a")
        second = core.child("module_a")

        first.attach(first_file, name="nacho.module_a")
        second.attach(second_file, name="nacho.module_a")

        assert core.routes["nacho.module_a"] == [first_file, second_file]

    async def test_matched_name_only_feeds_its_own_list(self) -> None:
        """命中名字：自层出口只投该模块自己那份，不再带上核心的全量出口。"""
        own = CollectingProcessor(name="own")
        everything = CollectingProcessor(name="local-all")
        core = LogCore(console=False, processors=[everything], dispatch_timeout=0.01)
        core.attach(own, name="nacho.module_a")

        await core.start()
        try:
            core.child("module_a").info("a 的日志")
            core.child("module_b").info("b 的日志")
            assert await wait_until(lambda: own.received == ["a 的日志"]) is True
            assert await wait_until(lambda: everything.received == ["b 的日志"]) is True
        finally:
            await core.stop()

        # module_a 自层覆盖：不再进全量出口；module_b 没自层出口，整份回落核心
        assert own.received == ["a 的日志"]
        assert everything.received == ["b 的日志"]

    async def test_name_without_own_channel_uses_copied_config(self) -> None:
        """没单独挂出口的名字不是「没出口」，而是走从核心复制来的那份落回配置。"""
        everything = CollectingProcessor(name="local-all")
        core = LogCore(console=False, processors=[everything], dispatch_timeout=0.01)
        core.child("module_x")  # 派生时复制核心的解析结果（核心只有 local-all）
        assert core.routes["nacho.module_x"] == [everything]

        await core.start()
        try:
            core.child("module_x").info("第一条")
            core.child("module_x").info("第二条")
            assert await wait_until(lambda: len(everything.received) == 2) is True
        finally:
            await core.stop()

        assert everything.received == ["第一条", "第二条"]  # 一条都没丢

    async def test_submodule_name_is_covered_by_segment_prefix(self) -> None:
        """名字按 ``.`` 逐段派生：``api.robot`` 的落回配置来自 ``api``，``apix`` 不会。"""
        own = CollectingProcessor(name="own")
        everything = CollectingProcessor(name="local-all")
        core = LogCore(console=False, processors=[everything], dispatch_timeout=0.01)
        core.attach(own, name="nacho.api")

        await core.start()
        try:
            core.child("api.robot").info("子模块")
            core.child("apix").info("不是子模块")
            assert await wait_until(lambda: own.received == ["子模块"]) is True
            assert await wait_until(lambda: everything.received == ["不是子模块"]) is True
        finally:
            await core.stop()

        # api.robot 回落 api（自层只有 own），所以不进全量出口；apix 才回落核心
        assert own.received == ["子模块"]
        assert everything.received == ["不是子模块"]

    async def test_parent_mounts_by_name_and_detach_removes_it(self) -> None:
        """父模块也能按名字挂载；卸载后该名字回落核心那份落回配置。"""
        own = CollectingProcessor(name="own")
        everything = CollectingProcessor(name="local-all")
        core = LogCore(console=False, processors=[everything], dispatch_timeout=0.01)
        core.attach(own, name="nacho.module_a")
        assert core.routes["nacho.module_a"] == [own]  # 自层覆盖：只剩 own

        await core.start()
        try:
            core.child("module_a").info("第一")
            assert await wait_until(lambda: own.received == ["第一"]) is True

            core.detach("own")
            assert core.routes["nacho.module_a"] == [everything]  # 自层空了 -> 回落核心

            core.child("module_a").info("第二")
            assert await wait_until(lambda: everything.received == ["第二"]) is True
        finally:
            await core.stop()

        assert own.received == ["第一"]

    async def test_core_and_child_each_write_through_their_own_copy(self) -> None:
        """核心自己与派生出来的名字，写的都是各自那份配置副本。"""
        everything = CollectingProcessor(name="local-all")
        core = LogCore(console=False, processors=[everything], dispatch_timeout=0.01)

        await core.start()
        try:
            core.info("核心自己的日志")
            core.child("nacho.core.queue").info("框架内部日志")
            assert await wait_until(lambda: len(everything.received) == 2) is True
        finally:
            await core.stop()

        assert everything.received == ["核心自己的日志", "框架内部日志"]

    def test_child_name_is_relative_and_qualify_fills_prefix(self) -> None:
        """``child`` 名字相对父实例：``core.child("a1")`` -> ``nacho.a1``；全名原样使用。"""
        core = LogCore("nacho", console=False)
        assert core.child("a1").name == "nacho.a1"
        assert core.child("robot.arm").name == "nacho.robot.arm"
        assert core.child("nacho.a1").name == "nacho.a1"  # 已是全名：原样使用
        assert core.qualify("a1") == "nacho.a1"
        assert core.qualify("nacho.a1") == "nacho.a1"

    async def test_relative_child_name_routes_by_full_path(self) -> None:
        """相对 child 写日志时，路由按补全后的完整名字 ``nacho.robot`` 命中出口。"""
        own = CollectingProcessor(name="own")
        everything = CollectingProcessor(name="local-all")
        core = LogCore("nacho", console=False, processors=[everything], dispatch_timeout=0.01)
        core.attach(own, name="nacho.robot")

        await core.start()
        try:
            core.child("robot").info("关节过载")
            assert await wait_until(lambda: own.received == ["关节过载"]) is True
        finally:
            await core.stop()

        assert own.received == ["关节过载"]
        assert everything.received == []  # robot 自层覆盖：核心全量出口收不到

    def test_console_lands_on_the_core_config(self) -> None:
        """最小化启动的控制台挂在核心自己那份配置上，派生时会被复制给子实例。"""
        core = LogCore(dispatch_timeout=0.01)
        assert [processor.name for processor in core.processors] == ["console"]
        assert core.child("api").effective_outputs() == [core.get_processor("console")]

    async def test_stats_reports_every_instance_config(self) -> None:
        """内省接口：stats / routes 看得到每个名字实例实际会投的出口。"""
        everything = CollectingProcessor(name="local-all")
        core = LogCore(console=False, processors=[everything], dispatch_timeout=0.01)
        core.attach(CollectingProcessor(name="a-file"), name="nacho.module_a")
        core.child("module_x")  # 没单独挂出口 -> 落回核心那份的副本

        assert core.stats["routes"] == {
            "nacho": ["local-all"],
            "nacho.module_a": ["a-file"],  # 自层覆盖：只剩本层挂的
            "nacho.module_x": ["local-all"],  # 没自层出口 -> 回落核心
        }
        assert core.routes["nacho.module_a"] == [core.get_processor("a-file")]

    async def test_stats_aggregates_drops_from_both_buffers(self) -> None:
        """两级缓冲都可能丢日志，stats 汇成一个视图，排查只看一处。"""
        core = LogCore(console=False, queue_maxsize=1, dispatch_timeout=0.01)
        # 分发器未启动：队列容量只有 1，第二条按 DROP_OLDEST 挤掉最旧的
        core.info("a")
        core.info("b")

        assert core.stats["dropped"] == {"queue": 1, "buffers": 0, "total": 1}

    async def test_child_shares_queue_and_channels(self) -> None:
        core = LogCore(console=False, dispatch_timeout=0.01)
        child = core.child("api")
        assert child is not core
        assert child.queue is core.queue

        # 通过子实例挂载，核心同样看得到（共享同一份注册表）
        processor = CollectingProcessor()
        child.attach(processor)
        assert core.get_processor("collecting") is processor

        await core.start()
        try:
            core.child("api").info("经子实例写入")
            assert await wait_until(lambda: processor.received == ["经子实例写入"]) is True
            # 子实例不持有分发器，也绝不该自己 start
            assert child.running is False
        finally:
            await core.stop()

    async def test_attach_mount_uses_default_core_and_routes(self) -> None:
        processor = CollectingProcessor()
        attached = attach_mount("module_a", processor)
        assert attached is processor
        # 默认核心带控制台；module_a 那份配置是「核心配置的副本 + 本层挂的」
        core = default_core()
        assert core.routes["nacho.module_a"] == [core.get_processor("console"), processor]

        core = default_core()
        await core.start()
        try:
            get_logger("module_b").info("别的模块")  # 先发，保证它已被分发并丢弃
            get_logger("module_a").info("本模块")
            assert await wait_until(lambda: processor.received == ["本模块"]) is True
            assert processor.received == ["本模块"]
        finally:
            await core.stop()

    async def test_attach_mount_replaces_same_module(self) -> None:
        first = CollectingProcessor()
        attach_mount("module_a", first)
        second = CollectingProcessor()
        attach_mount("module_a", second)

        core = default_core()
        assert core.get_processor("collecting") is second
        assert first not in core.processors
        # 旧实例不会留在配置里，只剩「核心配置的副本 + 新的 second」
        assert core.routes["nacho.module_a"] == [core.get_processor("console"), second]

    async def test_two_cores_are_isolated(self) -> None:
        first = CollectingProcessor()
        second = CollectingProcessor()
        core_a = LogCore("a", console=False, processors=[first], dispatch_timeout=0.01)
        core_b = LogCore("b", console=False, processors=[second], dispatch_timeout=0.01)
        await core_a.start()
        await core_b.start()
        try:
            core_a.info("只进 a")
            assert await wait_until(lambda: first.received == ["只进 a"]) is True
            assert second.received == []
        finally:
            await core_a.stop()
            await core_b.stop()

    async def test_each_module_gets_its_own_file(self, tmp_path: Path) -> None:
        """端到端：一个模块一个文件，两个文件互不混杂。"""
        core = LogCore(name="nacho", console=False, dispatch_timeout=0.01)
        path_a = tmp_path / "module_a.log"
        path_b = tmp_path / "module_b.log"
        attach_mount(
            "module_a",
            LocalFileLogProcessor(path_a, name="local-a", buffer_size=1, flush_interval=0),
            core=core,
        )
        attach_mount(
            "module_b",
            LocalFileLogProcessor(path_b, name="local-b", buffer_size=1, flush_interval=0),
            core=core,
        )
        await core.start()
        try:
            core.child("module_a").info("只进 a")
            core.child("module_b").info("只进 b")

            def both_written() -> bool:
                if not (path_a.exists() and path_b.exists()):
                    return False
                return (
                    "只进 a" in path_a.read_text(encoding="utf-8")
                    and "只进 b" in path_b.read_text(encoding="utf-8")
                )

            assert await wait_until(both_written) is True
        finally:
            await core.stop()

        text_a = path_a.read_text(encoding="utf-8")
        text_b = path_b.read_text(encoding="utf-8")
        assert "只进 a" in text_a
        assert "只进 b" not in text_a
        assert "只进 b" in text_b
        assert "只进 a" not in text_b


class TestChildConfigCopy:
    """配置继承：``child`` 派生时复制父实例的解析结果当落回配置；子层挂了自层出口
    就覆盖父级（不再带上父级文件出口，控制台保留），没挂才整份回落，且不重复投递。"""

    async def test_subname_own_channel_overrides_parent_global(self) -> None:
        """子名字自己挂了出口后只投它，核心的全量出口不再收到该子名字的日志。"""
        root_out = CollectingProcessor(name="root-out")
        own = CollectingProcessor(name="own")
        core = LogCore(console=False, processors=[root_out], dispatch_timeout=0.01)
        core.attach(own, name="nacho.robot")

        await core.start()
        try:
            core.child("nacho.robot.arm").info("关节过载")
            assert await wait_until(lambda: own.received == ["关节过载"]) is True
        finally:
            await core.stop()

        assert own.received == ["关节过载"]
        assert root_out.received == []  # 自层覆盖：核心全量出口收不到

    async def test_local_file_overrides_parent_file_but_keeps_console(self) -> None:
        """自层覆盖：子实例挂了自己的文件后不再进父级文件，但控制台仍保留。"""
        stream = io.StringIO()
        parent_file = CollectingProcessor(name="parent-file")
        own_file = CollectingProcessor(name="own-file")
        core = LogCore(console_stream=stream, dispatch_timeout=0.01)
        core.attach(parent_file)
        core.attach(own_file, name="nacho.robot")

        await core.start()
        try:
            core.child("nacho.robot").info("只进自己那份")
            assert await wait_until(lambda: own_file.received == ["只进自己那份"]) is True
            assert await wait_until(lambda: "只进自己那份" in stream.getvalue()) is True
        finally:
            await core.stop()

        assert own_file.received == ["只进自己那份"]
        assert parent_file.received == []  # 父级文件被覆盖
        assert core.effective_outputs("nacho.robot") == [
            core.get_processor("console"),
            own_file,
        ]

    async def test_nearest_own_layer_wins(self) -> None:
        """层层挂载：只有「最近一层挂了自层出口」的那份生效，不会层层累加。"""
        root_out = CollectingProcessor(name="root-out")
        first = CollectingProcessor(name="first")
        second = CollectingProcessor(name="second")
        core = LogCore(console=False, processors=[root_out], dispatch_timeout=0.01)
        core.attach(first, name="nacho")
        core.attach(second, name="nacho.robot")

        await core.start()
        try:
            core.child("nacho.robot.arm").info("深处")
            assert await wait_until(lambda: second.received == ["深处"]) is True
        finally:
            await core.stop()

        assert second.received == ["深处"]  # robot 自层出口胜出
        assert first.received == []
        assert root_out.received == []

    async def test_nearest_layer_device_receives_each_record_once(self) -> None:
        """父层设备收到子层日志是「回落」的结果，每条只收一次——不是每层各冒泡一遍。"""
        own = CollectingProcessor(name="own")
        core = LogCore(console=False, dispatch_timeout=0.01)
        core.attach(own, name="nacho.robot")

        await core.start()
        try:
            for _ in range(3):
                core.child("nacho.robot.arm").info("一条")
            assert await wait_until(lambda: len(own.received) == 3) is True
        finally:
            await core.stop()

        assert own.received == ["一条", "一条", "一条"]  # 3 条，不是 9 条

    def test_declared_level_is_inherited_downward(self) -> None:
        """父层声明级别，子名字默认继承——不必给每个子模块重复配一遍。"""
        core = LogCore(console=False)
        core.child("nacho.robot").set_level("ERROR")

        arm = core.child("nacho.robot.arm")
        assert arm.effective_level() is LogLevel.ERROR
        assert arm.is_enabled_for("ERROR") is True
        assert arm.is_enabled_for("INFO") is False

    def test_subname_overrides_what_it_keeps_inheriting(self) -> None:
        """继承了再改一部分：子层覆盖级别，同时照旧继承父层的输出设备。"""
        own = CollectingProcessor(name="own")
        core = LogCore(console=False)
        core.child("nacho.robot").set_level("ERROR")
        core.attach(own, name="nacho.robot")
        core.child("nacho.robot.arm").set_level("DEBUG")

        arm = core.child("nacho.robot.arm")
        assert arm.effective_level() is LogLevel.DEBUG
        # 级别被自己覆盖了，设备仍然是继承来的
        assert arm.effective_outputs() == [own]
        # 同级的兄弟不受影响，继续用父层声明的级别
        assert core.child("nacho.robot.leg").effective_level() is LogLevel.ERROR

    def test_level_copy_does_not_look_back(self) -> None:
        """级别也是复制来的：父层改了，已经派生过的子实例维持自己那份不变。"""
        core = LogCore(console=False)
        arm = core.child("nacho.robot.arm")
        assert arm.effective_level() is LogLevel.INFO  # 派生时复制的是默认级别

        core.child("nacho.robot").set_level("WARNING")

        assert arm.effective_level() is LogLevel.INFO  # 冻结：不回头跟着父层变
        assert core.child("nacho.robot.leg").effective_level() is LogLevel.WARNING

    def test_declaration_does_not_leak_to_other_branches(self) -> None:
        """名字按 ``.`` 分段：``robot`` 的声明不影响 ``vision``，也吃不到 ``robotx``。"""
        core = LogCore(console=False)
        core.child("nacho.robot").set_level("ERROR")

        assert core.child("nacho.vision").effective_level() is LogLevel.INFO
        assert core.child("nacho.robotx").effective_level() is LogLevel.INFO

    def test_effective_outputs_are_self_contained_and_introspectable(self) -> None:
        """内省：``effective_outputs`` 给出该名字那份配置副本，只读且不重复。"""
        root_out = CollectingProcessor(name="root-out")
        own = CollectingProcessor(name="own")
        core = LogCore(console=False, processors=[root_out])
        core.attach(own, name="nacho")  # 全名就是核心自己 -> 挂到核心实例上

        assert core.effective_outputs() == [root_out, own]
        # 还没派生过：返回核心这份（因为「现在派生一个」拿到的就是它）
        assert core.effective_outputs("nacho.robot.arm") == [root_out, own]

        arm = core.child("nacho.robot.arm")
        assert core.effective_outputs("nacho.robot.arm") == [root_out, own]  # 派生后是它的副本
        assert arm.effective_outputs() == [root_out, own]
        assert core.routes["nacho"] == [root_out, own]  # 核心自己那份也如实登记

    def test_child_accepts_level_declaration(self) -> None:
        """``child(name, level=...)`` 是「本层声明级别」的简写。"""
        core = LogCore(console=False)
        core.child("nacho.robot", level="DEBUG")

        assert core.child("nacho.robot.arm").effective_level() is LogLevel.DEBUG
        assert core.child("nacho.robot.arm").is_enabled_for("DEBUG") is True


class TestChildConfigFreeze:
    """落回配置冻结：子实例在**派生那一刻**复制一份父实例的解析结果，之后父实例再挂 /
    再摘都不回头影响它；而之后才派生的下层复制到的是**改完**的结果。

    这里与 :class:`TestChildConfigCopy` 是同一套语义的两个侧面：那边验证「复制到了什么」，
    这里验证「复制之后不再变」。
    """

    def test_child_copies_parent_devices_and_freezes(self) -> None:
        """派生时复制父实例解析结果当落回配置，之后父实例再挂出口不再影响它。"""
        root_out = CollectingProcessor(name="root-out")
        first = CollectingProcessor(name="first")
        later = CollectingProcessor(name="later")
        core = LogCore("a", console=False, processors=[root_out])
        b = core.child("a.b")  # 落回配置 = 核心解析结果 -> [root-out]
        b.attach(first, name="a.b")  # 本层挂自层出口 -> b 实际会投 [first]

        core.child("a.b.c", level="INFO")  # 派生：c 的落回配置 = b 解析结果 -> [first]
        assert core.effective_outputs("a.b.c") == [first]

        b.attach(later, name="a.b")  # 父实例之后再挂出口
        assert core.effective_outputs("a.b.c") == [first]  # 冻结：看不到 later
        assert core.effective_outputs("a.b") == [first, later]
        # 改完之后才派生的下层，落回配置是**改完**的结果
        core.child("a.b.d", level="INFO")
        assert core.effective_outputs("a.b.d") == [first, later]

    async def test_frozen_copy_ignores_parent_devices_added_later(self) -> None:
        """端到端：c 的落回配置派生过一次后，b 之后新增的出口不会漏给 c。"""
        root_out = CollectingProcessor(name="root-out")
        p1 = CollectingProcessor(name="p1")
        p2 = CollectingProcessor(name="p2")
        core = LogCore("a", console=False, processors=[root_out], dispatch_timeout=0.01)
        b = core.child("a.b")
        b.attach(p1, name="a.b")
        c = core.child("a.b.c", level="INFO")

        await core.start()
        try:
            c.info("第一次")
            assert await wait_until(lambda: p1.received == ["第一次"]) is True

            b.attach(p2, name="a.b")  # c 的落回配置已经定格，b 之后再挂都不回头影响它
            c.info("第二次")
            assert await wait_until(
                lambda: p1.received == ["第一次", "第二次"]
            ) is True
        finally:
            await core.stop()

        assert p1.received == ["第一次", "第二次"]  # c 手里那份冻结，照旧只进 p1
        assert p2.received == []  # b 后来挂的 p2，c 收不到
        assert root_out.received == []  # c 的落回配置里根本没有 root-out


class TestDispatcherFiltering:
    """过滤器属于分发器：挂载出口时用 ``log_filter`` 交给它，在查找分发时生效。"""

    def test_processor_base_has_no_filter(self) -> None:
        """处理机只管落地，不再自带任何过滤钩子。"""
        assert not hasattr(BaseLogProcessor, "accept")

    def test_non_log_filter_is_rejected(self) -> None:
        core = LogCore(console=False)
        with pytest.raises(TypeError):
            core.attach(CollectingProcessor(), log_filter=object())  # pyright: ignore[reportArgumentType]

    async def test_log_filter_is_applied_by_dispatcher(self) -> None:
        """过滤器把不该进该出口的日志挡在处理机之外，连它的缓冲区都不进。"""

        class KeepOnlyFilter(LogFilter):
            def match(self, record: LogRecord) -> bool:
                return "keep" in record.message

        kept = CollectingProcessor(name="kept")
        everything = CollectingProcessor(name="local-all")
        core = LogCore(console=False, processors=[everything], dispatch_timeout=0.01)
        core.attach(kept, log_filter=KeepOnlyFilter())

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
        """显式给出口挂 LevelFilter：分发器在路由时按级别筛选。"""
        low = CollectingProcessor(name="low")
        high = CollectingProcessor(name="high")
        core = LogCore(console=False, level="DEBUG", dispatch_timeout=0.01)
        core.attach(low)
        core.attach(high, log_filter=LevelFilter("ERROR"))

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
        core.attach(first, log_filter=LevelFilter("ERROR"))

        second = CollectingProcessor(name="file")
        core.attach(second, replace=True)

        await core.start()
        try:
            core.info("info")
            assert await wait_until(lambda: second.received == ["info"]) is True
        finally:
            await core.stop()

        assert first.received == []


class TestManagerFacade:
    """进程门面：configure 增量挂载，get_logger 派生共享核心的子实例。"""

    def test_configure_returns_core_with_console(self) -> None:
        core = configure("nacho", console=False)
        assert isinstance(core, LogCore)
        assert default_core() is core
        assert manager.core is core
        assert manager.root is core

    def test_configure_twice_attaches_incrementally(self) -> None:
        """二次 configure 不再静默丢弃参数，而是增量挂载。"""
        core = configure("nacho", console=False)
        assert core.processors == []

        processor = CollectingProcessor()
        assert configure("nacho", processors=[processor]) is core
        assert core.get_processor("collecting") is processor

    def test_configure_replaces_same_name_channel(self, tmp_path: Path) -> None:
        """换输出路径：同名通道被替换，这是「一个模块换文件」的关键路径。"""
        first = LocalFileLogProcessor(tmp_path / "old.log")
        core = configure("nacho", console=False, processors=[first])

        second = LocalFileLogProcessor(tmp_path / "new.log")
        configure("nacho", processors=[second])

        assert core.processors == [second]
        assert core.get_processor("local") is second

    def test_get_logger_returns_shared_child(self) -> None:
        core = configure("nacho", console=False)
        assert get_logger() is core

        child = get_logger("api")
        assert child is not core
        assert child.queue is core.queue
        assert get_logger("api") is child  # 同名会命中缓存

    async def test_manager_start_stop_delegates_to_core(self) -> None:
        core = configure("nacho", console=False)
        await manager.start()
        try:
            assert core.running is True
        finally:
            await manager.stop()
        assert core.running is False

    def test_reset_detaches_default_core(self) -> None:
        core = configure("nacho", console=False)
        manager.reset()
        assert manager.core is None
        assert manager.root is None
        assert current_default_core() is None
        assert core.running is False  # reset 只解除引用，不停机
