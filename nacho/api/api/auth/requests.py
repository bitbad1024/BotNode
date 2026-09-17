"""鉴权入口的请求体。

字段用 :mod:`nacho.api.services.user.validation` 里收口的那些类型别名：字符集、长度这些
规则集中在用户那一侧（同一套规则也给将来的注册 / 改资料用），这里只声明「这个字段用哪
条规则」。
"""
from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ...services.user.validation import Account, Password


class LoginRequest(BaseModel):
    """登录请求：账号 + 密码。

    密码类型是 ``SecretStr``：打印、日志、异常里都只显示 ``**********``，不落明文。
    """

    account: Account = Field(description="登录账号（3-32 位字母、数字、下划线、点、短横线）")
    password: Password = Field(description="登录密码（8-128 位）")

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={"example": {"account": "admin", "password": "nacho-admin"}},
        frozen=True,
    )
