"""OneBot 管理路由的注入件（``Depends`` 的那些东西）。

单独一份是为了让 ``router.py`` 只剩「接口长什么样」：怎么拿到服务、怎么要求登录，
堆在路由里会盖住接口本身。

**登录校验直接复用鉴权那份**（:data:`nacho.api.api.auth.dependencies.CurrentUserDep`）：
OneBot 管理接口用的就是登录令牌，没有第二套凭据。跨业务的那份（取请求编号）在
:mod:`nacho.api.common.dependencies`。

**光认人不够，还得看身份**：OneBot 管理是**全局**的事——踢任意人的客户端、吊销任意人的令牌，
所以「登录了就能干」等于把所有人的机器人交给每一个登录用户。归属就是令牌记录的 ``id``，
带 ``admin`` 角色的人**不限**（能管所有归属），其余人**只限自己的 id**（``user.user.id``）。
判管理员用 :func:`is_admin`，「能不能碰某个归属」由 :func:`may_touch` /
:func:`ensure_can_touch` 把关。

两种越界的出口是**故意不同**的：

* 调用方自己写出来的归属（``?id=``）→ **403**，说清楚「你不能碰这个归属」，比返回一个空列表有用；
* 按 **id** 找东西（``{token_id}``）→ **404**，越界与不存在走同一个出口。给 403 等于告诉对方
  「这条 id 是存在的」，拿 id 就能试探出别人有没有令牌；这也和 ``/auth/sessions`` 吊销别人
  会话时的处理一致。

**昵称**：令牌列表要显示归属的昵称，所以这里还提供一个 ``UserStore`` 注入件
（:data:`UserStoreDep`）——拿归属的 ``id`` 去用户表查，查不到就空串（``id`` 不一定是用户 id，
本层不解释它的语义）。
"""
from __future__ import annotations

from typing import Annotated, Protocol, cast

from fastapi import Depends, Request, status

from ...common.errors import ApiError, ErrorCode
from ...services.auth.models import CurrentUser
from ...services.user.protocols import UserStore
from ..auth.dependencies import CurrentUserDep
from .protocols import OneBotLike


class _AppState(Protocol):
    """挂在 ``app.state`` 上的东西（由 :func:`nacho.api.create_app` 写入）。"""

    onebot_server: OneBotLike | None
    user_store: UserStore


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


def get_user_store(request: Request) -> UserStore:
    """取用户存储：装配时挂在 ``app.state.user_store`` 上（见 :func:`create_app`）。

    令牌列表用它把归属的 ``id`` 翻成昵称。
    """
    app = cast("_App", request.app)
    return app.state.user_store


#: 依赖简写：路由函数里写 ``users: UserStoreDep`` 即可
UserStoreDep = Annotated[UserStore, Depends(get_user_store)]


# --------------------------------------------------------------------------- 授权
#: 管理员角色名。约定与演示账号一致（admin 账号带 ``("admin", "user")``，见
#: :mod:`nacho.api.services.user.demo`）。
ADMIN_ROLE: str = "admin"


def is_admin(user: CurrentUser) -> bool:
    """这人是不是管理员：能管所有账号。"""
    return ADMIN_ROLE in user.user.roles


def may_touch(user: CurrentUser, owner_id: str) -> bool:
    """归属 ``owner_id`` 名下的令牌 / 客户端，这人能不能碰：管理员随便碰，其余人只限自己。"""
    return is_admin(user) or owner_id == user.user.id


def ensure_can_touch(user: CurrentUser, owner_id: str) -> None:
    """碰不得就 **403**；用在「归属是调用方自己写出来的」那处（``?id=``）。

    按 id 找东西的地方**不要**用它 —— 那里一律回 404，免得拿 id 试探出存在性，见模块开头。
    """
    if not may_touch(user, owner_id):
        raise ApiError(
            ErrorCode.HTTP_ERROR,
            f"只能操作自己（{user.user.id}）名下的内容",
            status_code=status.HTTP_403_FORBIDDEN,
        )


__all__ = [
    "ADMIN_ROLE",
    "CurrentUserDep",
    "OneBotDep",
    "UserStoreDep",
    "ensure_can_touch",
    "get_onebot",
    "get_user_store",
    "is_admin",
    "may_touch",
]
