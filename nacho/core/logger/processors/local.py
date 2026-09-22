"""本地文件日志处理机。

以 JSON Lines 格式异步写文件（每条日志一行），文件 IO 通过
:func:`asyncio.to_thread` 放到线程池执行，避免阻塞事件循环。
支持按大小滚动切分。
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO, cast, override

from ..models import LogLevel, LogRecord, TimestampLike
from ..queue import OverflowPolicy
from .base import BaseLogProcessor


class LocalFileLogProcessor(BaseLogProcessor):
    """把日志落到本地文件，并支持从文件检索。"""

    name: str = "local"

    def __init__(
        self,
        path: "str | os.PathLike[str]",
        *,
        name: str | None = None,
        buffer_size: int = 200,
        flush_interval: float = 2.0,
        max_failures: int = 5,
        overflow_policy: "OverflowPolicy | str" = OverflowPolicy.DROP_OLDEST,
        max_bytes: int | None = 20 * 1024 * 1024,
        backup_count: int = 3,
        encoding: str = "utf-8",
    ) -> None:
        """
        :param name: 处理机名称，默认 ``"local"``；同时挂多个文件出口时
            （每个模块一个文件）必须各自命名，名称即唯一标识。
        """
        super().__init__(
            name=name,
            buffer_size=buffer_size,
            flush_interval=flush_interval,
            max_failures=max_failures,
            overflow_policy=overflow_policy,
        )
        self._path: Path = Path(path)
        self._max_bytes: int | None = max_bytes
        self._backup_count: int = backup_count
        self._encoding: str = encoding
        self._file: TextIO | None = None

    # ------------------------------------------------------------------ 生命周期
    @override
    async def _on_start(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = await asyncio.to_thread(
            open, self._path, "a", encoding=self._encoding
        )

    @override
    async def _on_stop(self) -> None:
        if self._file is not None:
            await asyncio.to_thread(self._file.close)
            self._file = None

    # ------------------------------------------------------------------ 写入
    @override
    async def write(self, records: list[LogRecord]) -> None:
        if not records:
            return
        lines = [
            json.dumps(record.to_dict(), ensure_ascii=False) + "\n" for record in records
        ]
        await asyncio.to_thread(self._write_lines_sync, lines)

    def _write_lines_sync(self, lines: list[str]) -> None:
        if self._file is None:  # 未启动时按需打开，保证不丢日志
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self._path, "a", encoding=self._encoding)

        self._file.writelines(lines)
        self._file.flush()
        os.fsync(self._file.fileno())
        self._rotate_if_needed()

    def _rotate_if_needed(self) -> None:
        if not self._max_bytes or self._max_bytes <= 0:
            return
        try:
            size = self._path.stat().st_size
        except OSError:  # pragma: no cover - 文件被外部删除等
            return
        if size < self._max_bytes:
            return

        assert self._file is not None
        self._file.close()
        for index in range(self._backup_count - 1, 0, -1):
            older = self._path.with_suffix(self._path.suffix + f".{index}")
            newer = self._path.with_suffix(self._path.suffix + f".{index + 1}")
            if older.exists():
                os.replace(older, newer)
        if self._backup_count > 0:
            os.replace(self._path, self._path.with_suffix(self._path.suffix + ".1"))
        self._file = open(self._path, "a", encoding=self._encoding)

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
    ) -> list[LogRecord]:
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
    ) -> list[LogRecord]:
        results: list[LogRecord] = []
        for file_path in self._log_files():
            if not file_path.exists():
                continue
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
                        results.append(record)

        # 文件为追加写入（旧 -> 新），检索结果按时间倒序返回
        results.sort(key=lambda item: item.timestamp, reverse=True)
        return results[offset : offset + limit]

    def _log_files(self) -> list[Path]:
        """返回当前文件与所有备份文件，供检索使用。"""
        files = [self._path]
        for index in range(1, self._backup_count + 1):
            files.append(self._path.with_suffix(self._path.suffix + f".{index}"))
        return files

    # ------------------------------------------------------------------ 状态
    @property
    @override
    def stats(self) -> dict[str, object]:
        data: dict[str, object] = super().stats
        data["path"] = str(self._path)
        return data
