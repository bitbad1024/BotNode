"""UserStore 的落库实现：用 SQLModel 描述表、用 AsyncSession 查，不手写 SQL。

表结构由 :class:`UserTable` 声明——类型 / 长度 / 约束写在 Python 里，建表语句由 SQLAlchemy
**按方言生成**，所以 sqlite 与 mariadb 共用同一份定义，不用各写一套 DDL（也就不会出现
``TEXT`` 做主键在 MariaDB 上建不了表这类问题）：

    id             VARCHAR(64)  PRIMARY KEY
    account        VARCHAR(32)  UNIQUE（长度对齐 ``ACCOUNT_MAX_LENGTH``）
    password_hash  VARCHAR(255) NOT NULL                # 算法$迭代$盐$摘要
    nickname       VARCHAR(128) NOT NULL DEFAULT ''
    roles          TEXT         NOT NULL DEFAULT '[]'   # JSON 数组
    disabled       BOOLEAN      NOT NULL DEFAULT FALSE

``roles`` 在库里是 JSON 字符串，进出都转成 ``tuple[str, ...]``；其余字段直接对应
:class:`~botnode.api.services.user.models.UserRecord`。本类负责「查」与「注册那一笔新增」（外加
建表 / 种演示账号）——改资料 / 停用 / 删除还没有：真要管账号，照同一份协议另接即可。

引擎由外部注入（:class:`AsyncEngine`）：本模块不建引擎、不读配置，连接参数归入口层管；
会话按「一次查询一个会话」开，用完即关。
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from typing import cast

from sqlalchemy import Column, Text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlmodel import Field, SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from ...common.errors import AccountAlreadyExistsError
from .demo import DEMO_USERS
from .models import USER_ROLE, UserRecord
from .protocols import PasswordHasher
from .security import Pbkdf2PasswordHasher


class UserTable(SQLModel, table=True):
    """``users`` 表：登录要用到的全部字段（密码只存哈希，明文不落库）。"""

    # SQLModel 默认按类名生成表名（这里会成 usertable），显式钉成 users。
    # 基类把 __tablename__ 声明成 declared_attr，写 str 或用 declared_attr 都与 SQLModel
    # 自己的类型标注对不上（库自身的类型缺陷）；行为已验证（表名确为 users），故定向忽略。
    __tablename__ = "users"  # pyright: ignore[reportAssignmentType, reportUnannotatedClassAttribute]

    #: 用户 id；令牌里带的就是它
    id: str = Field(primary_key=True, max_length=64)
    #: 登录账号；唯一，登录时按它查
    account: str = Field(unique=True, index=True, max_length=32)
    password_hash: str = Field(max_length=255)
    nickname: str = Field(default="", max_length=128)
    #: 角色：库里存 JSON 数组字符串，进出由 :func:`_decode_roles` / ``json.dumps`` 转
    roles: str = Field(default="[]", sa_column=Column(Text(), nullable=False))
    disabled: bool = Field(default=False)


def _decode_roles(raw: str) -> tuple[str, ...]:
    """把库里的 roles 串还原成角色元组；坏了 / 不是数组就当成没有角色（不抛）。"""
    try:
        # json.loads 在 typeshed 里返回 Any，先 cast 成 object 表态，下面用 isinstance 现场校验
        decoded = cast(object, json.loads(raw))
    except ValueError:
        return ()
    if isinstance(decoded, list):
        return tuple(str(item) for item in cast("list[object]", decoded))
    return ()


def _to_record(row: UserTable) -> UserRecord:
    """把一行 :class:`UserTable` 转成内部流转的 :class:`UserRecord`。"""
    return UserRecord(
        id=row.id,
        account=row.account,
        password_hash=row.password_hash,
        nickname=row.nickname,
        roles=_decode_roles(row.roles),
        disabled=bool(row.disabled),
    )


class SqlUserStore:
    """把用户放在数据库里：按账号 / 按 id 两路查询，走 SQLModel 会话。"""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        hasher: PasswordHasher | None = None,
    ) -> None:
        """
        :param engine: 异步引擎（由入口层按 :class:`DatabaseSettings` 建好传进来）；
        :param hasher: 密码哈希器，只给 :meth:`seed_demo` 用，默认 PBKDF2。
        """
        self._engine: AsyncEngine = engine
        self._hasher: PasswordHasher = hasher if hasher is not None else Pbkdf2PasswordHasher()
        # commit 后不把属性置过期：异步下再取属性会触发隐式刷新（lazy load）而报错
        # class_ 显式给 SQLModel 的 AsyncSession（它才有 exec），泛型参是不变的，不写会推成基类
        self._sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )

    # ------------------------------------------------------------------ 启动
    async def ensure_schema(self) -> None:
        """建表（幂等）：DDL 按方言生成，已有的表就跳过。"""
        async with self._engine.begin() as conn:
            await conn.run_sync(UserTable.metadata.create_all)

    async def seed_demo(self) -> int:
        """表是空的时候种入演示账号（只有一个 admin），返回**种了几条**。

        已有数据就一条都不动（返回 0）——幂等：谁调都不会把现有账号改成演示账号。
        """
        async with self._sessions() as session:
            existing = await session.exec(select(UserTable.id).limit(1))
            if existing.first() is not None:
                return 0
            for account, password, nickname, roles, disabled in DEMO_USERS:
                session.add(
                    UserTable(
                        id=f"u-{account}",
                        account=account,
                        password_hash=self._hasher.hash(password),
                        nickname=nickname,
                        roles=json.dumps(list(roles), ensure_ascii=False),
                        disabled=disabled,
                    )
                )
            await session.commit()
            return len(DEMO_USERS)

    # ------------------------------------------------------------------ 新增
    async def add(
        self,
        *,
        account: str,
        password_hash: str,
        nickname: str = "",
        roles: Iterable[str] = (USER_ROLE,),
    ) -> UserRecord:
        """新增一个账号（注册那一笔写入）；账号已被占用抛 :class:`AccountAlreadyExistsError`。

        ``id`` 按 ``u-<账号>`` 生成（与 :meth:`seed_demo` 同一套：账号本身唯一，id 自然唯一）。
        密码只进哈希 —— 明文到不了这一层。写库撞上 ``account`` 的唯一约束（并发下两个请求同时
        注册同一个账号）时，把数据库的 ``IntegrityError`` 翻成那个 409 的异常：调用方不必先查
        一遍再写，查了也拦不住并发。

        ``roles`` 默认 ``(USER_ROLE,)``：注册出来的是普通用户，与 :meth:`seed_demo` 那份口径
        一致（演示账号是 ``("admin", "user")``）。以前这里没写 roles，落库就是表默认的 ``[]``
        —— 账号成了「谁也不是」。
        """
        async with self._sessions() as session:
            row = UserTable(
                id=f"u-{account}",
                account=account,
                password_hash=password_hash,
                nickname=nickname,
                roles=json.dumps(list(roles), ensure_ascii=False),
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                raise AccountAlreadyExistsError() from exc
        return _to_record(row)

    # ------------------------------------------------------------------ 改资料
    async def set_nickname(self, user_id: str, nickname: str) -> UserRecord | None:
        """改昵称（个人设置那一笔），返回改完的记录；用户不存在返回 ``None``。

        只改 ``nickname`` 一列：密码将来走「改密码」、角色与停用走管理入口。
        """
        async with self._sessions() as session:
            row = await session.get(UserTable, user_id)
            if row is None:
                return None
            row.nickname = nickname
            await session.commit()
            return _to_record(row)

    # ------------------------------------------------------------------ 查询
    async def get_by_account(self, account: str) -> UserRecord | None:
        """按账号取用户；没有就返回 ``None``。"""
        async with self._sessions() as session:
            result = await session.exec(
                select(UserTable).where(UserTable.account == account)
            )
            row = result.first()
            return _to_record(row) if row is not None else None

    async def get_by_id(self, user_id: str) -> UserRecord | None:
        """按 id 取用户（令牌里带的是 id）；没有返回 ``None``。"""
        async with self._sessions() as session:
            result = await session.exec(select(UserTable).where(UserTable.id == user_id))
            row = result.first()
            return _to_record(row) if row is not None else None

    async def get_by_ids(self, user_ids: Iterable[str]) -> dict[str, UserRecord]:
        """按一批 id 取用户（**一条** ``WHERE id IN (...)``）；不在库里的 id 就不在结果里。

        列表页一次把昵称查齐，别每条一次查询（N+1）。
        """
        wanted = list(dict.fromkeys(user_ids))  # 去重（保序）：IN 里重复没意义
        if not wanted:
            return {}
        async with self._sessions() as session:
            result = await session.exec(
                select(UserTable).where(col(UserTable.id).in_(wanted))
            )
            return {row.id: _to_record(row) for row in result}

    async def list_all(self) -> list[UserRecord]:
        """列出全部用户（管理端的「归属」清单照它出）。

        按 ``account`` 排序：下拉选项的顺序要稳定，不能随插入顺序抖。
        """
        async with self._sessions() as session:
            result = await session.exec(select(UserTable).order_by(UserTable.account))
            return [_to_record(row) for row in result]
