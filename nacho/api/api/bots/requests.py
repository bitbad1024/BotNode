"""机器人管理接口的请求体（泛化版：签发加 ``platform`` 字段）。"""
from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class AddBotRequest(BaseModel):
    """添加一个机器人：选底层适配器 + 账号 + 备注。

    ``platform`` 选底层适配器（onebot / kook）；P5 过渡期只有 onebot 真正可签发，
    kook 留 P6（Bot Token 接入）。归属（谁的）不在这里：就是当前登录用户。
    """

    platform: str = Field(
        default="onebot",
        description="底层适配器（onebot / kook）",
        pattern="^(onebot|kook)$",
    )
    account: str = Field(
        default="",
        max_length=64,
        description="机器人账号（OneBot 是接入 WS 的机器人号；展示用）",
    )
    remark: str = Field(default="", max_length=255, description="备注（给人看）")

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
