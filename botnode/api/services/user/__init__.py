"""用户模块：一个用户长什么样、密码怎么存、人到哪查。

这一块**不管 HTTP**：本模块目前没有自己的接口（``/auth/me`` 算鉴权那一侧 —— 它是拿令牌
换身份），将来要加 ``/users`` 之类的查询，路由就落在入口层 :mod:`botnode.api.api`。它对外
提供的是::

    models.py      UserRecord（内部形状）/ UserProfile（对外资料）/ profile_of（两者转换）
    validation.py  账号与密码的规则（登录、将来注册 / 改资料共用一份）
    protocols.py   两块能力：人到哪查（UserStore）、密码怎么算（PasswordHasher）
    store_sql.py   落库实现 SqlUserStore（SQLModel + AsyncSession，不手写 SQL）
    security.py    默认实现：PBKDF2 密码哈希
    demo.py        演示账号的单一来源（SqlUserStore.seed_demo 用）

兩件事的边界在这::

    UserRecord   存储层交回来的样子（**带密码哈希**，只在内部流转）
    UserProfile  出门的样子（**没有密码字段**）

中间那步 :func:`profile_of` 是显式的：谁往记录上加个字段，不会自动对外可见，得先来这
里决定要不要露。

鉴权那一侧（``botnode.api.services.auth``）依赖本模块；反过来不行 —— 用户不知道令牌是什么。
"""
from __future__ import annotations

from .models import UserProfile, UserRecord, profile_of
from .protocols import PasswordHasher, UserStore
from .security import Pbkdf2PasswordHasher
from .store_sql import SqlUserStore
from .validation import (
    ACCOUNT_MAX_LENGTH,
    ACCOUNT_MIN_LENGTH,
    NICKNAME_MAX_LENGTH,
    NICKNAME_MIN_LENGTH,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    Account,
    Nickname,
    Password,
)

__all__ = [
    "UserRecord",
    "UserProfile",
    "profile_of",
    "UserStore",
    "PasswordHasher",
    "SqlUserStore",
    "Pbkdf2PasswordHasher",
    "Account",
    "Password",
    "Nickname",
    "ACCOUNT_MIN_LENGTH",
    "ACCOUNT_MAX_LENGTH",
    "PASSWORD_MIN_LENGTH",
    "PASSWORD_MAX_LENGTH",
    "NICKNAME_MIN_LENGTH",
    "NICKNAME_MAX_LENGTH",
]
