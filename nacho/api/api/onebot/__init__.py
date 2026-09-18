"""OneBot 管理入口：把「在线客户端列表」与「令牌管理」做成 HTTP 接口。

    protocols.py    接口层需要的 OneBot 能力（**结构化协议**，不 import nacho.onebot）
    dependencies.py 路由的注入件：怎么拿到 OneBot 服务、怎么要求登录
    requests.py     进来的请求体：IssueTokenRequest
    responses.py    出去的响应体：ClientData / TokenData / IssuedTokenData / KickData / RevokeData
    router.py       HTTP 入口：``<prefix>/onebot/clients`` 与 ``<prefix>/onebot/tokens``

这里只负责「对外长什么样」，并把请求翻译成对 OneBot 服务的一次调用；服务本身（WS 服务端、
令牌存哪）在 :mod:`nacho.onebot`，由主程序装配时传进 :func:`nacho.api.create_app`。

安全上的两条规矩：

* 这些接口都要登录（``Authorization: Bearer``，就是登录接口给的那个令牌），没登录一律 401；
* 令牌**明文只在签发那一次响应里出现**，列表 / 查询一律只给记录（库里存的本来也只有摘要）。
"""
from __future__ import annotations

from .requests import IssueTokenRequest, SetTokenEnabledRequest
from .responses import (
    ClientData,
    IssuedTokenData,
    KickData,
    RevokeData,
    TokenData,
)
from .router import router

__all__ = [
    "router",
    "IssueTokenRequest",
    "SetTokenEnabledRequest",
    "ClientData",
    "TokenData",
    "IssuedTokenData",
    "KickData",
    "RevokeData",
]
