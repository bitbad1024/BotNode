"""接口层的错误出口：错误码、异常类型、以及把它们翻成 HTTP 响应的处理器。

业务模块只管 ``raise`` 这里的异常，不用碰状态码和响应体::

    from tickneko.api import InvalidCredentialsError

    raise InvalidCredentialsError()          # -> 401 INVALID_CREDENTIALS
    raise AccountDisabledError()             # -> 403 ACCOUNT_DISABLED
    raise ApiError(ErrorCode.NOT_FOUND, "没有这个机器人", status_code=404)

这里按类型分三份::

    codes.py       错误码枚举（给客户端做分支用，稳定不变）
    exceptions.py  异常类型（自带 HTTP 状态码与响应头）
    handlers.py    把它们翻成统一响应的处理器（挂到 app 上）

出去的错误**永远**是 :class:`~tickneko.api.common.models.ErrorResponse` 那个形状，靠四层
兜底：

* :class:`ApiError` —— 业务主动抛的（含上面这些语义化子类）；
* FastAPI 的 ``RequestValidationError`` —— 请求体没过 pydantic（缺字段、账号字符集不对），
  翻成 ``422 VALIDATION_ERROR``，并把每一项错在哪个字段列进 ``details``；
* Starlette 的 ``HTTPException``（404 等）与**其它一切异常** —— 前者转成 ``HTTP_ERROR``，
  后者记一条 ERROR 日志（带堆栈）后回 ``500 INTERNAL_ERROR``，**只回错误码不回细节**，
  免得把内部实现暴露出去。

处理器由 :func:`register_exception_handlers` 一次性装到 app 上（:func:`create_app` 已经装好）。
"""
from __future__ import annotations

from .codes import ErrorCode, HttpStatus
from .exceptions import (
    AccountAlreadyExistsError,
    AccountDisabledError,
    ApiError,
    AvatarTooLargeError,
    AvatarTypeUnsupportedError,
    InternalError,
    InvalidCredentialsError,
    PasswordMismatchError,
    TokenExpiredError,
    TokenInvalidError,
    UnauthorizedError,
    ValidationError,
)
from .handlers import register_exception_handlers

__all__ = [
    "ErrorCode",
    "HttpStatus",
    "ApiError",
    "ValidationError",
    "InvalidCredentialsError",
    "PasswordMismatchError",
    "AccountDisabledError",
    "AccountAlreadyExistsError",
    "AvatarTooLargeError",
    "AvatarTypeUnsupportedError",
    "UnauthorizedError",
    "TokenInvalidError",
    "TokenExpiredError",
    "InternalError",
    "register_exception_handlers",
]
