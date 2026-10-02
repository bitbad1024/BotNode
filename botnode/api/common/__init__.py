"""接口层里不属于任何一个业务的东西：响应壳、错误出口、中间件、路由通用依赖。

这一段是**横向**的（每个业务都要用），所以不塞进任何业务模块里::

    models.py        响应壳：ApiResponse / ErrorResponse / ErrorPayload / ErrorDetail
    errors/          错误出口：错误码 / 异常类型 / 翻响应的处理器
    middlewares/     请求中间件：编号 + 访问日志
    dependencies.py  路由通用依赖：取这次请求的编号

业务自己的东西：HTTP 入口在 :mod:`botnode.api.api`、业务逻辑在 :mod:`botnode.api.services`。

这里的一律**不认识具体业务**：响应壳不知道 ``data`` 里装的是什么，错误出口也不知道哪一
个码是登录用的 —— 加一个业务模块不需要动这里。
"""
from __future__ import annotations

from .dependencies import trace_id_of
from .errors import (
    AccountDisabledError,
    ApiError,
    AvatarTooLargeError,
    AvatarTypeUnsupportedError,
    ErrorCode,
    HttpStatus,
    InternalError,
    InvalidCredentialsError,
    TokenExpiredError,
    TokenInvalidError,
    UnauthorizedError,
    ValidationError,
    register_exception_handlers,
)
from .middlewares import RequestLogMiddleware
from .models import (
    DEFAULT_TRACE_ID,
    ApiResponse,
    ErrorDetail,
    ErrorPayload,
    ErrorResponse,
)

__all__ = [
    # 响应壳
    "ApiResponse",
    "ErrorResponse",
    "ErrorPayload",
    "ErrorDetail",
    "DEFAULT_TRACE_ID",
    # 错误出口
    "ErrorCode",
    "HttpStatus",
    "ApiError",
    "ValidationError",
    "InvalidCredentialsError",
    "AccountDisabledError",
    "AvatarTooLargeError",
    "AvatarTypeUnsupportedError",
    "UnauthorizedError",
    "TokenInvalidError",
    "TokenExpiredError",
    "InternalError",
    "register_exception_handlers",
    # 中间件
    "RequestLogMiddleware",
    # 路由通用依赖
    "trace_id_of",
]
