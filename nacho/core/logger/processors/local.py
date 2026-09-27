"""本地文件日志处理机：**一个目录、按天分片**，片内写满一个时间跨度再换片。

片名 = ``<前缀>-<YYYY-MM-DD>[.<序号>].log``::

    logs/nacho-2026-09-28.log       当天第一片
    logs/nacho-2026-09-28.1.log     同一天写满一个时间跨度（或顶到大小上限）后的第二片
    logs/nacho-2026-09-29.log       第二天：新日期片，序号从 0 重来

为什么按天分片：日志是**一条时间线**，人翻的时候按时间找（「今天 / 昨天」），按天分片天然
对上这种问法；模块的区分靠记录里的 ``logger_name`` 字段，不靠文件 —— 所以「一个模块一个
文件」那套在这里没有必要，装配层只挂一份。

检索把该目录下同前缀的片当一份数据读（:meth:`_iter_matching_sync`）；给了 ``start`` / ``end``
时按**片名的日期**先裁掉不相干的天 —— 片名带日期是个便宜又好用的索引，省得为了「昨天的日志」
把半个月的片全扫一遍；没给 ``start`` 时按 ``search_days`` 兜一个下限（默认最近两天），因为
翻一页、自动刷新一次都要查一遍，没有边界就等于每次都把保留期内的片全读一遍。

几处实现取舍：

* **片是滚动的，句柄只有一个**：量级再大也只有一份打开的文件，每批写完 ``flush`` + ``fsync``；
* **换片在写之前判**：跨天 / 这一片已写满一个时间跨度 / 顶到大小上限，三者任一就换新片，
  于是「一片写多久」由时间说了算，不靠事后补救；
* **重启接着写**：启动时认领「最新的那片」（按日期 + 序号排），当天的就继续追加，跨天才开
  新日期片 —— 免得频繁重启攒出一堆只有几行的碎片；接着写时时间窗口从**那片最后一次写**
  （文件 mtime）算起，不然重启一次就白换一片；
* **清理按天**：``keep_days`` 到期的片在换片时删掉（``0`` = 不清理），按片名里的日期算；
  只删自己前缀的片，别人放这个目录里的东西不碰。

文件 IO 一律走 :func:`asyncio.to_thread`，不占事件循环。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TextIO, cast, override

from ..models import LogLevel, LogRecord, LogSearchResult, TimestampLike, normalize_timestamp
from ..queue import OverflowPolicy
from .base import BaseLogProcessor, ProcessorStats

#: 片名去掉前缀之后剩下的部分：``<YYYY-MM-DD>[.<序号>].log``
_SHARD_TAIL_RE: re.Pattern[str] = re.compile(
    r"^(?P<day>\d{4}-\d{2}-\d{2})(?:\.(?P<seq>\d+))?\.log$"
)


def _day_of(value: TimestampLike) -> date | None:
    """把宽松时间输入归一成「哪一天」（本地时区，与片名里的日期同一口径）。"""
    stamp = normalize_timestamp(value)
    return None if stamp is None else datetime.fromtimestamp(stamp).date()


class LocalProcessorStats(ProcessorStats):
    """:attr:`LocalFileLogProcessor.stats` 的形状：基类的那些字段 + 目录与当前片。"""

    directory: str
    #: 当前正在写的片；还没开始写就是空串
    path: str


class LocalFileLogProcessor(BaseLogProcessor):
    """把日志按天分片落到本地目录，并支持跨片检索。"""

    name: str = "local"

    def __init__(
        self,
        directory: "str | os.PathLike[str]",
        *,
        prefix: str = "nacho",
        rotate_minutes: float = 60.0,
        max_bytes: int | None = 20 * 1024 * 1024,
        keep_days: int = 14,
        search_days: int = 2,
        name: str | None = None,
        buffer_size: int = 200,
        flush_interval: float = 2.0,
        max_failures: int = 5,
        overflow_policy: "OverflowPolicy | str" = OverflowPolicy.DROP_OLDEST,
        encoding: str = "utf-8",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """
        :param directory: 放片的目录（配置里的 ``[logging.file] dir``）；
        :param prefix: 片名前缀（配置留空就用 ``[app] name``，于是片名与进程名一致）；
        :param rotate_minutes: 一片最多写多久（分钟）；``0`` = 只按天分片；
        :param max_bytes: 单片大小兜底（字节）：到了也换片；``None`` / ``0`` = 不限；
        :param keep_days: 保留几天（按片名里的日期算），到期的片在换片时删掉；``0`` = 不清理；
        :param search_days: 检索没给 ``start`` 时往回找几天（``0`` = 不限，整目录当一份数据读）；
        :param name: 处理机名称，默认 ``"local"``；挂多份（如再落一个备份目录）时各自命名，
            名称即唯一标识；
        :param now: 取当前时刻的函数，默认 :func:`datetime.now`；测试可以喂假时钟来验换片。
        """
        super().__init__(
            name=name,
            buffer_size=buffer_size,
            flush_interval=flush_interval,
            max_failures=max_failures,
            overflow_policy=overflow_policy,
        )
        self._dir: Path = Path(directory)
        self._prefix: str = prefix
        self._rotate_minutes: float = rotate_minutes
        self._max_bytes: int | None = max_bytes
        self._keep_days: int = keep_days
        self._search_days: int = search_days
        self._encoding: str = encoding
        self._now: Callable[[], datetime] = datetime.now if now is None else now
        #: 当前正在写的片；没认领过（没启动、也没写过）就是 ``None``
        self._path: Path | None = None
        #: 当前片在同一天里的序号（第一片是 0）与它是**什么时候开的**
        self._seq: int = 0
        self._opened_at: datetime | None = None
        self._file: TextIO | None = None

    # ------------------------------------------------------------------ 只读视图
    @property
    def directory(self) -> Path:
        """放片的目录。"""
        return self._dir

    @property
    def prefix(self) -> str:
        """片名前缀（``<前缀>-<日期>.log``）。"""
        return self._prefix

    @property
    def current_shard(self) -> Path | None:
        """当前正在写的片；还没写就是 ``None``（检索 / 测试要看写到哪儿时用）。"""
        return self._path

    def shards(self) -> list[Path]:
        """目录里属于本出口的片，按（日期，序号）**从新到旧**。"""
        return [path for _day, _seq, path in self._list_shards()]

    # ------------------------------------------------------------------ 生命周期
    @override
    async def _on_start(self) -> None:
        await asyncio.to_thread(self._claim_on_start)

    @override
    async def _on_stop(self) -> None:
        if self._file is not None:
            await asyncio.to_thread(self._file.close)
            self._file = None

    def _claim_on_start(self) -> None:
        """启动时认领「接着写哪一片」并打开句柄（线程里跑）。

        今天是哪一片就接着写哪一片，跨天才开新日期片 —— 频繁重启不会攒出一堆碎片。
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        moment: datetime = self._now()
        latest = self._list_shards()[0] if self._list_shards() else None
        if latest is not None and latest[0] == moment.date():
            _day, seq, path = latest
            self._seq = seq
            # 窗口从它**最后一次写**算起（文件 mtime）：重启后不至于因为「开片时刻」丢了就白换一片。
            # mtime 不在今天（时钟回拨 / 文件被外面拷进来）时按「刚开」算，免得一启动就换片。
            opened_at: datetime = moment
            with suppress(OSError):
                written_at = datetime.fromtimestamp(path.stat().st_mtime)
                opened_at = written_at if written_at.date() == moment.date() else moment
            self._opened_at = opened_at
        else:
            self._seq, path = self._free_shard(moment, 0)
            self._opened_at = moment
        self._path = path
        self._file = self._open(path)
        self._prune(moment)

    def _open(self, path: Path) -> TextIO:
        """打开一片（追加）：目录不存在就建，片文件不存在就创建。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        return open(path, "a", encoding=self._encoding)

    # ------------------------------------------------------------------ 写入
    @override
    async def write(self, records: list[LogRecord]) -> None:
        if not records:
            return
        lines = [json.dumps(record.to_dict(), ensure_ascii=False) + "\n" for record in records]
        await asyncio.to_thread(self._write_lines_sync, lines)

    def _write_lines_sync(self, lines: list[str]) -> None:
        moment: datetime = self._now()
        self._rotate_for(moment)  # 写之前把「这一批该进哪一片」定下来
        if self._file is None:  # 未启动时按需打开，保证不丢日志
            self._dir.mkdir(parents=True, exist_ok=True)
            assert self._path is not None
            self._file = self._open(self._path)

        self._file.writelines(lines)
        self._file.flush()
        os.fsync(self._file.fileno())

    def _rotate_for(self, moment: datetime) -> None:
        """写之前定片：还没认领过就认领，认领过但过期了就换下一片。"""
        if self._path is None or self._opened_at is None:
            self._claim_on_start()
            return
        if self._expired(moment):
            self._roll(moment)

    def _expired(self, moment: datetime) -> bool:
        """这一片还该不该继续写：跨天 / 超过 ``rotate_minutes`` / 顶到 ``max_bytes`` 都算过期。"""
        opened: datetime | None = self._opened_at
        if opened is None or opened.date() != moment.date():
            return True
        if self._rotate_minutes > 0 and moment - opened >= timedelta(
            minutes=self._rotate_minutes
        ):
            return True
        if self._max_bytes and self._max_bytes > 0 and self._path is not None:
            with suppress(OSError):  # 片被外面删了之类：当作没顶到上限，下一批重新打开
                if self._path.stat().st_size >= self._max_bytes:
                    return True
        return False

    def _roll(self, moment: datetime) -> None:
        """换到下一片：同一天序号 +1；跨天换成日期片（序号归 0）。"""
        if self._file is not None:
            self._file.close()
            self._file = None
        opened: datetime | None = self._opened_at
        same_day: bool = opened is not None and opened.date() == moment.date()
        self._seq, path = self._free_shard(moment, self._seq + 1 if same_day else 0)
        self._path = path
        self._opened_at = moment
        self._file = self._open(path)
        self._prune(moment)

    # ------------------------------------------------------------------ 片名与清理
    def _shard_path(self, day: date, seq: int) -> Path:
        """片名：``<前缀>-<日期>[.<序号>].log``（序号 0 不写，就是当天的第一片）。"""
        stamp: str = day.isoformat()
        tail: str = "" if seq == 0 else f".{seq}"
        return self._dir / f"{self._prefix}-{stamp}{tail}.log"

    def _free_shard(self, moment: datetime, seq: int) -> tuple[int, Path]:
        """挑一个**还没被占用**的片名：同一天的既有片往后让位，绝不覆盖已有数据。"""
        path: Path = self._shard_path(moment.date(), seq)
        while path.exists():
            seq += 1
            path = self._shard_path(moment.date(), seq)
        return seq, path

    def _list_shards(self) -> list[tuple[date, int, Path]]:
        """目录里属于本出口的片，按（日期，序号）从新到旧。"""
        found: list[tuple[date, int, Path]] = []
        if not self._dir.is_dir():
            return found
        head: str = f"{self._prefix}-"
        for path in self._dir.iterdir():
            if not path.is_file() or not path.name.startswith(head):
                continue
            match = _SHARD_TAIL_RE.match(path.name[len(head) :])
            if match is None:
                continue
            try:
                day: date = date.fromisoformat(match["day"])
            except ValueError:  # pragma: no cover - 日期形状已由正则卡住
                continue
            found.append((day, int(match["seq"] or 0), path))
        found.sort(reverse=True)  # 新的在前：检索里并列的两条也先看到新的
        return found

    def _prune(self, moment: datetime) -> None:
        """把超过保留期的片删掉（当前片永远不动；只删自己前缀的片）。"""
        if self._keep_days <= 0:
            return
        deadline: date = moment.date() - timedelta(days=self._keep_days)
        for day, _seq, path in self._list_shards():
            if path == self._path or day >= deadline:
                continue
            with suppress(OSError):
                path.unlink()

    # ------------------------------------------------------------------ 检索
    @override
    async def search(
        self,
        *,
        query: str | None = None,
        level: "LogLevel | str | None" = None,
        start: TimestampLike = None,
        end: TimestampLike = None,
        logger_name: str | None = None,
        owner_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> LogSearchResult:
        return await asyncio.to_thread(
            self._search_sync,
            query,
            level,
            start,
            end,
            logger_name,
            owner_id,
            limit,
            offset,
        )

    def _shards_within(self, start: TimestampLike, end: TimestampLike) -> list[Path]:
        """这一趟要读哪些片：按**片名的日期**先裁掉不相干的天。

        给了 ``start`` 就以它为准；没给就按 ``search_days`` 兜一个下限（``0`` = 不限，整目录
        当一份数据读）—— 日志页翻一页、自动刷新一次都要查一遍，没有边界的话每次都把保留期
        （``keep_days``）内的片全读一遍。
        """
        first: date | None = _day_of(start)
        if first is None and self._search_days > 0:
            first = self._now().date() - timedelta(days=self._search_days)
        last: date | None = _day_of(end)
        picked: list[Path] = []
        for day, _seq, path in self._list_shards():  # 新的在前
            if first is not None and day < first:
                continue
            if last is not None and day > last:
                continue
            picked.append(path)
        return picked

    def _iter_matching_sync(
        self,
        query: str | None,
        level: "LogLevel | str | None",
        start: TimestampLike,
        end: TimestampLike,
        logger_name: str | None,
        owner_id: str | None,
    ) -> Iterator[LogRecord]:
        """逐行扫描要读的那些片，产出满足条件的记录（检索就靠这一处解析）。"""
        for file_path in self._shards_within(start, end):
            with open(file_path, "r", encoding=self._encoding) as handle:
                for line in handle:
                    line: str = line.strip()
                    if not line:
                        continue
                    try:
                        # json.loads 返回 Any，先显式收敛为 object，避免 Any 参与后续类型检查
                        payload: object = cast(object, json.loads(line))
                        # 顶层必须是对象，非对象结构（数组/标量）无法还原为日志，直接跳过
                        if not isinstance(payload, Mapping):
                            continue
                        record = LogRecord.from_dict(
                            cast(Mapping[str, object], payload)
                        )
                    except (ValueError, TypeError):
                        continue  # 跳过损坏行，不影响整体检索
                    if record.matches(
                        query=query,
                        level=level,
                        start=start,
                        end=end,
                        logger_name=logger_name,
                        owner_id=owner_id,
                    ):
                        yield record

    def _search_sync(
        self,
        query: str | None,
        level: "LogLevel | str | None",
        start: TimestampLike,
        end: TimestampLike,
        logger_name: str | None,
        owner_id: str | None,
        limit: int,
        offset: int,
    ) -> LogSearchResult:
        matched = list(
            self._iter_matching_sync(query, level, start, end, logger_name, owner_id)
        )
        # 片是追加写的（旧 -> 新），检索结果按时间倒序返回。这份没有自增序号可依
        # （``seq`` 是落库那份的东西，文件里恒为 0），同一时刻的几条只能按时间戳并列。
        matched.sort(key=lambda item: (item.timestamp, item.seq), reverse=True)
        # 命中的总数就是 len(matched)：一次扫描既给这一页，也给总数（不必再扫一遍去数）
        return LogSearchResult(records=matched[offset : offset + limit], total=len(matched))

    # ------------------------------------------------------------------ 状态
    @property
    @override
    def stats(self) -> LocalProcessorStats:
        return {
            **super().stats,
            "directory": str(self._dir),
            "path": "" if self._path is None else str(self._path),
        }
