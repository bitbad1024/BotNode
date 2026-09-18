"""OneBot 管理接口的响应体。

明文令牌只在 :class:`IssuedTokenData` 里出现一次——之后查不回来（库里只有摘要），
客户端得自己存好。
"""
from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class ClientData(BaseModel):
    """在线客户端列表里的一行（快照）。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    #: 连接编号：``DELETE /onebot/clients/{id}`` 用这个踢人
    id: str
    #: 属于哪个账号（握手时由令牌定下来）
    account: str = Field(description="归属账号")
    #: 机器人号；还没收到事件时是 ``null``
    self_id: int | None = None
    #: 对端地址
    remote: str = ""
    #: 连上的时刻（Unix 秒）
    connected_at: float = 0.0


class TokenData(BaseModel):
    """一条令牌记录（**不含明文**）。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    #: 记录 id：``DELETE /onebot/tokens/{id}`` 用这个吊销
    id: str
    account: str = Field(description="这个令牌属于哪个账号")
    enabled: bool = True
    remark: str = ""
    created_at: float = 0.0


class IssuedTokenData(BaseModel):
    """签发令牌的结果。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    record: TokenData
    #: 明文令牌：**只在这一次响应里出现**，之后查不回来
    token: str = Field(description="明文令牌，请立刻保存，之后无法再查")


class KickData(BaseModel):
    """踢掉一个客户端的结果。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    client_id: str
    account: str
    #: 令牌是不是也一起吊销了（没有吊销的话，客户端会自动重连）
    revoked: bool


class RevokeData(BaseModel):
    """吊销令牌的结果。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    token_id: str
    removed: bool
