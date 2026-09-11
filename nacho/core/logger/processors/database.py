"""数据库日志处理机。

通过注入数据库层提供的 :class:`~nacho.core.logger.interfaces.DatabaseAdapter`
把日志批量落库，并支持从数据库检索。日志层只依赖适配器协议，
不关心底层是 sqlite / mysql / postgres。
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import cast, override

from ..interfaces import DatabaseAdapter
from ..models import LogLevel, LogRecord, TimestampLike, normalize_timestamp
from ..queue import OverflowPolicy
from .base import BaseLogProcessor

DEFAULT_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS {table} (
    record_id   VARCHAR(64)  PRIMARY KEY,
    timestamp   DOUBLE PRECISION NOT NULL,
    level       VARCHAR(16)  NOT NULL,
    logger_name VARCHAR(128) NOT NULL,
    message     TEXT         NOT NULL,
    extra       TEXT,
    exc_text    TEXT
)
"""

DEFAULT_INSERT_SQL = (
    "INSERT INTO {table} "
    "(record_id, timestamp, level, logger_name, message, extra, exc_text) "
    "VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p})"
)


class DatabaseLogProcessor(BaseLogProcessor):
    """把日志批量写入数据库，并支持 SQL 条件检索。"""

    name: str = "database"

    def __init__(
        self,
        adapter: DatabaseAdapter,
        *,
        name: str | None = None,
        table: str = "logs",
        paramstyle: str = "qmark",
        buffer_size: int = 500,
        flush_interval: float = 5.0,
        max_failures: int = 5,
        overflow_policy: "OverflowPolicy | str" = OverflowPolicy.DROP_OLDEST,
        ensure_schema: bool = True,
        create_table_sql: str | None = None,
        insert_sql: str | None = None,
        columns: "Mapping[str, str] | None" = None,
    ) -> None:
        """
        :param name: 处理机名称，默认 ``"database"``；同时挂多个库出口时
            必须各自命名，名称即唯一标识。
        """
        super().__init__(
            name=name,
            buffer_size=buffer_size,
            flush_interval=flush_interval,
            max_failures=max_failures,
            overflow_policy=overflow_policy,
        )
        if paramstyle not in ("qmark", "format", "numeric"):
            raise ValueError(f"不支持的 paramstyle: {paramstyle!r}")

        self._adapter: DatabaseAdapter = adapter
        self._table: str = table
        self._paramstyle: str = paramstyle
        self._ensure_schema: bool = ensure_schema
        self._create_table_sql: str = (create_table_sql or DEFAULT_CREATE_TABLE_SQL).format(
            table=table
        )
        self._insert_sql: str = (insert_sql or DEFAULT_INSERT_SQL).format(
            table=table, p=self._placeholder(1)
        )
        #: 数据库列名 -> 记录字段名，便于适配已有表结构
        self._columns: dict[str, str] = dict(
            columns
            or {
                "record_id": "record_id",
                "timestamp": "timestamp",
                "level": "level",
                "logger_name": "logger_name",
                "message": "message",
                "extra": "extra",
                "exc_text": "exc_text",
            }
        )

    def _placeholder(self, index: int) -> str:
        if self._paramstyle == "qmark":
            return "?"
        if self._paramstyle == "format":
            return "%s"
        return f":{index}"

    # ------------------------------------------------------------------ 生命周期
    @override
    async def _on_start(self) -> None:
        if self._ensure_schema:
            await self._adapter.execute(self._create_table_sql)  # pyright: ignore[reportUnusedCallResult]

    # ------------------------------------------------------------------ 写入
    @override
    async def write(self, records: list[LogRecord]) -> None:
        if not records:
            return
        rows = [self._to_row(record) for record in records]

        # 批量写入是适配器的基本能力，直接批量落库
        await self._adapter.execute_many(self._insert_sql, rows)  # pyright: ignore[reportUnusedCallResult]

    def _to_row(self, record: LogRecord) -> list[object]:
        mapping = {
            "record_id": record.record_id,
            "timestamp": record.timestamp,
            "level": record.level.name,
            "logger_name": record.logger_name,
            "message": record.message,
            "extra": json.dumps(record.extra, ensure_ascii=False),
            "exc_text": record.exc_text,
        }
        return [mapping[field] for field in self._columns.values()]

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
        limit: int = 100,
        offset: int = 0,
    ) -> list[LogRecord]:
        conditions: list[str] = []
        params: list[object] = []
        placeholders = self._placeholder

        def add(column: str, value: object) -> None:
            conditions.append(f"{column} = {placeholders(len(params) + 1)}")
            params.append(value)

        if level is not None:
            add(self._columns["level"], LogLevel.parse(level).name)
        if logger_name is not None:
            add(self._columns["logger_name"], logger_name)

        start_ts = normalize_timestamp(start)
        if start_ts is not None:
            conditions.append(
                f"{self._columns['timestamp']} >= {placeholders(len(params) + 1)}"
            )
            params.append(start_ts)

        end_ts = normalize_timestamp(end)
        if end_ts is not None:
            conditions.append(
                f"{self._columns['timestamp']} <= {placeholders(len(params) + 1)}"
            )
            params.append(end_ts)

        if query:
            conditions.append(
                f"{self._columns['message']} LIKE {placeholders(len(params) + 1)}"
            )
            params.append(f"%{query}%")

        sql = f"SELECT * FROM {self._table}"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += f" ORDER BY {self._columns['timestamp']} DESC LIMIT {placeholders(len(params) + 1)} OFFSET {placeholders(len(params) + 2)}"
        params.append(int(limit))
        params.append(int(offset))

        rows = await self._adapter.fetch_all(sql, params)
        return [self._from_row(row) for row in rows]

    def _from_row(self, row: Mapping[str, object]) -> LogRecord:
        """把一行数据库记录还原成 :class:`LogRecord`。

        列名以 ``_columns`` 映射为准，映射里没有该字段时回退到同名列。适配器返回的
        是无类型的 ``object``，这里按 :meth:`LogRecord.from_dict` 的同一套约定收窄：
        能校验的交给 :func:`isinstance`，跨不过去的边界值用 :func:`typing.cast` 明确
        表态，缺值一律退回默认值——``object`` 是顶层类型，不表态就必然类型报错。
        """
        def value(field: str) -> object:
            column = self._columns.get(field, field)
            return row[column] if column in row else row.get(field)

        def as_extra(source: Mapping[str, object]) -> dict[str, object]:
            """把 mapping 归一成 ``dict[str, object]``（键统一字符串化）。"""
            return {str(key): item for key, item in source.items()}

        raw_extra = value("extra")
        raw_exc_text = value("exc_text")

        # 显式标注为 dict[str, object]：dict 不变型，直接把 dict[str, str] 塞给
        # LogRecord.extra 会被类型检查拒绝
        extra: dict[str, object] = {}
        if isinstance(raw_extra, str):
            try:
                # json.loads 在 typeshed 中返回 Any，这里 cast 成 object 表态：
                # 解出来的形状未知，下面用 isinstance 现场校验
                decoded = cast(object, json.loads(raw_extra))
            except ValueError:
                extra = {"raw": raw_extra}
            else:
                # 合法 JSON 但顶层不是对象（数组 / 标量）时同样退回原文，避免静默丢数据
                if isinstance(decoded, Mapping):
                    extra = as_extra(cast(Mapping[str, object], decoded))
                else:
                    extra = {"raw": raw_extra}
        elif isinstance(raw_extra, Mapping):
            extra = as_extra(cast(Mapping[str, object], raw_extra))

        return LogRecord(
            message=cast(str, value("message") or ""),
            level=LogLevel.parse(value=cast(LogLevel | str, value("level") or LogLevel.INFO)),
            logger_name=cast(str, value("logger_name") or "nacho"),
            timestamp=float(cast(float | str | int, value("timestamp") or 0.0)),
            record_id=cast(str, value("record_id") or uuid.uuid4().hex),
            extra=extra,
            exc_text=raw_exc_text if isinstance(raw_exc_text, str) else None,
        )

    # ------------------------------------------------------------------ 状态
    @property
    @override
    def stats(self) -> dict[str, object]:
        data = super().stats
        data["table"] = self._table
        return data
