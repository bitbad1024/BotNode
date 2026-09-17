"""接口层的业务层：只算「业务怎么办」，**不认识 FastAPI**。

一个模块一件事，模块自带全套（协议 + 默认实现 + 编排）::

    user/   用户：用户长什么样、账号密码规则、人到哪查、密码怎么落库 + 默认实现
    auth/   鉴权：令牌怎么签与验、把「凭据换令牌」串起来的服务

依赖方向是**单向**的：``auth`` 用 ``user``（登录得先查到人），``user`` 不认识 ``auth``。
所以「用户怎么存 / 密码怎么算」定在 user 里，auth 只管**凭据怎么换成令牌**。

业务层不 import :mod:`nacho.api.api`（入口层）—— 依赖只能从入口层指向这里，再由
:func:`nacho.api.create_app` 把实现装配起来。跨业务的（响应壳、错误出口、中间件、日志）
一律去 :mod:`nacho.api.common`。
"""
from __future__ import annotations

from .auth import AuthService, HmacTokenService, TokenClaims, TokenService, resolve_secret
from .auth.models import Credentials, LoginResult

__all__ = [
    "AuthService",
    "Credentials",
    "LoginResult",
    "TokenClaims",
    "TokenService",
    "HmacTokenService",
    "resolve_secret",
]
