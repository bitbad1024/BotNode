"""``nacho/core/logger/processors/local.py`` 单元测试：按天分片、按时间/大小换片、跨片检索。

换片全看时间与片名，所以时间相关的用例靠**注入假时钟**（``now=``）——把「现在」握在手里，
不必真的等到跨天。盘上已有的片则直接手写 JSON 行造出来（每行一条日志，见 ``write_lines``）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nacho.core.logger.models import LogRecord
from nacho.core.logger.processors.local import LocalFileLogProcessor

#: 造记录时间戳时统一用**带时区**的时刻：不写 tzinfo 的话，``normalize_timestamp`` 当它是 UTC、
#: 而 ``datetime.timestamp()`` 当它是本地时间 —— 两边一混，过滤条件就会差出几个时区。
UTC = timezone.utc


class FakeClock:
    """假时钟：默认 2026-09-28 12:00，用例自己往前拨。"""

    def __init__(self, moment: datetime | None = None) -> None:
        self.moment: datetime = moment or datetime(2026, 9, 28, 12, 0, 0)

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **delta: float) -> None:
        self.moment += timedelta(**delta)


def make_processor(
    directory: Path,
    clock: FakeClock,
    *,
    rotate_minutes: float = 60.0,
    max_bytes: int | None = 20 * 1024 * 1024,
    keep_days: int = 14,
) -> LocalFileLogProcessor:
    """一份文件出口：``buffer_size=1`` 逐条直写，时间走假时钟。"""
    return LocalFileLogProcessor(
        directory,
        prefix="nacho",
        rotate_minutes=rotate_minutes,
        max_bytes=max_bytes,
        keep_days=keep_days,
        buffer_size=1,
        flush_interval=0,
        now=clock,
    )


def log(message: str, moment: datetime | None = None) -> LogRecord:
    """造一条日志；``moment`` 给的是它的时间戳（检索过滤用得上）。"""
    if moment is None:
        return LogRecord(message=message)
    return LogRecord(message=message, timestamp=moment.timestamp())


def write_lines(path: Path, *records: LogRecord) -> None:
    """手写几行日志（每行一个 JSON）—— 直接造「盘上已经有这些片」的场面。"""
    path.write_text(
        "".join(json.dumps(record.to_dict(), ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def touch_at(path: Path, moment: datetime) -> None:
    """把片的 mtime 拨到假时钟那一刻。

    真实 mtime 和假时钟可能落在**同一天**（跑测试时墙上时钟正好也是那天），而
    ``_claim_on_start`` 在 mtime 与「现在」同一天时会拿它当「最后一次写」—— 于是「重启接着写」
    这类用例会被真实时钟带偏：真实 00:08 与假时钟 12:00 差出十几个小时，一启动就换片。
    拨成假时钟，用例才只跟注入的时间有关。
    """
    stamp = moment.timestamp()
    os.utime(path, (stamp, stamp))


async def test_shard_name_is_prefix_and_day(tmp_path: Path) -> None:
    """片名 = ``<前缀>-<日期>.log``：当天第一片不带序号，落点就是它。"""
    clock = FakeClock()
    processor = make_processor(tmp_path, clock)
    await processor.start()
    try:
        await processor.write([log("第一条")])
        assert processor.current_shard == tmp_path / "nacho-2026-09-28.log"
        assert [path.name for path in processor.shards()] == ["nacho-2026-09-28.log"]
        assert "第一条" in read(tmp_path / "nacho-2026-09-28.log")
        # 状态里能看出「落哪个目录、正在写哪一片」
        assert processor.stats["directory"] == str(tmp_path)
        assert processor.stats["path"] == str(tmp_path / "nacho-2026-09-28.log")
    finally:
        await processor.stop()


async def test_rolls_when_the_span_elapses(tmp_path: Path) -> None:
    """片写满一个时间跨度就加序号换下一片（默认一小时）。"""
    clock = FakeClock()
    processor = make_processor(tmp_path, clock, rotate_minutes=60)
    await processor.start()
    try:
        await processor.write([log("第一片")])
        clock.advance(minutes=59)
        await processor.write([log("还进第一片")])
        clock.advance(minutes=1)  # 正好满 60 分钟：下一次写换片
        await processor.write([log("第二片")])
    finally:
        await processor.stop()

    first = tmp_path / "nacho-2026-09-28.log"
    second = tmp_path / "nacho-2026-09-28.1.log"
    assert "第一片" in read(first) and "还进第一片" in read(first)
    assert "第二片" not in read(first)
    assert "第二片" in read(second)
    assert processor.shards() == [second, first]  # 新的在前


async def test_rolls_when_the_shard_hits_max_bytes(tmp_path: Path) -> None:
    """时间没到、但片顶到大小上限也换片 —— 爆量不至于把一片写成好几个 G。"""
    clock = FakeClock()
    processor = make_processor(tmp_path, clock, rotate_minutes=0, max_bytes=1)
    await processor.start()
    try:
        await processor.write([log("一")])
        await processor.write([log("二")])  # 上一片已经 >= 1 字节 -> 换到 .1
    finally:
        await processor.stop()

    assert [path.name for path in processor.shards()] == [
        "nacho-2026-09-28.1.log",
        "nacho-2026-09-28.log",
    ]


async def test_new_day_opens_a_new_date_shard(tmp_path: Path) -> None:
    """跨天换日期片，序号归零（新的一天又是「第一片」）。"""
    clock = FakeClock()
    processor = make_processor(tmp_path, clock, rotate_minutes=60)
    await processor.start()
    try:
        await processor.write([log("二十八号第一片")])
        clock.advance(minutes=60)
        await processor.write([log("二十八号第二片")])  # 同一天写满一个跨度 -> .1
        clock.moment = datetime(2026, 9, 29, 0, 5)
        await processor.write([log("二十九号")])  # 跨天：新日期片，序号从 0 重来
    finally:
        await processor.stop()

    assert [path.name for path in processor.shards()] == [
        "nacho-2026-09-29.log",
        "nacho-2026-09-28.1.log",
        "nacho-2026-09-28.log",
    ]
    assert "二十九号" in read(tmp_path / "nacho-2026-09-29.log")


async def test_restart_continues_the_same_shard(tmp_path: Path) -> None:
    """重启接着写**同一片**：频繁重启不该攒出一堆只有几行的碎片。"""
    clock = FakeClock()
    first = make_processor(tmp_path, clock)
    await first.start()
    try:
        await first.write([log("重启前")])
    finally:
        await first.stop()
    shard = first.current_shard
    assert shard is not None
    touch_at(shard, clock.moment)  # 片最后一次写 = 假时钟那一刻，重启不该因为真实时钟而换片

    second = make_processor(tmp_path, clock)
    await second.start()
    try:
        assert second.current_shard == shard  # 认领的是同一片
        await second.write([log("重启后")])
    finally:
        await second.stop()

    assert second.shards() == [shard]  # 没有多出一片
    text = read(shard)
    assert "重启前" in text and "重启后" in text


async def test_keeps_only_the_last_days(tmp_path: Path) -> None:
    """``keep_days`` 到期的片在换片时删掉（按片名里的日期算）；别人的文件不碰。"""
    clock = FakeClock(datetime(2026, 9, 28, 12, 0))
    for day in ("2026-09-01", "2026-09-20", "2026-09-27"):
        write_lines(tmp_path / f"nacho-{day}.log", log(f"{day} 的旧日志"))
    outsider = tmp_path / "别人的.log"
    write_lines(outsider, log("不是这个出口的片"))

    processor = make_processor(tmp_path, clock, keep_days=7)
    await processor.start()  # 启动就认领 + 清理一轮
    try:
        await processor.write([log("今天的")])
    finally:
        await processor.stop()

    names = sorted(path.name for path in tmp_path.iterdir())
    assert "nacho-2026-09-01.log" not in names  # 27 天前：删了
    assert "nacho-2026-09-20.log" not in names  # 8 天前：也在保留期外
    assert "nacho-2026-09-27.log" in names  # 昨天：留着
    assert "nacho-2026-09-28.log" in names  # 今天这片
    assert outsider.name in names  # 不是我的前缀：不碰


async def test_search_reads_all_shards_and_skips_other_days(tmp_path: Path) -> None:
    """检索把该前缀的片当一份数据读；给了 ``start`` 就按**片名的日期**先裁掉不相干的天。"""
    write_lines(
        tmp_path / "nacho-2026-09-27.log",
        log("昨天那条", datetime(2026, 9, 27, 23, 0, tzinfo=UTC)),
    )
    write_lines(
        tmp_path / "nacho-2026-09-28.log",
        log("第一条", datetime(2026, 9, 28, 1, 0, tzinfo=UTC)),
        log("第二条", datetime(2026, 9, 28, 2, 0, tzinfo=UTC)),
    )
    # 前缀不同的片不是这个出口的：别人写的日志不该被读进来
    write_lines(
        tmp_path / "other-2026-09-28.log",
        log("别人那条", datetime(2026, 9, 28, 3, 0, tzinfo=UTC)),
    )

    processor = make_processor(tmp_path, FakeClock(), rotate_minutes=0)

    found = await processor.search()
    assert [record.message for record in found.records] == ["第二条", "第一条", "昨天那条"]
    assert found.total == 3

    # 分页按倒序切：本页 1 条，总数仍是 3
    page = await processor.search(limit=1, offset=1)
    assert [record.message for record in page.records] == ["第一条"]
    assert page.total == 3

    # 只查今天：27 号那片按片名的日期就被裁掉了，压根不读
    today = await processor.search(start=datetime(2026, 9, 28, 0, 0, tzinfo=UTC))
    assert [record.message for record in today.records] == ["第二条", "第一条"]
    assert today.total == 2

    # 只查昨天：今天那片同理被裁掉
    yesterday = await processor.search(
        start=datetime(2026, 9, 27, 0, 0, tzinfo=UTC),
        end=datetime(2026, 9, 27, 23, 30, tzinfo=UTC),
    )
    assert [record.message for record in yesterday.records] == ["昨天那条"]


async def test_broken_lines_are_skipped(tmp_path: Path) -> None:
    """坏行（不是 JSON / 顶层不是对象 / 空行）跳过：一条坏数据不该毁掉整次检索。"""
    write_lines(tmp_path / "nacho-2026-09-28.log", log("好的一条", datetime(2026, 9, 28, 1, 0)))
    good = read(tmp_path / "nacho-2026-09-28.log")
    (tmp_path / "nacho-2026-09-28.log").write_text(
        "不是 JSON\n[1, 2, 3]\n\n" + good, encoding="utf-8"
    )

    processor = make_processor(tmp_path, FakeClock(), rotate_minutes=0)
    found = await processor.search()
    assert [record.message for record in found.records] == ["好的一条"]
