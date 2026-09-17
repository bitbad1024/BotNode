"""鉴权模块要向外要的那块能力：令牌怎么签、怎么验（协议）。

这里只声明「需要什么」，**不实现**：默认实现在
:mod:`nacho.api.services.auth.security`（HMAC 令牌），要换成别的形式（如 JWT / 带刷新令牌
的那套）只要方法签名一致，从 :func:`nacho.api.create_app` 传进来即可。

:class:`TokenClaims` 放在协议这一侧（而不是实现里）：所有 :class:`TokenService` 的实现
**要能产出同一个形状**，服务层才不用管背后用的是什么。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class TokenClaims:
    """一个令牌里装的东西（解析成功才拿得到）。"""

    #: 令牌的主人（用户 id）
    subject: str
    issued_at: float
    expires_at: float
    #: 令牌编号，方便日志里追某一次登录
    token_id: str = ""

    @property
    def expires_in(self) -> int:
        """还有多少秒过期（已过期就是 0）。"""
        return max(0, int(self.expires_at - time.time()))

    def is_expired(self, *, now: float | None = None) -> bool:
        """是否过期；``now`` 可以传进来便于测试。"""
        return (time.time() if now is None else now) >= self.expires_at


@runtime_checkable
class TokenService(Protocol):
    """令牌的签发与解析。

    ``parse`` 负责把关：过期、签名不对、格式不是我们发的，都抛
    :class:`~nacho.api.common.errors.ApiError`（401），调用方拿到的一定是合法令牌。
    """

    def issue(self, subject: str, *, ttl: float | None = None) -> str:
        """给 ``subject``（用户 id）签一个令牌；``ttl`` 为空时用签发器默认的。"""
        ...

    def parse(self, token: str) -> TokenClaims:
        """解析并校验令牌；过期 / 被改过 / 格式不对都抛 401 的
        :class:`~nacho.api.common.errors.ApiError`。"""
        ...
