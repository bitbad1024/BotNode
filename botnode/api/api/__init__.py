"""接口层的 HTTP 入口层：只负责「对外怎么说」。

这里放的是**认识 FastAPI 的那一半**：路由、依赖注入、请求 / 响应 schema。
业务怎么算（查人、验密码、签令牌）在 :mod:`botnode.api.services` —— 依赖方向是
**入口层 -> 业务层**，业务层不认识 FastAPI，也不 import 本包。

    auth/     鉴权入口：``POST <prefix>/auth/login``、``GET <prefix>/auth/me``
    profile/  个人设置入口：``<prefix>/profile``（改昵称 / 头像）
    onebot/   OneBot 管理入口：``<prefix>/onebot/clients``、``<prefix>/onebot/tokens``（兼容面）
    bots/     机器人管理入口：``<prefix>/bots``（跨平台增 / 启停 / 删）
    log/      运行日志入口：``GET <prefix>/logs``
    workflow/ 工作流入口：``<prefix>/workflows``（定义 / 版本 / 发布 / 入库前校验）

加一个接口就动这一个目录；跨业务的通用件（响应壳、错误出口、中间件、日志）在
:mod:`botnode.api.common`。
"""
from __future__ import annotations

from .auth.router import router as auth_router
from .bots.router import router as bots_router
from .log.router import router as log_router
from .onebot.router import router as onebot_router
from .profile.router import router as profile_router
from .workflow import router as workflow_router

__all__ = [
    "auth_router",
    "bots_router",
    "log_router",
    "onebot_router",
    "profile_router",
    "workflow_router",
]
