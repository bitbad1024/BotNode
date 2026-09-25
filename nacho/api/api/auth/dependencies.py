"""鉴权路由的注入件（``Depends`` 的那些东西）。

单独一份是为了让 ``router.py`` 只剩「接口长什么样」：怎么拿到业务层的服务、令牌从哪来、
这次登录从哪来，堆在路由里会盖住接口本身。

跨业务的那份（取请求编号）在 :mod:`nacho.api.common.dependencies`。
"""
from __future__ import annotations

from typing import Annotated, Protocol, cast

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ...common.dependencies import trace_id_of
from ...common.errors import UnauthorizedError
from ...common.headers import SESSION_EXPIRES_STATE
from ...logging import OWNER_ID_STATE
from ...options import ApiOptions
from ...services.auth.models import CurrentUser
from ...services.auth.service import AuthService
from ...services.profile.service import ProfileService
from ...services.session.client import describe_client
from ...services.session.models import ClientInfo

#: 会话令牌的 Cookie 名。**HttpOnly**：JS 读不到、改不了，XSS 也就偷不走。
SESSION_COOKIE: str = "nacho_session"
#: 客户端 ip 的代理头：只有配了 ``[api].trust_proxy`` 才认（否则可以随便伪造）
CLIENT_IP_HEADER: str = "X-Forwarded-For"
#: 客户端自报的设备名（如「我的 iPhone」）；没有就从 User-Agent 推一个
DEVICE_NAME_HEADER: str = "X-Device-Name"

#: 令牌来源：``Authorization: Bearer <token>``；
#: ``auto_error=False`` 是不让框架直接回它自己的错误格式，交给我们自己抛统一的 401
bearer_scheme = HTTPBearer(
    auto_error=False,
    description=(
        "令牌：浏览器走 HttpOnly Cookie（nacho_session），非浏览器客户端可以带这个头；"
        "登录接口的响应体里也会回一份明文"
    ),
)


class _AppState(Protocol):
    """挂在 ``app.state`` 上的东西（由 :func:`nacho.api.create_app` 写入）。"""

    auth_service: AuthService
    api_options: ApiOptions
    profile_service: ProfileService


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


def get_profile_service(request: Request) -> ProfileService:
    """取个人设置服务：装配时挂在 ``app.state.profile_service`` 上。"""
    app = cast("_App", request.app)
    return app.state.profile_service


def get_api_options(request: Request) -> ApiOptions:
    """取接口层选项（信任代理、Cookie 有效期这类开关要用）。"""
    app = cast("_App", request.app)
    return app.state.api_options


def token_candidates(
    request: Request, credentials: HTTPAuthorizationCredentials | None
) -> list[str]:
    """令牌从哪来：**优先 HttpOnly Cookie**，其次 ``Authorization`` 头（去重保序）。

    浏览器那条路只有 Cookie（JS 读不到它，塞不进请求头）；头这条是给非浏览器客户端
    （脚本、接口文档的 Authorize）留的。
    """
    return list(
        dict.fromkeys(
            part
            for part in (
                request.cookies.get(SESSION_COOKIE, ""),
                credentials.credentials if credentials is not None else "",
            )
            if part
        )
    )


def client_info_of(request: Request, *, trust_proxy: bool = False) -> ClientInfo:
    """把这次请求的来源整理成 :class:`ClientInfo`（ip + 设备名 + User-Agent）。

    ip 默认取**连接的对端地址**；代理头 ``X-Forwarded-For`` 只在 ``trust_proxy`` 打开时
    才认——它本身是客户端可伪造的，没挂在可信代理后面就不能信。设备名优先用客户端自报的
    ``X-Device-Name``，没有就按 User-Agent 拼一个。
    """
    ip: str = ""
    if trust_proxy:
        forwarded: str = request.headers.get(CLIENT_IP_HEADER, "")
        if forwarded:
            ip = forwarded.split(",")[0].strip()  # 取最左边那一段 = 最初发起的客户端
    if not ip:
        ip = request.client.host if request.client else ""
    return describe_client(
        ip=ip,
        user_agent=request.headers.get("User-Agent", ""),
        device_name=request.headers.get(DEVICE_NAME_HEADER, ""),
    )


def get_client(
    request: Request, options: Annotated[ApiOptions, Depends(get_api_options)]
) -> ClientInfo:
    """这次登录从哪来（登录时要记进会话）。"""
    return client_info_of(request, trust_proxy=options.trust_proxy)


async def _current_user(
    request: Request,
    service: Annotated[AuthService, Depends(get_auth_service)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> CurrentUser:
    """要求登录：没带令牌 401；令牌无效 / 账号不可用由服务抛 401。

    Cookie 与 ``Authorization`` 头都带时**先认 Cookie，不行再试头**：浏览器只发 Cookie，
    脚本只发头，两者都发的通常是在调试——一条不认就换另一条，别让"顺手带上"的那个坏令牌
    把好令牌顶掉。

    认证成功顺带把两样东西挂到 ``request.state``：滑动续期后的剩余秒数（键名见
    :data:`~nacho.api.common.headers.SESSION_EXPIRES_STATE`），请求日志中间件会把它写进
    ``X-Session-Expires-In``，前端倒计时照着拨准；以及**这次操作属于谁**（键名见
    :data:`~nacho.api.logging.OWNER_ID_STATE`），中间件拿它当访问日志的 ``owner_id``。
    """
    candidates: list[str] = token_candidates(request, credentials)
    if not candidates:
        raise UnauthorizedError("缺少令牌")

    failure: UnauthorizedError = UnauthorizedError("令牌无效或已过期")
    for token in candidates:
        try:
            result: CurrentUser = await service.current_user(
                token, trace_id=trace_id_of(request)
            )
        except UnauthorizedError as exc:  # 这条不行，试下一条
            failure = exc
            continue
        # 键名走常量：写在这、读在请求日志中间件，两边各写一遍字符串迟早对不上
        setattr(request.state, SESSION_EXPIRES_STATE, result.expires_in)
        # 认出来了就是「这个人的操作」：留给日志当 owner_id。没走鉴权的请求（登录接口本身）
        # 自然没这一笔，日志那边就是空串 = 公共所有者
        setattr(request.state, OWNER_ID_STATE, result.user.id)
        return result
    raise failure


#: 依赖简写：路由函数里写 ``service: AuthServiceDep`` 即可
AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
#: 依赖简写：拿个人设置服务（头像查询要用）
ProfileServiceDep = Annotated[ProfileService, Depends(get_profile_service)]
#: 依赖简写：拿接口层选项（Cookie 要不要持久）
ApiOptionsDep = Annotated[ApiOptions, Depends(get_api_options)]
#: 依赖简写：令牌可能没带，拿到的是可空凭据
BearerDep = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]
#: 依赖简写：这次登录从哪来
ClientDep = Annotated[ClientInfo, Depends(get_client)]
#: 依赖简写：要登录的接口挂这个，拿到的是当前用户（含所在会话）
CurrentUserDep = Annotated[CurrentUser, Depends(_current_user)]
