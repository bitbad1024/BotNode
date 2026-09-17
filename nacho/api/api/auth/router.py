"""鉴权入口的 HTTP 入口：``POST <prefix>/auth/login`` 与 ``GET <prefix>/auth/me``。

* ``/login``：账号 + 密码换一个令牌；失败按 :class:`~nacho.api.common.models.ErrorResponse`
  回 401（凭据不对）/ 403（账号停用）/ 422（参数没过校验）；
* ``/me``：拿令牌换当前用户资料 —— 顺带演示令牌怎么用（``Authorization: Bearer <token>``），
  令牌没带 / 过期 / 被改过都回 401。

通行证用 FastAPI 的 ``HTTPBearer(auto_error=False)``：没带令牌时不让框架直接回它自己的
错误格式，而是交给我们自己抛 :class:`~nacho.api.common.errors.UnauthorizedError`，出口形状
才统一。

路由本身不含业务判断：把请求体翻译成业务层的 :class:`~nacho.api.services.auth.models.Credentials`，
调 :class:`~nacho.api.services.auth.service.AuthService`，再把领域结果装配成响应模型。
"""
from __future__ import annotations

from fastapi import APIRouter, Request, status

from ...common.dependencies import trace_id_of
from ...common.errors import HttpStatus, UnauthorizedError
from ...common.models import ApiResponse, ErrorResponse
from ...services.auth.models import Credentials, LoginResult
from ...services.user.models import UserProfile
from .dependencies import AuthServiceDep, BearerDep
from .requests import LoginRequest
from .responses import LoginData

router = APIRouter(prefix="/auth", tags=["登录"])


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
    service: AuthServiceDep,
) -> ApiResponse[LoginData]:
    """账号 + 密码换令牌。

    请求体没过校验（账号字符集、密码长度）时不会进到这里，由异常处理器回 422。
    """
    trace_id: str = trace_id_of(request)
    credentials = Credentials(
        account=payload.account,
        password=payload.password.get_secret_value(),
    )
    result: LoginResult = await service.login(credentials, trace_id=trace_id)
    data = LoginData(token=result.token, expires_in=result.expires_in, user=result.user)
    return ApiResponse[LoginData](data=data, trace_id=trace_id)


@router.get(
    "/me",
    response_model=ApiResponse[UserProfile],
    summary="当前登录用户",
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorResponse,
            "description": "没带令牌 / 令牌过期或无效",
        },
    },
)
async def me(
    request: Request,
    service: AuthServiceDep,
    credentials: BearerDep,
) -> ApiResponse[UserProfile]:
    """用令牌换当前用户资料（演示令牌怎么使）。"""
    if credentials is None:
        raise UnauthorizedError("缺少令牌")
    trace_id: str = trace_id_of(request)
    profile: UserProfile = await service.current_user(
        credentials.credentials, trace_id=trace_id
    )
    return ApiResponse[UserProfile](data=profile, trace_id=trace_id)
