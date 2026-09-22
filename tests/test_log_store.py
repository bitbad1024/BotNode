"""日志落库：:class:`~nacho.db.SqlLogStore`（``logs`` 表）与数据库处理机的接线。

处理机自己不拼 SQL——建表、写入、检索都归 store 管，所以这里两头都测：store 的
写入 / 检索 / 清理，以及处理机确实把缓冲里的日志交给了 store、并把检索原样转出去。

需要 ``sqlmodel`` + ``aiosqlite``（``pip install "nacho[api]"``），没装就整文件跳过。
"""
import time
from collections.abc import AsyncIterator

import pytest

pytest.importorskip("sqlmodel", reason="数据库层要装 sqlmodel：pip install \"nacho[api]\"")
pytest.importorskip("aiosqlite", reason="sqlite 的异步驱动：pip install \"nacho[api]\"")

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402
from sqlmodel.ext.asyncio.session import AsyncSession  # noqa: E402

from nacho.core.logger import DatabaseLogProcessor, LogLevel, LogRecord  # noqa: E402
from nacho.db import LogTable, SqlLogStore  # noqa: E402

#: 本文件用到的内存引擎：每个用例一份，用例结束由下面的 fixture 统一 dispose
_MEMORY_ENGINES: list[AsyncEngine] = []


async def memory_store() -> SqlLogStore:
    """挂在内存 sqlite 上、表已建好的日志存储。"""
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    _MEMORY_ENGINES.append(engine)
    store = SqlLogStore(engine)
    await store.ensure_schema()
    return store


@pytest.fixture(autouse=True)
async def _dispose_memory_engines() -> AsyncIterator[None]:
    """用例结束把上面那些内存引擎关掉（否则连接会跟着事件循环一起悬着）。"""
    yield
    while _MEMORY_ENGINES:
        await _MEMORY_ENGINES.pop().dispose()


def record(
    message: str,
    *,
    level: LogLevel = LogLevel.INFO,
    logger_name: str = "nacho",
    moment: float | None = None,
    extra: dict[str, object] | None = None,
    owner_id: str = "",
) -> LogRecord:
    return LogRecord(
        message=message,
        level=level,
        logger_name=logger_name,
        timestamp=time.time() if moment is None else moment,
        extra={} if extra is None else extra,
        owner_id=owner_id,
    )


# --------------------------------------------------------------------------- 存储：建表 / 写入 / 检索
async def test_ensure_schema_is_idempotent() -> None:
    store = await memory_store()
    await store.ensure_schema()  # 再建一次不报错


async def test_add_empty_batch_writes_nothing() -> None:
    store = await memory_store()
    assert await store.add([]) == 0
    assert await store.search() == []


async def test_add_and_search_roundtrip() -> None:
    """写进去的字段能原样读回来（含 extra 这份 JSON）。"""
    store = await memory_store()
    assert await store.add([record("hello", extra={"robot": "r-1"})]) == 1

    found = await store.search()
    assert len(found) == 1
    assert found[0].message == "hello"
    assert found[0].level is LogLevel.INFO
    assert found[0].logger_name == "nacho"
    assert found[0].extra == {"robot": "r-1"}
    assert found[0].exc_text is None


async def test_search_filters_and_order() -> None:
    """级别 / 实例名精确、正文模糊、时间闭区间、分页，以及「按时间倒序」。"""
    store = await memory_store()
    base = time.time()
    await store.add(
        [
            record("robot 执行第 1 步", logger_name="nacho.robot", moment=base - 10),
            record(
                "robot 执行第 2 步",
                logger_name="nacho.robot",
                moment=base - 5,
                level=LogLevel.ERROR,
            ),
            record("别的模块", logger_name="nacho.api", moment=base),
        ]
    )

    assert [r.message for r in await store.search()] == [
        "别的模块",
        "robot 执行第 2 步",
        "robot 执行第 1 步",
    ]
    assert [r.message for r in await store.search(level="error")] == ["robot 执行第 2 步"]
    assert len(await store.search(logger_name="nacho.robot")) == 2
    assert len(await store.search(query="robot 执行")) == 2
    assert [r.message for r in await store.search(start=base - 6, end=base - 4)] == [
        "robot 执行第 2 步"
    ]
    assert [r.message for r in await store.search(limit=1, offset=1)] == ["robot 执行第 2 步"]


