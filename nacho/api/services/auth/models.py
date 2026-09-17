"""鉴权业务层的输入 / 输出对象（**不认识 HTTP**）。

入口层把请求体翻译成 :class:`Credentials` 再交给
:class:`~nacho.api.services.auth.service.AuthService`；服务返回 :class:`LoginResult`，
入口层再装配成对外的响应模型 —— 这样业务层不会反过来依赖 HTTP schema。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..user.models import UserProfile


@dataclass(frozen=True)
class Credentials:
    """登录凭据（业务层的输入）。

    ``password`` 是**明文**，只在这一次调用里短暂存在；``repr`` 里被隐去，避免误打印。
    """

    account: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class LoginResult:
    """登录成功的结果：令牌 + 有效期 + 用户资料。"""

    token: str
    expires_in: int
    user: UserProfile
