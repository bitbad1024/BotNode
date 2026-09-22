"""接口层的 HTTP 入口层：只负责「对外怎么说」。

这里放的是**认识 FastAPI 的那一半**：路由、依赖注入、请求 / 响应 schema。
业务怎么算（查人、验密码、签令牌）在 :mod:`nacho.api.services` —— 依赖方向是
**入口层 -> 业务层**，业务层不认识 FastAPI，也不 import 本包。

    auth/     鉴权入口：``POST <prefix>/auth/login``、``GET <prefix>/auth/me``
    onebot/   OneBot 管理入口：``<prefix>/onebot/clients``、``<prefix>/onebot/tokens``
    log/      运行日志入口：``GET <prefix>/logs``

加一个接口就动这一个目录；跨业务的通用件（响应壳、错误出口、中间件、日志）在
:mod:`nacho.api.common`。
"""
from __future__ import annotations

from .auth.router import router as auth_router
from .log.router import router as log_router
from .onebot.router import router as onebot_router

__all__ = ["auth_router", "log_router", "onebot_router"]
