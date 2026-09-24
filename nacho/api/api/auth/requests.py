"""鉴权入口的请求体。

字段用 :mod:`nacho.api.services.user.validation` 里收口的那些类型别名：字符集、长度这些
规则集中在用户那一侧（同一套规则也给将来的注册 / 改资料用），这里只声明「这个字段用哪
条规则」。
"""
from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ...services.user.validation import Account, Nickname, Password


class LoginRequest(BaseModel):
    """登录请求：账号 + 密码（+ 要不要记住这台设备）。

    密码类型是 ``SecretStr``：打印、日志、异常里都只显示 ``**********``，不落明文。
    """

    account: Account = Field(description="登录账号（3-32 位字母、数字、下划线、点、短横线）")
    password: Password = Field(description="登录密码（8-128 位）")
    remember: bool = Field(
        default=False,
        description=(
            "记住这台设备：勾了会话滑动有效期更长（默认 30 天）且 Cookie 是持久 Cookie；"
            "不勾则有效期短（默认 2 小时）且 Cookie 是会话 Cookie（关浏览器即丢）"
        ),
    )
    previous_token: str = Field(
        default="",
        description=(
            "上一次登录拿到的令牌（可选）。认得出、且属于同一个账号时**复用它**：延长有效期、"
            "不新建会话（设备列表里也就不会多出一条），这个值原样作为本次的令牌发回。"
            "浏览器**不用传**——旧令牌在 HttpOnly Cookie 里，JS 读不到，后端会自己取。"
        ),
    )

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {"account": "admin", "password": "nacho-admin", "remember": True}
        },
        frozen=True,
    )


class RegisterRequest(BaseModel):
    """注册请求：账号 + 密码 + 昵称。

    规则同样来自 :mod:`nacho.api.services.user.validation`（账号 3-32 位字符集、密码 8-128 位、
    昵称 1-32 个字符），这里只声明「这个字段用哪条规则」。密码是 ``SecretStr``，不进日志。
    """

    account: Account = Field(description="登录账号（3-32 位字母、数字、下划线、点、短横线）")
    password: Password = Field(description="登录密码（8-128 位）")
    nickname: Nickname = Field(description="昵称（1-32 个字符，展示用）")

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {"account": "nacho", "password": "nacho-1234", "nickname": "Nacho"}
        },
        frozen=True,
    )
