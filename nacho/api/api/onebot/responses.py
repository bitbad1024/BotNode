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

    #: 这条连接自己的编号：``DELETE /onebot/clients/{client_id}`` 用这个踢人
    client_id: str
    #: 这个机器人那一行的主键（bot_id）：多实例后与归属 owner_id 分开
    id: str = Field(description="机器人主键（bot_id）")
    #: 归属用户（谁的）：查昵称 / 判范围用它
    owner_id: str = Field(default="", description="归属用户（谁的）")
    #: 归属那个 ``owner_id`` 在用户表里的昵称（查不到就空串）
    nickname: str = Field(default="", description="归属的昵称（按 owner_id 去用户表查）")
    #: 接入 WS 的那个 OneBot 机器人账号
    account: str = Field(default="", description="OneBot 机器人账号")
    #: 机器人号；还没收到事件时是 ``null``
    self_id: int | None = None
    #: 对端地址
    remote: str = ""
    #: 连上的时刻（Unix 秒）
    connected_at: float = 0.0


class TokenData(BaseModel):
    """一条机器人凭证记录（**不含明文**）。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    #: 记录主键（bot_id）：``DELETE /onebot/tokens/{id}`` 用这个吊销
    id: str
    #: 底层适配器：onebot / kook
    platform: str = Field(default="onebot", description="底层适配器（onebot / kook）")
    #: 归属用户（谁的）
    owner_id: str = Field(default="", description="归属用户（谁的）")
    #: 接入 WS 的那个 OneBot 机器人账号
    account: str = Field(default="", description="OneBot 机器人账号")
    enabled: bool = True
    remark: str = ""
    created_at: float = 0.0
    #: 归属那个 ``owner_id`` 在用户表里的昵称（查不到就空串）
    nickname: str = Field(default="", description="归属的昵称（按 owner_id 去用户表查）")
    #: **派生态**：此刻有没有正用它连着的在线客户端。不落库、不持久，
    #: 是接口层在组装响应时从服务端在线列表聚合出来的实时快照。
    online: bool = Field(default=False, description="此刻有没有用它连着的在线客户端")
    #: **派生态**：正用它连着的在线客户端快照（``online`` 为真时的详情）。
    #: 一个机器人可能同时挂着多条连接，这里逐条列出来。
    clients: list[ClientData] = Field(
        default_factory=list, description="正用它连着的在线客户端（快照）"
    )


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
    #: 这条连接属于谁
    id: str
    #: 令牌是不是也一起吊销了（没有吊销的话，客户端会自动重连）
    revoked: bool


class RevokeData(BaseModel):
    """吊销令牌的结果。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    token_id: str
    removed: bool
