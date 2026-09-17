"""鉴权业务：拿账号密码换一个令牌，拿令牌换回当前是谁。

    models.py       业务层的输入 / 输出：Credentials（凭据）、LoginResult（签发结果）
    protocols.py    要向外要的能力：TokenService（含令牌里的东西 TokenClaims）
    security.py     默认实现：HMAC 签名的不透明令牌 + 密钥怎么定
    service.py      业务编排：AuthService（查人 -> 比密码 -> 查停用 -> 签令牌）

「用户从哪来」不在本模块：查人与验密码是 :mod:`nacho.api.services.user` 的事，本模块
依赖它 —— 反过来不行（用户不认识令牌）。

本模块不认识 FastAPI：失败时抛 :class:`~nacho.api.common.errors.ApiError`，状态码已经
挂在异常上；要换协议（比如做成消息入口）这份逻辑能直接用。HTTP 入口在
:mod:`nacho.api.api.auth`。
"""
from __future__ import annotations

from .models import Credentials, LoginResult
from .protocols import TokenClaims, TokenService
from .security import HmacTokenService, resolve_secret
from .service import AuthService

__all__ = [
    "AuthService",
    "Credentials",
    "LoginResult",
    "TokenClaims",
    "TokenService",
    "HmacTokenService",
    "resolve_secret",
]
