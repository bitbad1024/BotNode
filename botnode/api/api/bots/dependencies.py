"""机器人管理路由的注入件（``Depends`` 的那些东西）。

单独一份是为了让 ``router.py`` 只剩「接口长什么样」：怎么拿到机器人管理服务，
堆在路由里会盖住接口本身。

**登录校验 / 身份判断直接复用 OneBot 管理那份**（:data:`botnode.api.api.auth.dependencies.CurrentUserDep`
与 :func:`botnode.api.api.onebot.dependencies.is_admin` / :func:`may_touch`）：登录令牌只有一套，
「管理员不限、其余人只限自己」的口径也是跨平台同一套。这里只补机器人管理**自己**那份：
从 ``app.state.bots_service`` 取服务（装配层注入的实现见 :class:`botnode.platforms.bridge.manager.BotManager`）。
"""
from __future__ import annotations

from typing import Annotated, Protocol, cast

from fastapi import Depends, Request, status

from ...common.errors import ApiError, ErrorCode
from .protocols import BotsService


class _AppState(Protocol):
    """挂在 ``app.state`` 上的东西（由 :func:`botnode.api.create_app` 写入）。"""

    bots_service: BotsService | None


class _App(Protocol):
    """FastAPI 的 ``app``，这里只关心它身上的 ``state``。"""

    state: _AppState


def get_bots(request: Request) -> BotsService:
    """取机器人管理服务：装配时挂在 ``app.state.bots_service`` 上（见 :func:`create_app`）。

    主程序没传服务（比如只起接口层、没装配 bridge）时回 **503 并说清楚**，而不是让
    ``None`` 一路滑到 500 —— 「没接入」和「出错了」对排障是两回事。
    """
    app = cast("_App", request.app)
    service = app.state.bots_service
    if service is None:
        raise ApiError(
            ErrorCode.HTTP_ERROR,
            "机器人管理未接入：主程序没有把服务传给 create_app",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return service


#: 依赖简写：路由函数里写 ``service: BotsDep`` 即可
BotsDep = Annotated[BotsService, Depends(get_bots)]


__all__ = ["BotsDep", "get_bots"]