"""鉴权路由的注入件（``Depends`` 的那些东西）。

单独一份是为了让 ``router.py`` 只剩「接口长什么样」：怎么拿到业务层的服务、令牌从哪个
头来，堆在路由里会盖住接口本身。

跨业务的那份（取请求编号）在 :mod:`nacho.api.common.dependencies`。
"""
from __future__ import annotations

from typing import Annotated, Protocol, cast

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ...services.auth.service import AuthService

#: 令牌来源：``Authorization: Bearer <token>``；
#: ``auto_error=False`` 是不让框架直接回它自己的错误格式，交给我们自己抛统一的 401
bearer_scheme = HTTPBearer(
    auto_error=False,
    description="登录接口返回的 token，按 Bearer 方式带在 Authorization 头上",
)


class _AppState(Protocol):
    """挂在 ``app.state`` 上的东西（由 :func:`nacho.api.create_app` 写入）。"""

    auth_service: AuthService


class _App(Protocol):
    """FastAPI 的 ``app``，这里只关心它身上的 ``state``。"""

    state: _AppState


def get_auth_service(request: Request) -> AuthService:
    """取登录服务：装配时挂在 ``app.state.auth_service`` 上（见 :func:`nacho.api.create_app`）。

    ``app.state`` 是运行时挂上去的，类型检查看不到，所以把 ``request.app`` 先 ``cast`` 成
    带 ``state.auth_service`` 的形状，避免整条链都是 ``Any``。
    """
    app = cast("_App", request.app)
    return app.state.auth_service


#: 依赖简写：路由函数里写 ``service: AuthServiceDep`` 即可
AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
#: 依赖简写：令牌可能没带，拿到的是可空凭据
BearerDep = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]
