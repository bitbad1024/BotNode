"""令牌注册表：一个 WS 端口接很多客户端，靠令牌认出「这条连接属于哪个账号」。

一个端口、多个账号，就像路由器接一堆设备：谁连进来由**令牌**决定，令牌 -> 账号的对应
关系记在这里。服务端握手时查一次，把账号绑到那条连接上（见
:meth:`nacho.onebot.server.OneBotServer.start`），之后每条事件都知道是哪个账号的。

三种实现，按需挑一个传给 :class:`~nacho.onebot.server.OneBotServer`::

    SqlTokenRegistry(engine)      落库：onebot_tokens 表，能运行时增删（推荐）
    InMemoryTokenRegistry()       内存：不接库时可用，进程重启即失效

**不传注册表 = 不校验**（谁都能连，归属记成匿名），所以不接数据库照样能跑起来。

明文只在签发时露一次（:attr:`IssuedToken.token`），库里存的是 :func:`hash_token` 的摘要。
"""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlmodel import Field, SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

#: 令牌明文的前缀：日志里一眼能认出这是本框架的令牌
TOKEN_PREFIX: str = "nbo_"


def generate_token() -> str:
    """造一个新令牌（明文）；只在签发时露一次，之后查不回来（库里只有摘要）。"""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """令牌摘要（sha256 十六进制）：**库里只存这个**。

    这里刻意**不用**口令那套慢哈希（bcrypt / PBKDF2）：令牌是 32 字节随机串、熵足够高，
    不存在离线爆破的问题；而慢哈希会让每次握手在查库前先耗上几十毫秒，白白拖慢连接建立。
    sha256 是确定性的，所以能直接 ``WHERE token_hash = ?`` 命中索引。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TokenRecord:
    """一条令牌记录（**不含明文**，可以放心地列给管理接口看）。"""

    #: 记录 id；管理接口按它吊销
    id: str
    #: 这个令牌属于哪个账号（一条连接归谁，就是这么定的）
    account: str
    #: 停用：记录还在，但不许再连
    enabled: bool = True
    #: 备注（给人看：这个令牌是给哪个机器人的）
    remark: str = ""
    #: 签发时间（Unix 秒）
    created_at: float = 0.0


@dataclass(frozen=True)
class IssuedToken:
    """签发结果：记录 + **明文**。

    明文只在这一次出现（:attr:`token`），库里只有摘要——给客户端看一眼就得自己存好。
    """

    record: TokenRecord
    token: str


class TokenRegistry(Protocol):
    """令牌注册表：把「连进来的令牌」翻成「哪条记录（含账号）」。

    服务端只认这一个协议，令牌存哪（库 / 内存 / 别处）由实现决定。
    """

    async def resolve(self, token: str) -> TokenRecord | None:
        """令牌对应的记录；令牌不存在 / 被吊销 / 停用就返回 ``None``（握手按 401 拒）。

        返回**整条记录**而不只是账号：连接上要记 ``id``，之后「删掉这个令牌」才知道
        该把哪些连接断开（见 :meth:`nacho.onebot.OneBotServer.revoke_by_id`）。
        """
        ...

    async def issue(self, account: str, *, remark: str = "") -> IssuedToken:
        """给 ``account`` 签一个新令牌。"""
        ...

    async def items(self) -> tuple[TokenRecord, ...]:
        """全部令牌（给管理接口列出来；**不含明文**）。"""
        ...

    async def remove_by_id(self, token_id: str) -> bool:
        """按记录 id 吊销（管理接口列表里拿到的是 id）；真删掉了返回 ``True``。"""
        ...


# --------------------------------------------------------------------------- 落库实现
class TokenTable(SQLModel, table=True):
    """``onebot_tokens`` 表：只存令牌摘要，明文不落库。

        id           VARCHAR(64) PRIMARY KEY
        token_hash   VARCHAR(64) UNIQUE   # sha256(明文)
        account      VARCHAR(64) INDEX
        enabled      BOOLEAN
        remark       VARCHAR(255)
        created_at   FLOAT                # Unix 秒
    """

    # SQLModel 默认按类名生成表名（这里会成 tokentable），显式钉成 onebot_tokens。
    # 基类把 __tablename__ 声明成 declared_attr，写 str 或用 declared_attr 都与 SQLModel
    # 自己的类型标注对不上（库自身的类型缺陷）；行为已验证，故定向忽略。
    __tablename__ = "onebot_tokens"  # pyright: ignore[reportAssignmentType, reportUnannotatedClassAttribute]

    id: str = Field(primary_key=True, max_length=64)
    token_hash: str = Field(unique=True, index=True, max_length=64)
    account: str = Field(index=True, max_length=64)
    enabled: bool = Field(default=True)
    remark: str = Field(default="", max_length=255)
    created_at: float = Field(default_factory=time.time)


def _to_record(row: TokenTable) -> TokenRecord:
    """把一行 :class:`TokenTable` 转成对外流转的 :class:`TokenRecord`。"""
    return TokenRecord(
        id=row.id,
        account=row.account,
        enabled=bool(row.enabled),
        remark=row.remark,
        created_at=float(row.created_at),
    )


class SqlTokenRegistry:
    """令牌放数据库：查 ``onebot_tokens`` 表（用法同
    :class:`nacho.api.services.user.store_sql.SqlUserStore`）。

    引擎由外部注入：本模块不建引擎、不读配置，连接参数归入口层管。
    """

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
            await conn.run_sync(TokenTable.metadata.create_all)

    # ------------------------------------------------------------------ 注册表
    async def resolve(self, token: str) -> TokenRecord | None:
        if not token:
            return None
        async with self._sessions() as session:
            result = await session.exec(
                select(TokenTable).where(TokenTable.token_hash == hash_token(token))
            )
            row = result.first()
        if row is None or not row.enabled:
            return None
        return _to_record(row)

    async def issue(self, account: str, *, remark: str = "") -> IssuedToken:
        token = generate_token()
        row = TokenTable(
            id=f"t-{uuid4().hex[:16]}",
            token_hash=hash_token(token),
            account=account,
            remark=remark,
        )
        async with self._sessions() as session:
            session.add(row)
            await session.commit()
        return IssuedToken(record=_to_record(row), token=token)

    async def items(self) -> tuple[TokenRecord, ...]:
        async with self._sessions() as session:
            result = await session.exec(select(TokenTable))
            return tuple(_to_record(row) for row in result)

    async def remove_by_id(self, token_id: str) -> bool:
        async with self._sessions() as session:
            row = await session.get(TokenTable, token_id)
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True


# --------------------------------------------------------------------------- 内存实现
class InMemoryTokenRegistry:
    """令牌放内存：不接库时用（测试 / 演示），进程重启即失效。"""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[TokenRecord, str]] = {}  # id -> (记录, 明文)

    async def resolve(self, token: str) -> TokenRecord | None:
        if not token:
            return None
        # 按**字节**比：compare_digest 不接受非 ASCII 的 str（会抛 TypeError），
        # 而客户端带什么字符来是不受我们控制的，认不出该返回 None 而不是炸
        given = token.encode("utf-8")
        for record, plain in self._rows.values():
            if secrets.compare_digest(plain.encode("utf-8"), given) and record.enabled:
                return record
        return None

    async def issue(self, account: str, *, remark: str = "") -> IssuedToken:
        token = generate_token()
        record = TokenRecord(
            id=f"t-{uuid4().hex[:16]}",
            account=account,
            remark=remark,
            created_at=time.time(),
        )
        self._rows[record.id] = (record, token)
        return IssuedToken(record=record, token=token)

    async def items(self) -> tuple[TokenRecord, ...]:
        return tuple(record for record, _ in self._rows.values())

    async def remove_by_id(self, token_id: str) -> bool:
        return self._rows.pop(token_id, None) is not None
