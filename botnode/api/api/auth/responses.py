"""鉴权入口的响应体：登录成功后给客户端的那份东西，以及「登录设备」列表。

用户资料直接复用 :class:`~botnode.api.services.user.models.UserProfile`（同一个用户在外面
不管从哪个接口出去，形状应当是一致的）。
"""
from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from ...services.user.models import UserProfile


class LoginData(BaseModel):
    """登录成功给的东西：令牌 + 有效期 + 用户资料。

    令牌**同时**写进了 HttpOnly Cookie（前端读不到，所以浏览器端不用管它）；
    这里仍然回一份明文，是给非浏览器客户端（脚本、接口调试）用的。
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    token: str = Field(description="令牌明文；浏览器已由后端写进 HttpOnly Cookie，可忽略")
    token_type: Literal["bearer"] = "bearer"
    #: 还有多少秒过期；**带令牌的请求会滑动续期**，所以这个数会一次次往后延，``0`` 表示不过期
    expires_in: int = Field(description="剩余有效秒数；0 表示不过期")
    user: UserProfile
    #: 这条登录的**令牌摘要**（等价于它的 id）；「登录设备」列表里靠它认出"当前这条"。
    #: 明文在上面的 ``token`` 里，这里回的是不可逆的摘要——它才是后续吊销要用的那个值。
    token_hash: str = ""
    #: 这次登录用的设备名，前端可以提示「已在新设备登录」
    device_name: str = ""
    #: ``true`` = 复用了客户端手里那个旧令牌（``token`` 和上次一样、设备列表不会多一条）；
    #: ``false`` = 新开了一条会话
    reused: bool = False


class SessionData(BaseModel):
    """「登录设备」列表里的一行。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    token_hash: str = Field(description="令牌摘要（这条登录的 id），吊销时按它定位")
    #: 是不是「当前这条」（前端据此标出自己）
    current: bool = False
    device_name: str = ""
    #: ``mobile`` / ``desktop`` / ``unknown``
    device_type: str = ""
    browser: str = ""
    os: str = ""
    ip: str = ""
    #: 什么时候登录的（Unix 秒）
    created_at: float = 0.0
    #: 是否勾了「记住设备」（勾了滑动有效期长、Cookie 也持久）
    remembered: bool = False


class RevokeSessionData(BaseModel):
    """吊销一条登录的结果。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    #: 刚被吊销的那条登录的令牌摘要（原样回，方便前端对上列表）
    token_hash: str
    removed: bool


class RevokeAllData(BaseModel):
    """「全部下线」的结果。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    #: 一共下线了几条（**包含当前这条**）
    count: int


class ChangedPasswordData(BaseModel):
    """改密码的结果。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    #: 顺手下线了几条**其他**登录（当前这条保留：发起修改的设备不该被自己踢掉）
    revoked_sessions: int = Field(default=0, description="被下线的其他登录条数")
