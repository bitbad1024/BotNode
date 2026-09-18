"""OneBot 管理接口的请求体。"""
from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class IssueTokenRequest(BaseModel):
    """签发一个令牌：给哪个账号、备注是什么。"""

    account: str = Field(
        min_length=1,
        max_length=64,
        description="这个令牌属于哪个账号（客户端用这个令牌连进来，就归到该账号下）",
    )
    remark: str = Field(default="", max_length=255, description="备注（给人看：给哪个机器人的）")

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={"example": {"account": "alice", "remark": "主号"}},
        frozen=True,
    )


class SetTokenEnabledRequest(BaseModel):
    """启用 / 停用一条令牌（记录还在，随时能启用回来；和"吊销"不同）。"""

    enabled: bool = Field(
        description="true 启用；false 停用——不许再连，并把正用它连着的客户端断开"
    )

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={"example": {"enabled": False}},
        frozen=True,
    )
