"""鉴权业务：拿账号密码换一组令牌，拿令牌换回"当前是谁"。

    models.py       业务层的输入 / 输出：Credentials、LoginResult、CurrentUser
    service.py      业务编排：AuthService（查人 -> 比密码 -> 查停用 -> 开会话）

令牌**不是 JWT、也不能自证**：访问令牌是随机串，绑在缓存里（见
:mod:`nacho.api.services.session`）；勾了「记住设备」再发一个长期令牌做自动登录。
令牌与会话的机制都在 :mod:`nacho.api.services.session`，本模块只用它。

「用户从哪来」也不在本模块：查人与验密码是 :mod:`nacho.api.services.user` 的事，本模块
依赖它 —— 反过来不行（用户不认识令牌）。

本模块不认识 FastAPI：失败时抛 :class:`~nacho.api.common.errors.ApiError`，状态码已经
挂在异常上；要换协议（比如做成消息入口）这份逻辑能直接用。HTTP 入口在
:mod:`nacho.api.api.auth`。
"""
from __future__ import annotations

from .models import Credentials, CurrentUser, LoginResult
from .service import AuthService

__all__ = [
    "AuthService",
    "Credentials",
    "CurrentUser",
    "LoginResult",
]
