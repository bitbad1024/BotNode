"""把异常翻成统一响应的处理器（四层兜底）。

这一份是错误出口里唯一认识 HTTP 的部分：其余两份（``codes`` / ``exceptions``）只描述
「错在哪、是什么码」，把状态码与 JSON 形状对应起来的是这里。

四个 handler 各管一段，注册顺序不影响：Starlette 按异常的类继承链找 handler。

* :class:`~nacho.api.common.errors.ApiError` —— 业务主动抛的，4xx 记 warning、5xx 记 error；
* FastAPI 的 ``RequestValidationError`` —— 请求体没过 pydantic，422 + 逐字段细节；
* Starlette 的 ``HTTPException``（404 等）—— 转成 ``HTTP_ERROR``，状态码照旧；
* ``Exception`` —— 兜底，记堆栈后回 500，**不带任何细节**。

异常处理器只需要 :func:`register_exception_handlers(app)` 一次装好。
"""
from __future__ import annotations

from typing import TypedDict, cast

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from nacho.core.logger import BaseLogger

from ...logging import api_logger
from ..models import DEFAULT_TRACE_ID, ErrorDetail
from .codes import ErrorCode
from .exceptions import ApiError, InternalError, ValidationError

#: 没过日志中间件时（比如单测直接调 handler）用的占位编号，同 models.DEFAULT_TRACE_ID
_TRACE_FALLBACK: str = DEFAULT_TRACE_ID


class _ValidationErrorItem(TypedDict, total=False):
    """``RequestValidationError.errors()`` 返回的每一项结构（pydantic 校验错误的扁平形态）。

    标出来是为了不让 ``exc.errors()`` 被推断成 ``Any`` 后一路传染——下面取 ``loc``/``msg``
    时就拿不到真实类型了。
    """

    loc: tuple[str | int, ...]
    msg: str


def to_error_details(exc: RequestValidationError) -> list[ErrorDetail]:
    """把 pydantic 的报错翻成我们的 ``details``：一个字段一项。

    ``loc`` 里的 ``body`` 只是位置说明，去掉；剩下的拼成 ``body.account`` 这样。
    自己校验器说的话前面带 ``Value error, ``，一并去掉（那是 pydantic 加的前缀）。
    """
    details: list[ErrorDetail] = []
    for error in cast(list[_ValidationErrorItem], exc.errors()):
        field: str = ".".join(str(part) for part in error.get("loc", ()))
        message: str = str(error.get("msg", "")).removeprefix("Value error, ")
        details.append(ErrorDetail(field=field, message=message))
    return details


def _trace_id(request: Request) -> str:
    """这一次请求的编号（日志中间件生成并挂到 ``request.state`` 上）。"""
    return str(getattr(request.state, "trace_id", "") or _TRACE_FALLBACK)  # noqa: B009 可能没挂上


def _json(request: Request, error: ApiError) -> JSONResponse:
    """按统一结构回一个 JSON 响应。"""
    return JSONResponse(
        status_code=error.status_code,
        content=jsonable_encoder(error.to_response(_trace_id(request)).model_dump()),
        headers=error.headers or None,
    )


def register_exception_handlers(app: "object", *, logger: BaseLogger | None = None) -> None:
    """把异常处理器装到 app 上（``FastAPI`` 实例）：四层兜底，出口形状统一。

    这里把 ``app`` 标成 ``object`` 是为了让本模块不必为了类型注解去 import FastAPI 的
    app 类——类型检查认 ``add_exception_handler`` 的调用方即可。
    """
    log: BaseLogger = logger if logger is not None else api_logger()
    add_handler = getattr(app, "add_exception_handler", None)
    if add_handler is None:  # 传进来的不是 FastAPI / Starlette 实例
        raise TypeError(f"需要一个 FastAPI 实例，收到 {type(app).__name__}")

    async def on_api_error(request: Request, exc: Exception) -> JSONResponse:
        """业务异常：4xx 记 warning（客户端的问题），5xx 记 error（我们的问题）。"""
        error: ApiError = exc if isinstance(exc, ApiError) else InternalError()
        record = log.error if error.status_code >= 500 else log.warning
        record(
            "请求失败",
            status=error.status_code,
            code=str(error.code),
            trace_id=_trace_id(request),
        )
        return _json(request, error)

    async def on_validation_error(request: Request, exc: Exception) -> JSONResponse:
        """请求体没过 pydantic：422 + 逐字段细节。"""
        details: list[ErrorDetail] = (
            to_error_details(exc) if isinstance(exc, RequestValidationError) else []
        )
        log.info(
            "请求校验失败",
            fields=[detail.field for detail in details],
            trace_id=_trace_id(request),
        )
        return _json(request, ValidationError("请求参数不合法", details))

    async def on_http_error(request: Request, exc: Exception) -> JSONResponse:
        """Starlette 的 HTTP 异常（404 等）：转成统一结构，状态码照旧。"""
        status_code: int = (
            exc.status_code if isinstance(exc, StarletteHTTPException) else 500
        )
        message: str = (
            exc.detail if isinstance(exc, StarletteHTTPException) else "请求无法处理"
        )
        return _json(
            request,
            ApiError(ErrorCode.HTTP_ERROR, str(message), status_code=status_code),
        )

    async def on_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """没预料到的异常：记堆栈，回 500 但不带细节。"""
        log.exception("请求处理异常", trace_id=_trace_id(request))
        return _json(request, InternalError())

    add_handler(ApiError, on_api_error)
    add_handler(RequestValidationError, on_validation_error)
    add_handler(StarletteHTTPException, on_http_error)
    add_handler(Exception, on_unexpected)
