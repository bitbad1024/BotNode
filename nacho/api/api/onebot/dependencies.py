"""OneBot 管理路由的注入件（``Depends`` 的那些东西）。

单独一份是为了让 ``router.py`` 只剩「接口长什么样」：怎么拿到服务、怎么要求登录，
堆在路由里会盖住接口本身。

**登录校验直接复用鉴权那份**（:data:`nacho.api.api.auth.dependencies.CurrentUserDep`）：
OneBot 管理接口用的就是登录令牌，没有第二套凭据。跨业务的那份（取请求编号）在
:mod:`nacho.api.common.dependencies`。
"""
from __future__ import annotations

from typing import Annotated, Protocol, cast

from fastapi import Depends, Request, status

from ...common.errors import ApiError, ErrorCode
from ..auth.dependencies import CurrentUserDep
from .protocols import OneBotLike


class _AppState(Protocol):
    """挂在 ``app.state`` 上的东西（由 :func:`nacho.api.create_app` 写入）。"""

    onebot_server: OneBotLike | None


class _App(Protocol):
    """FastAPI 的 ``app``，这里只关心它身上的 ``state``。"""

    state: _AppState


def get_onebot(request: Request) -> OneBotLike:
    """取 OneBot 服务：装配时挂在 ``app.state.onebot_server`` 上（见 :func:`create_app`）。

    主程序没传服务（比如只起接口层、没起 OneBot）时回 **503 并说清楚**，而不是让
    ``None`` 一路滑到 500 —— 「没接入」和「出错了」对排障是两回事。

    ``app.state`` 是运行时挂上去的，类型检查看不到，所以把 ``request.app`` 先 ``cast`` 成
    带 ``state.onebot_server`` 的形状，避免整条链都是 ``Any``。
    """
    app = cast("_App", request.app)
    server = app.state.onebot_server
    if server is None:
        raise ApiError(
            ErrorCode.HTTP_ERROR,
            "OneBot 未接入：主程序没有把服务传给 create_app",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return server


#: 依赖简写：路由函数里写 ``server: OneBotDep`` 即可
OneBotDep = Annotated[OneBotLike, Depends(get_onebot)]

__all__ = ["CurrentUserDep", "OneBotDep", "get_onebot"]
