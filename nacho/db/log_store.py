"""日志表与它的落库实现：用 SQLModel 描述表、用 AsyncSession 读写，不手写 SQL。

表结构由 :class:`LogTable` 声明——类型 / 长度 / 约束写在 Python 里，建表语句由 SQLAlchemy
**按方言生成**，所以 sqlite 与 mariadb 共用同一份定义，不用各写一套 DDL：

    record_id    VARCHAR(64)  PRIMARY KEY
    timestamp    DOUBLE       NOT NULL   # Unix 时间戳（秒），检索按它倒序
    level        VARCHAR(16)  NOT NULL   # DEBUG / INFO / WARNING / ERROR / CRITICAL
    logger_name  VARCHAR(128) NOT NULL   # 写日志的实例名
    owner_id     VARCHAR(64)  NOT NULL   # 所有者：谁的操作；空串 = 公共所有者
    message      TEXT         NOT NULL
    extra        TEXT                    # JSON 对象串
    exc_text     TEXT                    # 异常栈文本，没有异常就是 NULL

``extra`` 在库里是 JSON 字符串，进出都转一次（解析失败不抛，退回 ``{"raw": 原文}``）；
其余字段直接对应 :class:`~nacho.core.logger.models.LogRecord`。

本类只管「建表 / 写 / 查 / 清」，**不做缓冲与攒批**——什么时候写、一次写多少由处理机
说了算（见 :class:`~nacho.core.logger.processors.database.DatabaseLogProcessor`）。

引擎由外部注入（:class:`AsyncEngine`）：本模块不建引擎、不读配置，连接参数归入口层管；
会话按「一次写入 / 一次查询一个会话」开，用完即关。
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import cast

from sqlalchemy import Column, Double, Text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlmodel import Field, SQLModel, col, delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from nacho.core.logger.models import LogLevel, LogRecord, TimestampLike, normalize_timestamp


class LogTable(SQLModel, table=True):
    """``logs`` 表：一条日志一行。"""

    # SQLModel 默认按类名生成表名（这里会成 logtable），显式钉成 logs。
    # 基类把 __tablename__ 声明成 declared_attr，写 str 或用 declared_attr 都与 SQLModel
    # 自己的类型标注对不上（库自身的类型缺陷）；行为已验证（表名确为 logs），故定向忽略。
    __tablename__ = "logs"  # pyright: ignore[reportAssignmentType, reportUnannotatedClassAttribute]

    #: 记录 id（生成时就是 uuid4 的十六进制），主键：同一批里重复也只落一条
    record_id: str = Field(primary_key=True, max_length=64)
    #: Unix 时间戳（秒）。用 DOUBLE 而不是默认的 FLOAT：后者在 MariaDB 上只有 32 位有效
    #: 数字，存下秒级时间戳会直接丢精度
    timestamp: float = Field(sa_column=Column(Double(), nullable=False))
    #: 级别名（``DEBUG`` / ``INFO`` / ...）。存名字而不是数值：翻库时一眼看得懂
    level: str = Field(max_length=16)
    #: 写日志的实例名（``nacho.robot`` 这类）
    logger_name: str = Field(max_length=128)
    #: 所有者：这条日志是谁的操作（api 层填登录用户 id、ws 层填那条连接的归属）；
    #: 空串 = 公共所有者，走默认值，所以历史行 / 不带归属的写法都能直接落进来
    owner_id: str = Field(default="", max_length=64)
    #: 日志正文；长度不限，所以是 TEXT 而不是带长度的 VARCHAR
    message: str = Field(sa_column=Column(Text(), nullable=False))
    #: 附加字段：JSON 对象串
    extra: str | None = Field(default=None, sa_column=Column(Text(), nullable=True))
    #: 异常栈文本，没有异常就是 NULL
    exc_text: str | None = Field(default=None, sa_column=Column(Text(), nullable=True))


def _to_row(record: LogRecord) -> LogTable:
    """把一条日志转成表行：``extra`` 编成 JSON（不限 ASCII，中文原样存）。"""
    return LogTable(
        record_id=record.record_id,
        timestamp=record.timestamp,
        level=record.level.name,
        logger_name=record.logger_name,
        owner_id=record.owner_id,
        message=record.message,
        extra=json.dumps(record.extra, ensure_ascii=False),
        exc_text=record.exc_text,
    )


def _decode_extra(raw: str | None) -> dict[str, object]:
    """把库里的 extra 串还原成 ``dict[str, object]``。

    空值 / 坏 JSON / 顶层不是对象，一律当成「没有额外字段」或退回原文，**不抛**：
    一条坏数据不该让整次检索失败。
    """
    if not raw:
        return {}
    try:
        # json.loads 在 typeshed 中返回 Any，先 cast 成 object 表态：解出来的形状未知，
        # 下面用 isinstance 现场校验
        decoded = cast(object, json.loads(raw))
    except ValueError:
        return {"raw": raw}
    if isinstance(decoded, Mapping):
        return {
            str(key): item
            for key, item in cast("Mapping[object, object]", decoded).items()
        }
    return {"raw": raw}


def _to_record(row: LogTable) -> LogRecord:
    """把一行 :class:`LogTable` 还原成内部流转的 :class:`LogRecord`。"""
    return LogRecord(
        message=row.message,
        level=LogLevel.parse(row.level),
        logger_name=row.logger_name,
        timestamp=row.timestamp,
        record_id=row.record_id,
        extra=_decode_extra(row.extra),
        exc_text=row.exc_text,
        owner_id=row.owner_id or "",
    )


class SqlLogStore:
    """把日志放在数据库里：一次一批写入、按条件检索，走 SQLModel 会话。"""

    def __init__(self, engine: AsyncEngine) -> None:
        """
        :param engine: 异步引擎（由入口层按 :class:`DatabaseSettings` 建好传进来）。
        """
        self._engine: AsyncEngine = engine
        # commit 后不把属性置过期：异步下再取属性会触发隐式刷新（lazy load）而报错
        # class_ 显式给 SQLModel 的 AsyncSession（它才有 exec），泛型参是不变的，不写会推成基类
        self._sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )

    # ------------------------------------------------------------------ 启动
    async def ensure_schema(self) -> None:
        """建表（幂等）：DDL 按方言生成，已有的表就跳过。"""
        async with self._engine.begin() as conn:
            await conn.run_sync(LogTable.metadata.create_all)

    # ------------------------------------------------------------------ 写入
    async def add(self, records: Sequence[LogRecord]) -> int:
        """把一批日志写进 ``logs`` 表，返回写入条数。

        空批直接返回：不开会话、不提交空事务。
        """
        if not records:
            return 0
        async with self._sessions() as session:
            session.add_all([_to_row(record) for record in records])
            await session.commit()
            return len(records)

    # ------------------------------------------------------------------ 检索
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
        """按条件检索：级别 / 实例名 / 所有者精确匹配，正文模糊匹配，时间戳闭区间，按时间倒序。

        过滤与排序都交给数据库做（条件拼进 ``WHERE`` / ``ORDER BY``），不全表捞回来再筛。
        ``owner_id`` 给 ``None`` 不限所有者，给空串就是只看公共日志。
        """
        statement = select(LogTable)
        if level is not None:
            statement = statement.where(LogTable.level == LogLevel.parse(level).name)
        if logger_name is not None:
            statement = statement.where(LogTable.logger_name == logger_name)
        if owner_id is not None:
            statement = statement.where(LogTable.owner_id == owner_id)

        start_ts = normalize_timestamp(start)
        if start_ts is not None:
            statement = statement.where(LogTable.timestamp >= start_ts)

        end_ts = normalize_timestamp(end)
        if end_ts is not None:
            statement = statement.where(LogTable.timestamp <= end_ts)

        if query:
            statement = statement.where(col(LogTable.message).contains(query))

        statement = (
            statement.order_by(col(LogTable.timestamp).desc())
            .offset(int(offset))
            .limit(int(limit))
        )
        async with self._sessions() as session:
            result = await session.exec(statement)
            return [_to_record(row) for row in result]

    # ------------------------------------------------------------------ 清理
    async def delete_before(self, before: TimestampLike) -> int:
        """删除 ``before`` 之前的历史日志，返回删除条数。

        ``before`` 是宽松输入，先归一化成 Unix 时间戳再比；``None`` 属非法输入。
        """
        cutoff = normalize_timestamp(before)
        if cutoff is None:
            raise ValueError("delete_before 需要一个明确的时刻，不能是 None")
        # DML 走引擎而不是会话：SQLModel 的 AsyncSession 已不认 execute（要 exec），而 exec
        # 只收检索语句、拿不回行数；引擎这条一次往返、行数也拿得到（与 ensure_schema 同风格）
        async with self._engine.begin() as conn:
            result = await conn.execute(
                delete(LogTable).where(col(LogTable.timestamp) < cutoff)
            )
            return int(result.rowcount or 0)


__all__ = [
    "LogTable",
    "SqlLogStore",
]
