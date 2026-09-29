"""机器人凭证的落库实现：``bot_credentials`` 表，用 SQLModel 描述、AsyncSession 读写。

表结构（类型 / 约束写在 Python 里，DDL 按方言生成，sqlite 与 mariadb 共用一份）::

    bot_credentials         一个机器人一行（跨平台，platform 区分底层适配器）
        bot_id        VARCHAR(64)  PRIMARY KEY   这一行的主键（跨平台唯一）
        platform      VARCHAR(16)  INDEX         底层适配器：onebot / kook
        owner_id      VARCHAR(64)  INDEX         归属用户（多用户隔离的过滤列）
        token_hash    VARCHAR(64)  UNIQUE        令牌 / Bot Token 的 sha256 摘要
        account       VARCHAR(64)                机器人账号（展示用）
        enabled       BOOLEAN      DEFAULT 1     停用开关
        remark        VARCHAR(255)
        created_at    FLOAT                       Unix 秒

与旧 ``onebot_tokens`` 的差异：旧表 ``id`` 即 owner_id 主键（一个归属一条）；新表
``bot_id`` 主键（一个用户多个机器人），``owner_id`` 降为过滤列，``platform`` 区分平台。
引擎由外部注入（同用户 / 会话 / 工作流存储的惯例），本模块不建引擎、不读配置。
"""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlmodel import Field, SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from .models import BotCredential, BotPlatform


def hash_token(token: str) -> str:
    """令牌 / Bot Token 摘要（sha256 十六进制）：**库里只存这个**。

    同 :func:`nacho.onebot.tokens.hash_token`：令牌是随机串、熵足够高，不用慢哈希；
    sha256 确定性，能 ``WHERE token_hash = ?`` 命中唯一索引。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IssuedBotCredential:
    """签发结果：记录 + **明文**（明文只在这一次出现，之后查不回来）。"""

    record: BotCredential
    token: str


class BotCredentialTable(SQLModel, table=True):
    """``bot_credentials`` 表：一个机器人一行，只存令牌摘要，明文不落库。"""

    # SQLModel 默认按类名生成表名，显式钉死（同其它表）。
    __tablename__ = "bot_credentials"  # pyright: ignore[reportAssignmentType, reportUnannotatedClassAttribute]

    bot_id: str = Field(primary_key=True, max_length=64)
    platform: str = Field(index=True, max_length=16)
    owner_id: str = Field(index=True, max_length=64)
    token_hash: str = Field(unique=True, index=True, max_length=64)
    account: str = Field(default="", max_length=64)
    enabled: bool = Field(default=True)
    remark: str = Field(default="", max_length=255)
    created_at: float = Field(default_factory=time.time)


def _to_record(row: BotCredentialTable) -> BotCredential:
    """一行表 -> 对外流转的 :class:`BotCredential`。"""
    return BotCredential(
        platform=row.platform,  # type: ignore[arg-type]  # 落库时校验过，这里回字面量
        owner_id=row.owner_id,
        bot_id=row.bot_id,
        token_hash=row.token_hash,
        account=row.account,
        enabled=bool(row.enabled),
        remark=row.remark,
        created_at=float(row.created_at),
    )


class SqlBotStore:
    """机器人凭证放数据库：查 ``bot_credentials`` 表。

    引擎由外部注入（同其它落库存储）；本模块不建引擎、不读配置。
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine: AsyncEngine = engine
        # commit 后不把属性置过期：异步下再取属性会触发隐式刷新而报错
        self._sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )

    # ------------------------------------------------------------------ 启动
    async def ensure_schema(self) -> None:
        """建表（幂等）：DDL 按方言生成，已有的表跳过。"""
        async with self._engine.begin() as conn:
            await conn.run_sync(BotCredentialTable.metadata.create_all)

    # ------------------------------------------------------------------ 注册表
    async def resolve(self, token: str) -> BotCredential | None:
        """按令牌明文 -> 摘要，找到对应记录（握手入口）；令牌不存在 / 停用返回 None。"""
        if not token:
            return None
        async with self._sessions() as session:
            result = await session.exec(
                select(BotCredentialTable).where(
                    BotCredentialTable.token_hash == hash_token(token)
                )
            )
            row = result.first()
        if row is None or not row.enabled:
            return None
        return _to_record(row)

    async def issue(
        self,
        owner_id: str,
        *,
        platform: BotPlatform = "onebot",
        account: str = "",
        remark: str = "",
        token: str | None = None,
    ) -> IssuedBotCredential:
        """给 ``owner_id`` 签一个机器人（每次**新 bot_id**，一个用户可多个）。

        ``token`` 不传就随机生成（OneBot 反向 WS 用）；Kook（P6）由用户填 Bot Token，
        传入即按它存摘要。``platform`` 决定这条凭证属于哪个底层适配器。

        :raises ValueError: ``token`` 为空串（既没传也没生成出有效值）。
        """
        if token is None:
            token = "nbo_" + secrets.token_urlsafe(32)
        if not token:
            raise ValueError("机器人凭证不能是空令牌")
        row = BotCredentialTable(
            bot_id=uuid4().hex,
            platform=platform,
            owner_id=owner_id,
            token_hash=hash_token(token),
            account=account,
            remark=remark,
        )
        async with self._sessions() as session:
            session.add(row)
            await session.commit()
        return IssuedBotCredential(record=_to_record(row), token=token)

    async def list_records(self, *, owner_id: str | None = None) -> list[BotCredential]:
        """列机器人；给 ``owner_id`` 就只列那个归属下的（多实例：一个用户多条）。"""
        statement = select(BotCredentialTable)
        if owner_id is not None:
            statement = statement.where(BotCredentialTable.owner_id == owner_id)
        statement = statement.order_by(BotCredentialTable.created_at.desc())
        async with self._sessions() as session:
            result = await session.exec(statement)
            return [_to_record(row) for row in result.all()]

    async def get_by_bot_id(self, bot_id: str) -> BotCredential | None:
        """按 bot_id 取一条；没有返回 None。"""
        async with self._sessions() as session:
            row = await session.get(BotCredentialTable, bot_id)
        return None if row is None else _to_record(row)

    async def remove_by_bot_id(self, bot_id: str) -> bool:
        """按 bot_id 删一条；删掉了返回 True。"""
        async with self._sessions() as session:
            row = await session.get(BotCredentialTable, bot_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def set_enabled(self, bot_id: str, enabled: bool) -> bool:
        """启用 / 停用一条；改到了返回 True。"""
        async with self._sessions() as session:
            row = await session.get(BotCredentialTable, bot_id)
            if row is None:
                return False
            row.enabled = enabled
            await session.commit()
            return True
