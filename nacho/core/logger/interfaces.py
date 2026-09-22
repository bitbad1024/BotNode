"""日志系统对外依赖的抽象协议。

日志系统不直接依赖具体数据库实现，只依赖这里的协议：数据库层（``nacho.db``）给出一个
符合协议的对象（如 :class:`~nacho.db.SqlLogStore`），就能当数据库日志处理机的后端，
从而保证「日志层」与「数据库层」解耦。

:class:`LogStore` 约定的都是「基本能力」，没有可选 / 额外能力之分，实现必须全部给出：

* 建表：:meth:`LogStore.ensure_schema`（幂等，起步时调一次）；
* 增加：:meth:`LogStore.add`（一次一批，处理机攒够了才交过来）；
* 查询：:meth:`LogStore.search`（条件与处理机的 ``search`` 一一对应）；
* 清理历史日志：:meth:`LogStore.delete_before`，
  调用方只需传入「删除该时刻之前」的时间参数。

语句、连接、事务都在实现那边（如 SQLModel + :class:`~sqlmodel.ext.asyncio.session.AsyncSession`），
处理机只负责攒批、调用与容错。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from .models import LogLevel, LogRecord, TimestampLike


@runtime_checkable
class LogStore(Protocol):
    """日志存储协议（由数据库层实现）。

    方法均为基本能力，实现需要全部给出。
    """

    async def ensure_schema(self) -> None:
        """建表（幂等）：表已经在就什么都不做。"""
        ...

    async def add(self, records: Sequence[LogRecord]) -> int:
        """增加：把一批日志落库，返回写入条数。"""
        ...

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
        """查询：按条件检索已落地的日志。

        ``owner_id`` 精确匹配所有者：``None`` 不限（默认），空串 = 只要公共的。
        """
        ...
        ...

    async def delete_before(self, before: TimestampLike) -> int:
        """清理历史日志：删除 ``before`` 时刻之前的记录，返回删除行数。

        约定：``before`` 是宽松输入（时间戳 / ISO 字符串 /
        :class:`datetime.datetime`），实现**必须**先用
        :func:`~nacho.core.logger.models.normalize_timestamp` 归一化成
        Unix 时间戳（``float`` 秒）再比较 / 落查询；不要直接拿原始入参去比，
        也不要把规范形式改成字符串——默认表结构的 ``timestamp`` 列是数值类型，
        传字符串既比不对，各数据库方言的解析函数差异也很大。

        :param before: 删除该时刻之前的日志；``None`` 属于非法输入，
            应抛 :class:`ValueError`。
        """
        ...