async def test_delete_before_removes_only_older() -> None:
    store = await memory_store()
    base = time.time()
    await store.add([record("旧", moment=base - 100), record("新", moment=base)])

    assert await store.delete_before(base - 50) == 1
    assert [r.message for r in await store.search()] == ["新"]
    assert await store.delete_before(base - 50) == 0  # 已经清干净了
    with pytest.raises(ValueError):
        await store.delete_before(None)  # 没有明确时刻就是非法输入


async def test_broken_extra_does_not_break_search() -> None:
    """库里 extra 坏了（手工改过 / 老数据）：退回原文，而不是让整次检索炸掉。"""
    store = await memory_store()
    async with AsyncSession(_MEMORY_ENGINES[-1]) as session:
        session.add(
            LogTable(
                record_id="r-broken",
                timestamp=time.time(),
                level="INFO",
                logger_name="nacho",
                message="坏数据",
                extra="{不是 JSON",
            )
        )
        await session.commit()

    found = await store.search()
    assert found[0].extra == {"raw": "{不是 JSON"}


# --------------------------------------------------------------------------- 处理机：缓冲 -> store
async def test_processor_writes_through_store() -> None:
    """到水位线即刻把整批交给 store（处理机自己不碰 SQL）。"""
    store = await memory_store()
    processor = DatabaseLogProcessor(store, buffer_size=2, flush_interval=0)
    await processor.start()
    try:
        await processor.handle_many([record("一"), record("二")])
        assert processor.stats["written"] == 2
        found = await processor.search()
        assert sorted(r.message for r in found) == ["一", "二"]
    finally:
        await processor.stop()


async def test_processor_start_builds_table() -> None:
    """表是 ``_on_start`` 建的：没建表的话下面这条写入会失败（处理机只记账、不抛）。"""
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    _MEMORY_ENGINES.append(engine)
    processor = DatabaseLogProcessor(SqlLogStore(engine), buffer_size=5, flush_interval=0)
    await processor.start()
    try:
        await processor.write([record("建表之后才写得进来")])
        assert processor.stats["failed"] == 0
        assert [r.message for r in await processor.search()] == ["建表之后才写得进来"]
    finally:
        await processor.stop()


# --------------------------------------------------------------------------- 所有者
async def test_owner_round_trip_and_filter() -> None:
    """所有者进出数据库不丢，且能按它检索：``None`` 不限、空串只看公共、给值只看那个人。"""
    store = await memory_store()
    await store.add(
        [
            record("公共的一条"),
            record("管理员的一条", owner_id="u-admin"),
            record("机器人的一条", owner_id="u-robot"),
        ]
    )

    assert len(await store.search()) == 3  # 不给就是不限所有者
    assert [r.message for r in await store.search(owner_id="")] == ["公共的一条"]
    assert [r.message for r in await store.search(owner_id="u-admin")] == ["管理员的一条"]
    found = await store.search(owner_id="u-robot")
    assert [(r.message, r.owner_id) for r in found] == [("机器人的一条", "u-robot")]


async def test_processor_keeps_owner() -> None:
    """处理机只是搬运工：它不碰所有者，原样交给 store，检索也一样转发。"""
    store = await memory_store()
    processor = DatabaseLogProcessor(store, buffer_size=5, flush_interval=0)
    await processor.write([record("登录成功", owner_id="u-admin"), record("框架启动")])

    assert [r.message for r in await processor.search(owner_id="u-admin")] == ["登录成功"]
    assert [r.message for r in await processor.search(owner_id="")] == ["框架启动"]


async def test_processor_search_accepts_loose_timestamps() -> None:
    """检索的时间参数是宽松输入（ISO 字符串也认），由 store 归一化。"""
    store = await memory_store()
    processor = DatabaseLogProcessor(store, buffer_size=5, flush_interval=0)
    await processor.write([record("早些时候", moment=1_600_000_000.0)])

    assert [r.message for r in await processor.search(start="2020-09-13T12:26:30")] == [
        "早些时候"
    ]
    assert await processor.search(start="2030-01-01T00:00:00") == []
