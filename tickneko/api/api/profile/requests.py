"""个人设置的请求体：目前只有「改昵称」（头像走字节流，不套 JSON）。"""
from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ...services.user.validation import Nickname


class UpdateProfileRequest(BaseModel):
    """改个人资料：昵称。

    昵称规则复用 :data:`~tickneko.api.services.user.validation.Nickname`（去两端空白、1-32 个字符），
    与注册时那个是同一套 —— 两个入口对「什么算合法昵称」只能有一个说法。
    """

    nickname: Nickname = Field(description="昵称（1-32 个字符，展示用）")

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={"example": {"nickname": "TickNeko"}},
        frozen=True,
    )
