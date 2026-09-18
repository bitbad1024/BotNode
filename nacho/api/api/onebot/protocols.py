"""接口层需要的那点 OneBot 管理能力（**结构化协议**，不 import ``nacho.onebot``）。

为什么要绕一层协议：``nacho.onebot`` 依赖 ``websockets``，而 ``nacho.api`` 是独立的
可选依赖（``pip install "nacho[api]"`` 就能跑登录）。让接口层直接 import 它，等于装上
api 就必须装 onebot——所以这里只声明「接口层要用到哪些方法」，由
:class:`nacho.onebot.OneBotServer` / :class:`nacho.onebot.TokenRegistry` **结构化满足**，
两边不用互相 import（同一套路子见 :mod:`nacho.api.services.user.protocols`）。

数据那几个（:class:`ClientLike` / :class:`TokenLike`）的成员一律写成 **只读属性**
（``@property``）：对面是冻结数据类，字段不可写；协议里若声明成可写属性，
冻结数据类反而对不上（协议要求能赋值，它做不到）。

装配时主程序把服务挂到 ``app.state.onebot_server`` 上，路由按这里的协议取用。
"""
from __future__ import annotations

from typing import Protocol


class ClientLike(Protocol):
    """在线列表里的一行（对应 :class:`nacho.onebot.ClientEntry`）。"""

    @property
    def id(self) -> str:
        """连接编号（踢人时按它定位）。"""
        ...

    @property
    def account(self) -> str:
        """归属账号（握手时由令牌定下来）。"""
        ...

    @property
    def self_id(self) -> int | None:
        """机器人号；还没收到事件时是 ``None``。"""
        ...

    @property
    def remote(self) -> str:
        """对端地址。"""
        ...

    @property
    def connected_at(self) -> float:
        """连上的时刻（Unix 秒）。"""
        ...


class TokenLike(Protocol):
    """一条令牌记录（对应 :class:`nacho.onebot.TokenRecord`，**不含明文**）。"""

    @property
    def id(self) -> str:
        """记录 id（吊销时按它定位）。"""
        ...

    @property
    def account(self) -> str:
        """这个令牌属于哪个账号。"""
        ...

    @property
    def enabled(self) -> bool:
        """停用：记录还在，但不许再连。"""
        ...

    @property
    def remark(self) -> str:
        """备注。"""
        ...

    @property
    def created_at(self) -> float:
        """签发时间（Unix 秒）。"""
        ...


class IssuedLike(Protocol):
    """签发结果（对应 :class:`nacho.onebot.IssuedToken`）。"""

    @property
    def record(self) -> TokenLike:
        """令牌记录（不含明文）。"""
        ...

    @property
    def token(self) -> str:
        """明文令牌：只在这一次出现，之后查不回来。"""
        ...


class TokenRegistryLike(Protocol):
    """令牌注册表里接口层用得到的三件事（列 / 签 / 吊销）。"""

    async def items(self) -> tuple[TokenLike, ...]:
        """全部令牌（不含明文）。"""
        ...

    async def issue(self, account: str, *, remark: str = "") -> IssuedLike:
        """给 ``account`` 签一个新令牌。"""
        ...

    async def remove_by_id(self, token_id: str) -> bool:
        """按记录 id 吊销；真删掉了返回 ``True``。"""
        ...


class OneBotLike(Protocol):
    """接口层要用的 OneBot 服务端（对应 :class:`nacho.onebot.OneBotServer`）。"""

    def roster(self, *, account: str | None = None) -> tuple[ClientLike, ...]:
        """在线客户端列表（快照）；给 ``account`` 就只看那一个账号下的。"""
        ...

    async def kick(self, client_id: str, *, revoke: bool = False) -> bool:
        """踢掉一个客户端；``revoke=True`` 连令牌一起吊销。"""
        ...

    async def revoke_by_id(self, token_id: str) -> bool:
        """吊销一个令牌（按记录 id），并把正用它连着的客户端断开。"""
        ...

    async def set_token_enabled(self, token_id: str, enabled: bool) -> bool:
        """启用 / 停用一条令牌（记录还在）；停用会连同断开正用它连着的客户端。"""
        ...

    @property
    def tokens(self) -> TokenRegistryLike | None:
        """令牌注册表；``None`` 表示没配（不校验，也没有令牌可管）。"""
        ...
