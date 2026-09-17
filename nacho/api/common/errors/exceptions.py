"""接口层的异常类型。

业务模块用异常表达「这次请求办不了」，HTTP 那层的事（状态码、响应头）由异常自己带着，
业务代码不用知道::

    raise InvalidCredentialsError()      # 401 + WWW-Authenticate: Bearer
    raise AccountDisabledError()         # 403

基类 :class:`ApiError` 装「一个错误码 + 一句话 + 一个状态码 + 逐字段细节」，下面这些
语义化子类只是把常见的组合预先配好，省得每处都写一遍状态码。

异常类型也是**全局**的（跟错误码一样）：一个模块抛、另一个模块也能 catch。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from fastapi import status

from ..models import DEFAULT_TRACE_ID, ErrorDetail, ErrorPayload, ErrorResponse
from .codes import ErrorCode, HttpStatus

#: 没过日志中间件时（比如单测直接调 handler）用的占位编号，同 models.DEFAULT_TRACE_ID
_TRACE_FALLBACK: str = DEFAULT_TRACE_ID


class ApiError(Exception):
    """接口层异常：一个错误码 + 一句话 + 一个 HTTP 状态码。

    :param code: :class:`ErrorCode` 里的错误码。
    :param message: 给人看的一句话（会原样进响应）。
    :param status_code: HTTP 状态码，默认 400。
    :param details: 逐字段的细节（校验失败时列「哪个字段、为什么」）。
    :param headers: 要一起回的响应头（如 ``WWW-Authenticate``）。
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        details: Sequence[ErrorDetail] = (),
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message: str = message
        self.status_code: int = status_code
        self.details: list[ErrorDetail] = list(details)
        self.headers: dict[str, str] = dict(headers or {})

    def to_payload(self) -> ErrorPayload:
        """错误本体（响应里 ``error`` 那一段）。"""
        return ErrorPayload(code=str(self.code), message=self.message, details=list(self.details))

    def to_response(self, trace_id: str = _TRACE_FALLBACK) -> ErrorResponse:
        """完整的失败响应体。"""
        return ErrorResponse(error=self.to_payload(), trace_id=trace_id)

# --------------------------------------------------------------------- 语义化子类
class ValidationError(ApiError):
    """请求体没过校验（422）；一般不用手动抛，交给 pydantic。"""

    def __init__(self, message: str, details: Sequence[ErrorDetail] = ()) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            message,
            status_code=HttpStatus.UNPROCESSABLE_ENTITY,
            details=details,
        )


class InvalidCredentialsError(ApiError):
    """账号或密码不对（401）。

    账号「不存在」和「密码错」都用它：分两种回给客户端等于免费告诉对方哪些账号存在。
    """

    def __init__(self, message: str = "账号或密码不正确") -> None:
        super().__init__(
            ErrorCode.INVALID_CREDENTIALS,
            message,
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
        )


class AccountDisabledError(ApiError):
    """账号被停用（403）：身份对，但不许进。"""

    def __init__(self, message: str = "账号已停用") -> None:
        super().__init__(
            ErrorCode.ACCOUNT_DISABLED, message, status_code=status.HTTP_403_FORBIDDEN
        )


class UnauthorizedError(ApiError):
    """没带令牌 / 令牌用不了（401）。"""

    def __init__(self, message: str = "需要登录", code: ErrorCode = ErrorCode.UNAUTHORIZED) -> None:
        super().__init__(
            code,
            message,
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
        )


class TokenInvalidError(UnauthorizedError):
    """令牌被改过 / 不是本服务签的（401）。"""

    def __init__(self, message: str = "令牌无效") -> None:
        super().__init__(message, code=ErrorCode.TOKEN_INVALID)


class TokenExpiredError(UnauthorizedError):
    """令牌过期（401），客户端拿它决定去重新登录。"""

    def __init__(self, message: str = "令牌已过期") -> None:
        super().__init__(message, code=ErrorCode.TOKEN_EXPIRED)


class InternalError(ApiError):
    """服务端出错（500）；响应里不带细节，细节只在日志里。"""

    def __init__(self, message: str = "服务内部错误") -> None:
        super().__init__(
            ErrorCode.INTERNAL_ERROR,
            message,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
