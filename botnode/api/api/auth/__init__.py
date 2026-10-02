"""鉴权入口层：注册、登录与「当前用户」三个 HTTP 接口。

    requests.py     进来的请求体：RegisterRequest（注册）、LoginRequest（登录）
    responses.py    出去的响应体：LoginData（令牌 + 有效期 + 用户资料）；注册直接回 UserProfile
    dependencies.py 路由的注入件：怎么拿到业务层的服务、令牌从哪个头来
    router.py       HTTP 入口：``POST <prefix>/auth/register``、``POST <prefix>/auth/login``、
                    ``GET <prefix>/auth/me``

这里只负责「对外长什么样」，并把请求翻译成业务层的输入；真正的编排（开账号 / 查人 -> 比密码 ->
查停用 -> 签令牌）在 :class:`botnode.api.services.auth.AuthService`。

安全上的几条硬规矩（由业务层保证，这里只回执）：

* 登录失败按**同一个** 401 回：账号不存在与密码错分开说等于免费告诉对方哪些账号存在；
  停用是 403（身份没错，只是不许进）；**注册正好相反** —— 必须明说「账号已被注册」（409），
  否则前端没法提示换个账号；
* 密码只在请求体里以 ``SecretStr`` 出现一次，进了业务层就立刻被用完，不进日志、不进响应。
"""
from __future__ import annotations

from .dependencies import AuthServiceDep, BearerDep, bearer_scheme, get_auth_service
from .requests import LoginRequest, RegisterRequest
from .responses import LoginData
from .router import router

__all__ = [
    "LoginRequest",
    "RegisterRequest",
    "LoginData",
    "router",
    "get_auth_service",
    "bearer_scheme",
    "AuthServiceDep",
    "BearerDep",
]
