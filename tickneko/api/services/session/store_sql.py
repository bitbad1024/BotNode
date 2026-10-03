"""会话存储的落库实现：``auth_sessions`` 表，一行一次登录。

表结构由 :class:`SessionTable` 声明，DDL 按方言生成（sqlite / mariadb 共用一份定义），
查询走 ``AsyncSession``，不手写 SQL。

    token_hash    VARCHAR(64)  PRIMARY KEY   # **令牌摘要**（不是明文）
    user_id       VARCHAR(64)  INDEX
    remembered    BOOLEAN                    # 勾了「记住设备」才真
    created_at    FLOAT
    ip            VARCHAR(64)
    device_name   VARCHAR(128)
    device_type   VARCHAR(16)                # mobile / desktop / unknown
    browser       VARCHAR(32)
    os            VARCHAR(32)
    user_agent    VARCHAR(512)

**主键就是令牌摘要**（列名就叫 ``token_hash``，不叫 ``id``，免得又有人以为是两样东西）：
摘要不可逆，拿它当对外 id 是安全的，吊销时 ``DELETE WHERE token_hash = 摘要`` 即可。
库里存了摘要，缓存就只是**加速层**——缓存没了、Redis 重启了，照样能回库认出令牌。

.. note::
   这一列以前叫 ``id``。``create_all`` 是幂等的、**不会改已有的表**，所以老库要手工
   ``ALTER TABLE auth_sessions RENAME COLUMN id TO token_hash;``（mariadb 同样语法），
   或者直接删表重建（会话数据本来就是易失的）。
"""
from __future__ import annotations

import time

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlmodel import Field, SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from .errors import TokenHashCollisionError
from .models import ClientInfo, SessionRecord


class SessionTable(SQLModel, table=True):
    """``auth_sessions`` 表：一次登录一行（令牌摘要 + 设备信息）。"""

    # SQLModel 默认按类名生成表名（这里会成 sessiontable），显式钉成 auth_sessions。
    # 基类把 __tablename__ 声明成 declared_attr，写 str 或 declared_attr 都对不上类型
    # （库自身的类型缺陷）；行为已验证，故定向忽略。
    __tablename__ = "auth_sessions"  # pyright: ignore[reportAssignmentType, reportUnannotatedClassAttribute]

    #: 令牌摘要：主键，也是对外露的那个 id
    token_hash: str = Field(primary_key=True, max_length=64)
    user_id: str = Field(index=True, max_length=64)
    remembered: bool = Field(default=False)
    created_at: float = Field(default_factory=time.time)
    ip: str = Field(default="", max_length=64)
    device_name: str = Field(default="", max_length=128)
    device_type: str = Field(default="", max_length=16)
    browser: str = Field(default="", max_length=32)
    os: str = Field(default="", max_length=32)
    user_agent: str = Field(default="", max_length=512)


def _to_record(row: SessionTable) -> SessionRecord:
    """把一行 :class:`SessionTable` 转成业务层流转的 :class:`SessionRecord`。"""
    return SessionRecord(
        token_hash=row.token_hash,
        user_id=row.user_id,
        created_at=float(row.created_at),
        remembered=bool(row.remembered),
        ip=row.ip,
        device_name=row.device_name,
        device_type=row.device_type,
        browser=row.browser,
        os=row.os,
        user_agent=row.user_agent,
    )


class SqlSessionStore:
    """会话放数据库：用法同
    :class:`tickneko.api.services.user.store_sql.SqlUserStore`（引擎由入口层注入）。"""

    def __init__(self, engine: AsyncEngine) -> None:
        """:param engine: 异步引擎（由入口层按 :class:`DatabaseSettings` 建好传进来）。"""
        self._engine: AsyncEngine = engine
        # commit 后不把属性置过期：异步下再取属性会触发隐式刷新（lazy load）而报错
        self._sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )

    # ------------------------------------------------------------------ 启动
    async def ensure_schema(self) -> None:
        """建表（幂等）：DDL 按方言生成，已有的表就跳过。"""
        async with self._engine.begin() as conn:
            await conn.run_sync(SessionTable.metadata.create_all)

    # ------------------------------------------------------------------ 增删查
    async def create(
        self,
        token_hash: str,
        user_id: str,
        *,
        client: ClientInfo,
        remembered: bool,
    ) -> SessionRecord:
        row = SessionTable(
            token_hash=token_hash,
            user_id=user_id,
            remembered=remembered,
            created_at=time.time(),
            ip=client.ip,
            device_name=client.device_name,
            device_type=client.device_type,
            browser=client.browser,
            os=client.os,
            user_agent=client.user_agent,
        )
        try:
            async with self._sessions() as session:
                session.add(row)
                await session.commit()
        except IntegrityError as exc:
            # 摘要撞上主键（别的约束这儿也撞不上）。翻成域内的错，别把 sqlalchemy 的类型
            # 漏到存储层之外；会话在这里就回滚了，什么都没写进去。
            raise TokenHashCollisionError(token_hash) from exc
        return _to_record(row)

    async def get(self, token_hash: str) -> SessionRecord | None:
        async with self._sessions() as session:
            row = await session.get(SessionTable, token_hash)
            return _to_record(row) if row is not None else None

    async def set_remembered(self, token_hash: str, *, remembered: bool) -> SessionRecord | None:
        async with self._sessions() as session:
            row = await session.get(SessionTable, token_hash)
            if row is None:
                return None
            row.remembered = remembered
            session.add(row)
            await session.commit()
            return _to_record(row)

    async def remove(self, token_hash: str) -> bool:
        async with self._sessions() as session:
            row = await session.get(SessionTable, token_hash)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def remove_all(self, user_id: str) -> int:
        # 先查再逐条删（而不是一条 DELETE ... WHERE）：一个用户开着的登录本来就没几条，
        # 这样也不用碰 sqlalchemy 的 delete() 与 Result.rowcount 那套类型标注。
        async with self._sessions() as session:
            result = await session.exec(
                select(SessionTable).where(col(SessionTable.user_id) == user_id)
            )
            rows = list(result.all())
            for row in rows:
                await session.delete(row)
            await session.commit()
            return len(rows)

    async def list_for_user(self, user_id: str) -> tuple[SessionRecord, ...]:
        async with self._sessions() as session:
            result = await session.exec(
                select(SessionTable)
                .where(col(SessionTable.user_id) == user_id)
                .order_by(col(SessionTable.created_at).desc())
            )
            return tuple(_to_record(row) for row in result)
