"""数据库层：日志系统 :class:`~nacho.core.logger.interfaces.DatabaseAdapter` 协议的落地实现。

日志层只认协议、不关心底层是什么库；这一层负责把真正的连接收进来：

* :class:`SqliteAdapter`：标准库 sqlite3，单文件零部署，``qmark`` 占位；
* :class:`MariadbAdapter`：MariaDB / MySQL，PyMySQL 驱动（可选依赖，
  ``pip install "nacho[mariadb]"``），``format`` 占位。

两者都是「单连接 + 一把锁 + 线程池」：语句全部丢 :func:`asyncio.to_thread`
执行，不阻塞事件循环。日志写入是批量低频的，单连接足够；要抗高并发
再换连接池实现，接口不变。

表名来自配置文件、要拼进 SQL，两个适配器在构造时都会用
:func:`require_table_name` 校验成合法标识符，不做字符串拼接转义。
"""
from __future__ import annotations

import asyncio
import re
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, cast

from nacho.core.logger.models import TimestampLike, normalize_timestamp

if TYPE_CHECKING:  # 驱动是可选依赖，只在类型检查时导入
    import pymysql

#: 合法表名（标识符）；表名要拼进 SQL，来自配置文件，不能直接拼
_TABLE_NAME: re.Pattern[str] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def require_table_name(table: str) -> str:
    """校验表名是合法标识符，返回原值；不是就抛 :class:`ValueError`。"""
    if not _TABLE_NAME.match(table):
        raise ValueError(f"表名要合法标识符，收到 {table!r}")
    return table


def _check_cutoff(before: TimestampLike) -> float:
    """delete_before 的公共前置：归一化成 Unix 时间戳，``None`` 属非法输入。"""
    cutoff = normalize_timestamp(before)
    if cutoff is None:
        raise ValueError("delete_before 需要一个明确的时刻，不能是 None")
    return cutoff


class SqliteAdapter:
    """sqlite3 实现的 :class:`~nacho.core.logger.interfaces.DatabaseAdapter`。"""

    def __init__(self, path: Path, *, table: str) -> None:
        require_table_name(table)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock: asyncio.Lock = asyncio.Lock()
        self._table: str = table

    async def execute(self, sql: str, params: Sequence[object] | None = None) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._execute, sql, params)

    async def execute_many(self, sql: str, rows: Sequence[Sequence[object]]) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._execute_many, sql, rows)

    async def fetch_all(
        self, sql: str, params: Sequence[object] | None = None
    ) -> list[Mapping[str, object]]:
        async with self._lock:
            return await asyncio.to_thread(self._fetch_all, sql, params)

    async def delete_before(self, before: TimestampLike) -> int:
        return await self.execute(
            f"DELETE FROM {self._table} WHERE timestamp < ?", (_check_cutoff(before),)
        )

    def close(self) -> None:
        """关连接（余量落库之后由调用方调）。"""
        self._conn.close()

    # -- 同步实现（只在线程池里跑，锁已保证单连接不并发）--
    def _execute(self, sql: str, params: Sequence[object] | None) -> int:
        cursor = self._conn.execute(sql, tuple(params or ()))
        self._conn.commit()
        return cursor.rowcount

    def _execute_many(self, sql: str, rows: Sequence[Sequence[object]]) -> int:
        cursor = self._conn.executemany(sql, [tuple(row) for row in rows])
        self._conn.commit()
        return cursor.rowcount

    def _fetch_all(
        self, sql: str, params: Sequence[object] | None
    ) -> list[Mapping[str, object]]:
        cursor = self._conn.execute(sql, tuple(params or ()))
        # row_factory 之后 fetchall 的元素类型收不回来，cast 成 Row 再转 dict
        rows = cast("list[sqlite3.Row]", cursor.fetchall())
        return [dict(row) for row in rows]


class MariadbAdapter:
    """MariaDB / MySQL 实现（PyMySQL 驱动，纯 Python、零编译依赖）。

    驱动是可选依赖：没装会在构造时报错并给出安装提示。每次执行前
    ``ping(reconnect=True)``，连接被服务端掐断后自动重连。
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        table: str,
    ) -> None:
        require_table_name(table)
        try:
            import pymysql
        except ImportError as exc:
            hint = "MariadbAdapter 需要 PyMySQL：pip install PyMySQL（或 pip install 'nacho[mariadb]'）"
            raise RuntimeError(hint) from exc
        try:
            self._conn: pymysql.connections.Connection = pymysql.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                database=database,
                charset="utf8mb4",
            )
        except pymysql.err.Error as exc:
            raise RuntimeError(
                f"MariaDB 连不上 {user}@{host}:{port}/{database}：{exc}"
            ) from exc
        self._lock: asyncio.Lock = asyncio.Lock()
        self._table: str = table

    async def execute(self, sql: str, params: Sequence[object] | None = None) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._execute, sql, params)

    async def execute_many(self, sql: str, rows: Sequence[Sequence[object]]) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._execute_many, sql, rows)

    async def fetch_all(
        self, sql: str, params: Sequence[object] | None = None
    ) -> list[Mapping[str, object]]:
        async with self._lock:
            return await asyncio.to_thread(self._fetch_all, sql, params)

    async def delete_before(self, before: TimestampLike) -> int:
        return await self.execute(
            f"DELETE FROM {self._table} WHERE timestamp < %s", (_check_cutoff(before),)
        )

    def close(self) -> None:
        """关连接（余量落库之后由调用方调）。"""
        self._conn.close()

    # -- 同步实现（只在线程池里跑，锁已保证单连接不并发）--
    def _execute(self, sql: str, params: Sequence[object] | None) -> int:
        self._conn.ping(reconnect=True)
        with self._conn.cursor() as cursor:
            affected = cursor.execute(sql, tuple(params or ()))
        self._conn.commit()
        return affected or 0

    def _execute_many(self, sql: str, rows: Sequence[Sequence[object]]) -> int:
        self._conn.ping(reconnect=True)
        with self._conn.cursor() as cursor:
            affected = cursor.executemany(sql, [tuple(row) for row in rows])
        self._conn.commit()
        return affected or 0

    def _fetch_all(
        self, sql: str, params: Sequence[object] | None
    ) -> list[Mapping[str, object]]:
        self._conn.ping(reconnect=True)
        with self._conn.cursor() as cursor:
            cursor.execute(sql, tuple(params or ()))
            return [dict(row) for row in cursor.fetchall()]


__all__ = [
    "MariadbAdapter",
    "SqliteAdapter",
    "require_table_name",
]
