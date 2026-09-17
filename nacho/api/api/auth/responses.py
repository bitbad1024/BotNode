"""鉴权入口的响应体：登录成功后给客户端的那份东西。

只有令牌与它自己的元信息，用户资料直接复用
:class:`~nacho.api.services.user.models.UserProfile`（同一个用户在外面不管从哪个接口出去，
形状应当是一致的）。
"""
from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from ...services.user.models import UserProfile


class LoginData(BaseModel):
    """登录成功给的东西：令牌 + 有效期 + 用户资料。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    token: str = Field(description="登录令牌，后续请求带在 Authorization 头上")
    token_type: Literal["bearer"] = "bearer"
    #: 还有多少秒过期（客户端拿它决定什么时候重新登录）
    expires_in: int = Field(description="令牌剩余有效秒数")
    user: UserProfile
