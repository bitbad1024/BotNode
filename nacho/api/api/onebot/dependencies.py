"""OneBot 管理路由的注入件（``Depends`` 的那些东西）。

单独一份是为了让 ``router.py`` 只剩「接口长什么样」：怎么拿到服务、怎么要求登录，
堆在路由里会盖住接口本身。

**登录校验直接复用鉴权那份**（:data:`nacho.api.api.auth.dependencies.CurrentUserDep`）：
OneBot 管理接口用的就是登录令牌，没有第二套凭据。跨业务的那份（取请求编号）在
:mod:`nacho.api.common.dependencies`。

**光认人不够，还得定范围**：OneBot 管理是**全局**的事——踢任意账号的客户端、给任意账号
签令牌，所以「登录了就能干」等于把所有人的机器人交给每一个登录用户。这里把权限收成一个
概念：:func:`scope_of` —— 带 :data:`ADMIN_ROLE` 的人**不限范围**，其余人**只限自己那个
账号**（``user.user.account``）。于是「谁能碰哪个账号」只有一处定义，六个接口都按它来。

两种越界的出口是**故意不同**的：

* 调用方自己写出来的账号名（``?account=``、签发请求体里的 ``account``）→ **403**，
  说清楚「你不能碰这个账号」，比返回一个空列表有用；
* 按 **id** 找东西（``{client_id}`` / ``{token_id}``）→ **404**，越界与不存在走同一个出口。
  给 403 等于告诉对方「这条 id 是存在的」，拿 id 就能试探出别人有几条令牌；这也和
  ``/auth/sessions`` 吊销别人会话时的处理一致。
"""
from __future__ import annotations

from typing import Annotated, Protocol, cast

from fastapi import Depends, Request, status

from ...common.errors import ApiError, ErrorCode
from ...services.auth.models import CurrentUser
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


# --------------------------------------------------------------------------- 范围（授权）
#: 管理员角色名。约定与演示账号一致（admin 账号带 ``("admin", "user")``，见
#: :mod:`nacho.api.services.user.demo`）。
#:
#: 它只在这里用一次：定「谁的范围不限」。哪天别的模块也要判管理员，就该把它挪到共同的
#: 位置去，别再抄一份字符串。
ADMIN_ROLE: str = "admin"


def scope_of(user: CurrentUser) -> str | None:
    """这个人能碰的账号范围：管理员是 ``None``（**不限**），其余人只限**自己那一个**。

    ``user.user.account`` 就是登录账号（``admin`` / ``robot``），而 OneBot 令牌与在线客户端
    的 ``account`` 说的也是「归哪个账号」——两边是同一套命名，这也是这套权限成立的前提。
    """
    return None if ADMIN_ROLE in user.user.roles else user.user.account


def may_touch(user: CurrentUser, account: str) -> bool:
    """``account`` 名下的令牌 / 客户端，这个人能不能碰。"""
    allowed = scope_of(user)
    return allowed is None or allowed == account


def ensure_can_touch(user: CurrentUser, account: str) -> None:
    """碰不得就 **403**；用在「账号名是调用方自己写出来的」那两处（``?account=`` 与签发请求体）。

    按 id 找东西的地方**不要**用它 —— 那里一律回 404，免得拿 id 试探出存在性，见模块开头。
    """
    if not may_touch(user, account):
        raise ApiError(
            ErrorCode.HTTP_ERROR,
            f"只能操作自己账号（{user.user.account}）下的内容",
            status_code=status.HTTP_403_FORBIDDEN,
        )


__all__ = [
    "ADMIN_ROLE",
    "CurrentUserDep",
    "OneBotDep",
    "ensure_can_touch",
    "get_onebot",
    "may_touch",
    "scope_of",
]
