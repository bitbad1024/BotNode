"""日志表与它的落库实现：用 SQLModel 描述表，写入走 Core insert、检索走 AsyncSession，不手写 SQL。

表结构由 :class:`LogTable` 声明——类型 / 长度 / 约束写在 Python 里，建表语句由 SQLAlchemy
**按方言生成**，所以 sqlite 与 mariadb 共用同一份定义，不用各写一套 DDL：

    seq          BIGINT       PRIMARY KEY AUTO_INCREMENT  # 插入顺序；检索按它倒序
    record_id    VARCHAR(64)  UNIQUE NOT NULL             # 生成时的 uuid4，去重靠它
    timestamp    DOUBLE       NOT NULL   # Unix 时间戳（秒）
    level        VARCHAR(16)  NOT NULL   # DEBUG / INFO / WARNING / ERROR / CRITICAL
    logger_name  VARCHAR(128) NOT NULL   # 写日志的实例名
    owner_id     VARCHAR(64)  NOT NULL   # 所有者：谁的操作；空串 = 公共所有者
    message      TEXT         NOT NULL
    extra        TEXT                    # JSON 对象串
    exc_text     TEXT                    # 异常栈文本，没有异常就是 NULL

``extra`` 在库里是 JSON 字符串，进出都转一次（解析失败不抛，退回 ``{"raw": 原文}``）；
其余字段直接对应 :class:`~tickneko.core.logger.models.LogRecord`。

**序号与记录 id 分了两个字段**：``seq`` 是自增主键（插入顺序，检索按它倒序），``record_id``
是生成时就定下的 uuid（唯一约束，多出口同一条日志去重靠它）。为什么排序不只看时间戳：同一
次调用里写下的几条时间戳常常一模一样（浮点秒的分辨率、Windows 上 ``time.time()`` 约 15ms 的
粒度），那时谁先谁后只有序号知道，翻页也不会因为「同刻几条顺序随机」而漏记 / 重记。

> **升级提示**：``seq`` 是后加的主键列，而 :meth:`SqlLogStore.ensure_schema` 只**建表**、
> 不改表 —— 已经存在的 ``logs`` 表不会自己长出这一列。升级时把日志表删掉重建即可
> （``DROP TABLE logs;``，下次启动按新定义建回来）；要留审计历史就先导出去再删。

本类只管「建表 / 写 / 查 / 清」，**不做缓冲与攒批**——什么时候写、一次写多少由处理机
说了算（见 :class:`~tickneko.core.logger.processors.database.DatabaseLogProcessor`）。

引擎由外部注入（:class:`AsyncEngine`）：本模块不建引擎、不读配置，连接参数归入口层管；
连接 / 会话都是「一次操作一个」，用完即关（写入一个事务，检索一个会话）。
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import cast

from sqlalchemy import BigInteger, Column, ColumnElement, Double, Integer, Text, func, insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlmodel import Field, SQLModel, col, delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from tickneko.core.logger.models import (
    LogLevel,
    LogRecord,
    LogSearchResult,
    TimestampLike,
    normalize_timestamp,
)


class LogTable(SQLModel, table=True):
    """``logs`` 表：一条日志一行。"""

    # SQLModel 默认按类名生成表名（这里会成 logtable），显式钉成 logs。
    # 基类把 __tablename__ 声明成 declared_attr，写 str 或用 declared_attr 都与 SQLModel
    # 自己的类型标注对不上（库自身的类型缺陷）；行为已验证（表名确为 logs），故定向忽略。
    __tablename__ = "logs"  # pyright: ignore[reportAssignmentType, reportUnannotatedClassAttribute]

    #: 自增序号：**插入顺序**，检索按它倒序（时间戳粒度不够时靠它定序，见
    #: :attr:`~tickneko.core.logger.models.LogRecord.seq`）。主键由它当，不是 uuid。
    #: 类型用 BIGINT，但 sqlite 退回 INTEGER：只有 ``INTEGER PRIMARY KEY`` 才是 rowid 别名
    #: （能自动分配），``BIGINT PRIMARY KEY`` 在 sqlite 上不会自增。
    seq: int | None = Field(
        default=None,
        primary_key=True,
        sa_type=BigInteger().with_variant(Integer, "sqlite"),
    )
    #: 记录 id（生成时就是 uuid4 的十六进制）：**唯一**，多出口同一条日志去重靠它
    #: （以前它是主键，所以那时「同一批里重复也只落一条」是顺带的；现在靠这条唯一约束）。
    record_id: str = Field(unique=True, max_length=64)
    #: Unix 时间戳（秒）。用 DOUBLE 而不是默认的 FLOAT：后者在 MariaDB 上只有 32 位有效
    #: 数字，存下秒级时间戳会直接丢精度
    timestamp: float = Field(sa_column=Column(Double(), nullable=False))
    #: 级别名（``DEBUG`` / ``INFO`` / ...）。存名字而不是数值：翻库时一眼看得懂
    level: str = Field(max_length=16)
    #: 写日志的实例名（``tickneko.robot`` 这类）
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


def _to_values(record: LogRecord) -> dict[str, object]:
    """把一条日志转成一行**取值**（``extra`` 编成 JSON，不限 ASCII，中文原样存）。

    给的是 dict 而不是 :class:`LogTable` 实例：写入走 Core 的 ``insert``（一批一次
    executemany），不从 ORM 那边过 —— 日志是只写不读的，进身份映射只会平白多一轮
    「回读自增主键」。
    """
    return {
        "record_id": record.record_id,
        "timestamp": record.timestamp,
        "level": record.level.name,
        "logger_name": record.logger_name,
        "owner_id": record.owner_id,
        "message": record.message,
        "extra": json.dumps(record.extra, ensure_ascii=False),
        "exc_text": record.exc_text,
    }


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
    """把一行 :class:`LogTable` 还原成内部流转的 :class:`LogRecord`（含自增序号）。"""
    return LogRecord(
        message=row.message,
        level=LogLevel.parse(row.level),
        logger_name=row.logger_name,
        timestamp=row.timestamp,
        record_id=row.record_id,
        extra=_decode_extra(row.extra),
        exc_text=row.exc_text,
        owner_id=row.owner_id or "",
        seq=row.seq or 0,
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

        空批直接返回：不开连接、不提交空事务。

        写入走引擎 + Core ``insert``（一次 executemany，一个事务）：日志是**只写不读**的，
        不必让 ORM 把自增主键回读进身份映射 —— 那样一批多条时主键分配还可能互相打架
        （SQLAlchemy 会为此告警），而主键交给数据库连着往下发就好。
        """
        if not records:
            return 0
        values = [_to_values(record) for record in records]
        async with self._engine.begin() as conn:
            _ = await conn.execute(insert(LogTable), values)
        return len(records)

    # ------------------------------------------------------------------ 检索
    def _filter_clauses(
        self,
        *,
        query: str | None,
        level: LogLevel | str | None,
        start: TimestampLike,
        end: TimestampLike,
        logger_name: str | None,
        owner_id: str | None,
    ) -> list[ColumnElement[bool]]:
        """把检索条件拼成一维 ``WHERE`` 子句：``search`` 的**这一页**与**总数**共用同一套。

        共用一处，是为了让「翻页的条目」与「翻页的总数」永远算同一批记录。
        ``owner_id`` 给 ``None`` 不限所有者，给空串就是只看公共日志。
        """
        clauses: list[ColumnElement[bool]] = []
        if level is not None:
            clauses.append(col(LogTable.level) == LogLevel.parse(level).name)
        if logger_name is not None:
            clauses.append(col(LogTable.logger_name) == logger_name)
        if owner_id is not None:
            clauses.append(col(LogTable.owner_id) == owner_id)

        start_ts = normalize_timestamp(start)
        if start_ts is not None:
            clauses.append(col(LogTable.timestamp) >= start_ts)

        end_ts = normalize_timestamp(end)
        if end_ts is not None:
            clauses.append(col(LogTable.timestamp) <= end_ts)

        if query:
            clauses.append(col(LogTable.message).contains(query))
        return clauses

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
        """按条件检索：级别 / 实例名 / 所有者精确匹配，正文模糊匹配，时间戳闭区间，按**序号倒序**。

        过滤与排序都交给数据库做（条件拼进 ``WHERE`` / ``ORDER BY``），不全表捞回来再筛。
        ``owner_id`` 给 ``None`` 不限所有者，给空串就是只看公共日志。

        排序键是自增 ``seq`` 而**不是**时间戳：同一毫秒（甚至同一批）写下的几条时间戳可能
        一模一样，那时「谁在谁后面」只有序号知道；序号单调且唯一，翻页也不会跳条 / 重条。

        **一页 + 总数**：两条语句共用同一套 ``WHERE``（:meth:`_filter_clauses`），开在同一个
        会话里 —— 总数只跟条件有关，与 ``limit`` / ``offset`` 无关，所以翻到第几页都算得出总页数。
        """
        clauses = self._filter_clauses(
            query=query,
            level=level,
            start=start,
            end=end,
            logger_name=logger_name,
            owner_id=owner_id,
        )
        page_statement = (
            select(LogTable)
            .where(*clauses)
            .order_by(col(LogTable.seq).desc())
            .offset(int(offset))
            .limit(int(limit))
        )
        total_statement = select(func.count()).select_from(LogTable).where(*clauses)
        async with self._sessions() as session:
            result = await session.exec(page_statement)
            records = [_to_record(row) for row in result]
            total = int((await session.exec(total_statement)).one())
        return LogSearchResult(records=records, total=total)

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
