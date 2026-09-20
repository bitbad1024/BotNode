"""鉴权入口的 HTTP 路由。

    POST   <prefix>/auth/login          账号 + 密码换令牌（可勾「记住设备」、可复用旧令牌）
    GET    <prefix>/auth/me             拿令牌换当前用户
    GET    <prefix>/auth/sessions              我开着的登录（登录设备列表）
    DELETE <prefix>/auth/sessions/{token_hash} 吊销其中一条
    DELETE <prefix>/auth/sessions       全部下线（含当前这条）

令牌**不是 JWT**，只有一种：后端签发后写进 **HttpOnly Cookie**（路径限定在 ``<prefix>/auth``
之外的接口也要用，所以路径是整个前缀下的 auth 段），JS 读不到、改不了，XSS 就偷不走它。
响应体里也回一份明文，是给非浏览器客户端（脚本 / 接口文档）用的，它们可以改走
``Authorization: Bearer``。

「记住设备」决定两件事，一起生效：

* **Cookie 的寿命**：勾了带 ``Max-Age``（持久 Cookie）；没勾不带，就是**会话 Cookie**，
  浏览器关掉即丢；
* **服务端滑动有效期**：勾了长（默认 30 天）、没勾短（默认 2 小时）；有通讯就一直延期，
  闲置超了才过期。

因为认证靠 Cookie，这里把 Cookie 设成 ``SameSite=Lax``：跨站的 POST / DELETE 不会带上它，
省掉大部分 CSRF 面（单机内网控制台够用；真要对公网再上 CSRF token）。
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from ...common.dependencies import trace_id_of
from ...common.errors import ApiError, ErrorCode, HttpStatus
from ...common.models import ApiResponse, ErrorResponse
from ...options import ApiOptions
from ...services.auth.models import Credentials, LoginResult
from ...services.user.models import UserProfile
from .dependencies import (
    SESSION_COOKIE,
    ApiOptionsDep,
    AuthServiceDep,
    ClientDep,
    CurrentUserDep,
)
from .requests import LoginRequest
from .responses import (
    LoginData,
    RevokeAllData,
    RevokeSessionData,
    SessionData,
)

#: 浏览器对 Cookie 有效期有上限（Chrome 约 400 天），"永不过期"就按这个封顶
_MAX_COOKIE_AGE: int = 400 * 24 * 3600

router = APIRouter(prefix="/auth", tags=["登录"])


def _cookie_path(options: ApiOptions) -> str:
    """Cookie 挂在鉴权路由下，别的接口不必带上它。"""
    return f"{options.prefix}/auth"


def _issue_cookie(
    response: Response, *, token: str, remember: bool, options: ApiOptions
) -> None:
    """把令牌写进 HttpOnly Cookie；勾了「记住设备」才是持久 Cookie。"""
    if remember:
        max_age: int = (
            int(options.remember_ttl) if options.remember_ttl > 0 else _MAX_COOKIE_AGE
        )
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=max_age,
            httponly=True,
            samesite="lax",
            path=_cookie_path(options),
        )
        return
    # 不带 Max-Age：会话 Cookie，浏览器关掉就没了（服务端那边还有滑动闲置过期兜着）
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        path=_cookie_path(options),
    )


def _forget_cookie(response: Response, *, options: ApiOptions) -> None:
    """清掉 Cookie（把当前这条会话吊销了 / 全部下线）。"""
    response.delete_cookie(SESSION_COOKIE, path=_cookie_path(options))


def _previous_token(request: Request, payload: LoginRequest) -> str:
    """这次登录想复用的旧令牌从哪来：**请求体显式传的优先，其次浏览器自动带的 Cookie**。

    浏览器没有别的路走复用——令牌在 HttpOnly Cookie 里，JS 读不到、塞不进请求体；而 Cookie
    本来就挂在 ``<prefix>/auth`` 下，登录这个请求会自动带上它。脚本客户端则走请求体那个字段。
    """
    return payload.previous_token or request.cookies.get(SESSION_COOKIE, "")


def _login_data(result: LoginResult) -> LoginData:
    """把业务层的结果装成响应模型。"""
    session = result.session
    return LoginData(
        token=result.token,
        expires_in=result.expires_in,
        user=result.user,
        token_hash=session.token_hash if session is not None else "",
        device_name=session.device_name if session is not None else "",
        reused=result.reused,
    )


@router.post(
    "/login",
    response_model=ApiResponse[LoginData],
    status_code=status.HTTP_200_OK,
    summary="登录",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "账号或密码不对"},
        status.HTTP_403_FORBIDDEN: {"model": ErrorResponse, "description": "账号已停用"},
        HttpStatus.UNPROCESSABLE_ENTITY: {
            "model": ErrorResponse,
            "description": "请求参数不合法",
        },
    },
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    service: AuthServiceDep,
    client: ClientDep,
    options: ApiOptionsDep,
) -> ApiResponse[LoginData]:
    """账号 + 密码换令牌；令牌写进 HttpOnly Cookie。

    ``remember=true`` 时 Cookie 是持久的（默认 30 天）、服务端滑动有效期也长；不勾则是
    会话 Cookie + 短有效期（默认 2 小时）。请求体没过校验时不会进到这里，由异常处理器回 422。
    """
    trace_id: str = trace_id_of(request)
    result: LoginResult = await service.login(
        Credentials(account=payload.account, password=payload.password.get_secret_value()),
        client=client,
        remember=payload.remember,
        previous_token=_previous_token(request, payload),
        trace_id=trace_id,
    )
    _issue_cookie(
        response, token=result.token, remember=payload.remember, options=options
    )
    return ApiResponse[LoginData](data=_login_data(result), trace_id=trace_id)


@router.get(
    "/me",
    response_model=ApiResponse[UserProfile],
    summary="当前登录用户",
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorResponse,
            "description": "没登录 / 令牌无效或过期",
        }
    },
)
async def me(request: Request, user: CurrentUserDep) -> ApiResponse[UserProfile]:
    """拿令牌换当前用户资料。

    前端启动时也用它**探测登录态**：令牌在 HttpOnly Cookie 里，JS 读不到，只能问服务端。
    响应头会带上 ``X-Session-Expires-In``（这次滑动续期后的剩余秒数），倒计时照着它走。
    """
    return ApiResponse[UserProfile](data=user.user, trace_id=trace_id_of(request))


@router.get(
    "/sessions",
    response_model=ApiResponse[list[SessionData]],
    summary="登录设备列表",
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorResponse,
            "description": "没登录 / 令牌无效或过期",
        }
    },
)
async def list_sessions(
    request: Request, user: CurrentUserDep, service: AuthServiceDep
) -> ApiResponse[list[SessionData]]:
    """我开着的全部登录（新的在前）；当前这条会标 ``current=true``。"""
    trace_id: str = trace_id_of(request)
    records = await service.sessions_of(user.user.id)
    data = [
        SessionData(
            token_hash=row.token_hash,
            current=row.token_hash == user.token_hash,
            device_name=row.device_name,
            device_type=row.device_type,
            browser=row.browser,
            os=row.os,
            ip=row.ip,
            created_at=row.created_at,
            remembered=row.remembered,
        )
        for row in records
    ]
    return ApiResponse[list[SessionData]](data=data, trace_id=trace_id)


@router.delete(
    "/sessions/{token_hash}",
    response_model=ApiResponse[RevokeSessionData],
    summary="吊销一条登录",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse, "description": "没登录 / 令牌无效"},
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "没有这条登录"},
    },
)
async def revoke_session(
    token_hash: str,
    request: Request,
    response: Response,
    user: CurrentUserDep,
    service: AuthServiceDep,
    options: ApiOptionsDep,
) -> ApiResponse[RevokeSessionData]:
    """把某台设备下线：**双删**（先删缓存里的令牌，再删库里的会话行）。

    路径上那个值就是**令牌摘要**（「登录设备」列表里每行的 ``token_hash``）。
    吊销的是当前这条时，顺带把 Cookie 也清掉；别人家的摘要传进来一律按"没有这条登录"
    处理（不回 403，免得靠它试探出"存在但没权限"）。
    """
    trace_id: str = trace_id_of(request)
    removed: bool = await service.revoke_session(token_hash, user_id=user.user.id)
    if not removed:
        raise ApiError(
            ErrorCode.HTTP_ERROR, "没有这条登录", status_code=status.HTTP_404_NOT_FOUND
        )
    if token_hash == user.token_hash:
        _forget_cookie(response, options=options)
    return ApiResponse[RevokeSessionData](
        data=RevokeSessionData(token_hash=token_hash, removed=True), trace_id=trace_id
    )


@router.delete(
    "/sessions",
    response_model=ApiResponse[RevokeAllData],
    summary="全部下线",
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorResponse,
            "description": "没登录 / 令牌无效或过期",
        }
    },
)
async def revoke_all_sessions(
    request: Request,
    response: Response,
    user: CurrentUserDep,
    service: AuthServiceDep,
    options: ApiOptionsDep,
) -> ApiResponse[RevokeAllData]:
    """把我开的登录全部下线——**包含当前这条**（调用方随后应回登录页）。"""
    trace_id: str = trace_id_of(request)
    count: int = await service.revoke_all_sessions(user.user.id)
    _forget_cookie(response, options=options)
    return ApiResponse[RevokeAllData](data=RevokeAllData(count=count), trace_id=trace_id)
