"""用户的两种形状：存的样子（内部）与露的样子（对外）。

为什么要分两个::

    UserRecord   存储层交回来的样子（**带 password_hash**，只在内部流转）
    UserProfile  出门的样子（**没有密码字段**）

直接把记录丢出去意味着「谁往模型上加个字段，它就自动对外可见」。备一层 :func:`profile_of`
之后，加字段得先想一下要不要露。

``UserRecord`` 里存的是**哈希**串（``算法$迭代$盐$摘要``），明文不进这个模型、更不会出现
在响应里；账号与密码的规则见 :mod:`botnode.api.services.user.validation`。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class UserRecord:
    """一个用户：接口层用到的全部字段（存储层交回来的样子）。"""

    id: str
    account: str
    #: 密码哈希（``算法$迭代$盐$摘要`` 之类的串），**不是明文**
    password_hash: str
    nickname: str = ""
    roles: tuple[str, ...] = ()
    #: 停用：账号还在，但不许登录
    disabled: bool = False


class UserProfile(BaseModel):
    """对外可见的用户资料（**没有密码字段**）。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    id: str
    account: str
    nickname: str = ""
    roles: list[str] = Field(default_factory=list)


def profile_of(user: UserRecord) -> UserProfile:
    """把存储层的用户对象转成对外资料（**不含密码哈希**）。

    入参是 :class:`UserRecord`（存储层交回来的样子，``frozen`` 只读）；数据库层若用自己的
    用户类，只要带上 ``id`` / ``account`` / ``nickname`` / ``roles`` 这些字段，或在调用前先转成
    ``UserRecord`` 即可。
    """
    return UserProfile(
        id=str(user.id),
        account=str(user.account),
        nickname=str(user.nickname),
        roles=[str(role) for role in user.roles],
    )
