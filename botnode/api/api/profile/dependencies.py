"""个人设置路由的注入件：取业务层的服务（与别的入口层一个路子）。"""
from __future__ import annotations

from typing import Annotated, Protocol, cast

from fastapi import Depends, Request

from ...services.profile import ProfileService


class _AppState(Protocol):
    """挂在 ``app.state`` 上、本模块要用到的东西（由 :func:`botnode.api.create_app` 写入）。"""

    profile_service: ProfileService


class _App(Protocol):
    state: _AppState


def get_profile_service(request: Request) -> ProfileService:
    """取个人设置服务：装配时挂在 ``app.state.profile_service`` 上。"""
    app = cast("_App", request.app)
    return app.state.profile_service


#: 依赖简写：路由函数里写 ``service: ProfileServiceDep`` 即可
ProfileServiceDep = Annotated[ProfileService, Depends(get_profile_service)]


__all__ = ["ProfileServiceDep", "get_profile_service"]
