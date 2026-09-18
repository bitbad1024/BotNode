"""OneBot 管理路由的注入件（``Depends`` 的那些东西）。

单独一份是为了让 ``router.py`` 只剩「接口长什么样」：怎么拿到服务、怎么要求登录，
堆在路由里会盖住接口本身。

跨业务的那份（取请求编号）在 :mod:`nacho.api.common.dependencies`。
"""
from __future__ import annotations

from typing import Annotated, Protocol, cast

from fastapi import Depends, Request, status

from ...common.dependencies import trace_id_of
from ...common.errors import ApiError, ErrorCode, UnauthorizedError
from ...services.user.models import UserProfile
from ..auth.dependencies import AuthServiceDep, BearerDep
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


async def _current_user(
    request: Request,
    service: AuthServiceDep,
    credentials: BearerDep,
) -> UserProfile:
    """要求登录：没带令牌 401；令牌无效 / 账号不可用由 :class:`AuthService` 抛 401。

    管理接口都要过这一道（签发令牌、踢人都是敏感操作），登录令牌就是登录接口给的那个。
    """
    if credentials is None:
        raise UnauthorizedError("缺少令牌")
    return await service.current_user(credentials.credentials, trace_id=trace_id_of(request))


#: 依赖简写：挂在路由参数上即表示「这个接口要登录」
CurrentUserDep = Annotated[UserProfile, Depends(_current_user)]
