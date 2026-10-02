"""数据库日志处理机。

本机**不碰数据库**：建表、写入、检索都在注入的
:class:`~botnode.core.logger.interfaces.LogStore` 后面（默认实现是
:class:`~botnode.db.SqlLogStore`：SQLModel 声明表 + AsyncSession 读写），日志层因此只依赖
一个协议，不依赖任何具体库，也不在手写 SQL。处理机自己只管攒批、调用与容错隔离。
"""
from __future__ import annotations

from typing import override

from ..interfaces import LogStore
from ..models import LogLevel, LogRecord, LogSearchResult, TimestampLike
from ..queue import OverflowPolicy
from .base import BaseLogProcessor, ProcessorStats


class DatabaseProcessorStats(ProcessorStats):
    """:attr:`DatabaseLogProcessor.stats` 的形状：基类的那些字段 + 用的存储类名。"""

    store: str


class DatabaseLogProcessor(BaseLogProcessor):
    """把日志批量写入数据库，并支持条件检索（落地在注入的 ``store`` 上）。"""

    name: str = "database"

    #: 落库与控制台一样是**全局留存出口**：模块给自己挂了专属文件出口（自层覆盖）后，
    #: 日志仍要继续落库——否则业务模块（如 botnode.api 挂了自己的 api.log）的日志
    #: 一条都进不了库，历史检索就只剩框架启动那几条。
    inherit_on_override: bool = True

    def __init__(
        self,
        store: LogStore,
        *,
        name: str | None = None,
        buffer_size: int = 500,
        flush_interval: float = 5.0,
        max_failures: int = 5,
        overflow_policy: "OverflowPolicy | str" = OverflowPolicy.DROP_OLDEST,
    ) -> None:
        """
        :param store: 日志存储，协议见 :class:`~botnode.core.logger.interfaces.LogStore`，
            默认实现是 :class:`~botnode.db.SqlLogStore`；表结构、连接、事务都归它管；
        :param name: 处理机名称，默认 ``"database"``；同时挂多个库出口时
            （例如两个不同库）必须各自命名，名称即唯一标识。
        """
        super().__init__(
            name=name,
            buffer_size=buffer_size,
            flush_interval=flush_interval,
            max_failures=max_failures,
            overflow_policy=overflow_policy,
        )
        self._store: LogStore = store

    # ------------------------------------------------------------------ 生命周期
    @override
    async def _on_start(self) -> None:
        # 后端是数据库，起步前先把表备好（幂等）；建表失败即启动失败，当场看得见
        await self._store.ensure_schema()

    # ------------------------------------------------------------------ 写入
    @override
    async def write(self, records: list[LogRecord]) -> None:
        if not records:
            return
        await self._store.add(records)  # 一批一次往返，落地方式由 store 决定

    # ------------------------------------------------------------------ 检索
    @override
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
    ) -> LogSearchResult:
        # 检索与总数都在 store 那边一次做完（同一套 WHERE），这里只转手
        return await self._store.search(
            query=query,
            level=level,
            start=start,
            end=end,
            logger_name=logger_name,
            owner_id=owner_id,
            limit=limit,
            offset=offset,
        )

    # ------------------------------------------------------------------ 状态
    @property
    @override
    def stats(self) -> DatabaseProcessorStats:
        return {**super().stats, "store": type(self._store).__name__}
