"""机器人管理接口的请求体（泛化版：签发加 ``platform`` 字段）。"""
from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AddBotRequest(BaseModel):
    """添加一个机器人：选底层适配器 + 账号 + 备注 + （Kook）Bot Token。

    ``platform`` 选底层适配器（onebot / kook）；OneBot 的令牌随机签发、用户不填，
    Kook 是正向 WS、用用户自填的 Bot Token 鉴权，所以 ``platform=kook`` 时 ``token``
    必填。归属（谁的）不在这里：就是当前登录用户。
    """

    platform: str = Field(
        default="onebot",
        description="底层适配器（onebot / kook）",
        pattern="^(onebot|kook)$",
    )
    account: str = Field(
        default="",
        max_length=64,
        description="机器人账号（OneBot 是接入 WS 的机器人号，Kook 是 Bot 名；展示用）",
    )
    remark: str = Field(default="", max_length=255, description="备注（给人看）")
    token: str = Field(
        default="",
        max_length=255,
        description="Kook 的 Bot Token（platform=kook 时必填；OneBot 自动签发，不用填）",
    )

    @model_validator(mode="after")
    def _require_token_for_kook(self) -> AddBotRequest:
        """Kook 正向 WS 靠 Bot Token 鉴权，没填没法连——在入口层就挡掉（422）。"""
        if self.platform == "kook" and not self.token.strip():
            raise ValueError("platform=kook 时必须填 Bot Token")
        return self

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={"example": {"platform": "onebot", "account": "机器人一号", "remark": "主号"}},
        frozen=True,
    )


class SetBotEnabledRequest(BaseModel):
    """启用 / 停用一个机器人（记录还在，随时能启用回来）。"""

    enabled: bool = Field(description="true 启用；false 停用")

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={"example": {"enabled": False}},
        frozen=True,
    )
