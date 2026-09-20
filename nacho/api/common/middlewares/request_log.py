"""请求日志中间件：给每条请求编号，并留一行访问日志。

它做三件事：

1. **编号**：``trace_id`` 从请求头 ``X-Trace-Id`` 来（上游编过就沿用），没有就现编一个，
   挂到 ``request.state.trace_id``（异常处理器要用它填进响应体）与响应头 ``X-Trace-Id``；
2. **记访问日志**：一次请求一行（方法、路径、状态码、耗时、客户端 IP），走
   :data:`~nacho.api.logging.ACCESS_LOGGER_NAME`（``api.access``）；
   ``options.access_log = False`` 就一条都不记（但编号照旧生成）；
3. **记没兜住的异常**：业务异常（:class:`~nacho.api.common.errors.ApiError`）由异常处理器记过
   了，这里不重复；其余记一条带堆栈的 ERROR。

用的是 Starlette 的 ``BaseHTTPMiddleware``：写法简单，代价是不能做流式响应。登录这类
普通 JSON 接口不在乎这个，真要流式再换纯 ASGI 中间件。
"""
from __future__ import annotations

import time
from typing import override
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from nacho.core.logger import BaseLogger

from ...logging import ACCESS_LOGGER_NAME, TRACE_ID_HEADER, api_logger
from ...options import ApiOptions
from ..headers import SESSION_EXPIRES_HEADER, SESSION_EXPIRES_STATE


class RequestLogMiddleware(BaseHTTPMiddleware):
    """请求日志中间件：编 ``trace_id``、记访问日志、异常时记一条错误日志。

    ``trace_id`` 从请求头 ``X-Trace-Id`` 来（上游编过就沿用），没有就现编一个，之后
    挂到 ``request.state.trace_id``（异常处理器要用它填进响应）与响应头上。

    访问日志走 :data:`ACCESS_LOGGER_NAME`；``options.access_log = False`` 就一条都不记
    （但 ``trace_id`` 照旧生成，响应头照旧带）。
    """

    def __init__(
        self,
        app: object,
        *,
        options: ApiOptions | None = None,
        logger: BaseLogger | None = None,
    ) -> None:
        super().__init__(app)  # pyright: ignore[reportArgumentType]
        self._options: ApiOptions = options if options is not None else ApiOptions()
        self._logger: BaseLogger | None = logger

    @property
    def options(self) -> ApiOptions:
        """当前生效的接口层选项（访问日志开关在里面）。"""
        return self._options

    @override
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """处理一次请求：编号 -> 放行 -> 记日志（出异常则记错误后原样抛回去）。"""
        trace_id: str = request.headers.get(TRACE_ID_HEADER) or uuid.uuid4().hex
        request.state.trace_id = trace_id

        started: float = time.perf_counter()
        try:
            response: Response = await call_next(request)
        except Exception as exc:
            self._log_failure(request, exc, trace_id, started)
            raise
        duration_ms: float = round((time.perf_counter() - started) * 1000, 2)
        response.headers[TRACE_ID_HEADER] = trace_id
        self._echo_session_expiry(request, response)

        if self._options.access_log:
            self._access.info(
                "请求完成",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=duration_ms,
                client=request.client.host if request.client else "",
                trace_id=trace_id,
            )
        return response

    def _echo_session_expiry(self, request: Request, response: Response) -> None:
        """把这次请求顺带续期后的剩余秒数回给客户端（鉴权依赖挂上去的）。

        访问令牌是**滑动续期**的：每次带令牌的请求都会把有效期往后延，所以登录时算出来的
        倒计时会越走越偏——让每个响应都把它带回去，前端照着拨准即可。
        """
        expires_in: object = getattr(request.state, SESSION_EXPIRES_STATE, None)
        if expires_in is not None:
            response.headers[SESSION_EXPIRES_HEADER] = str(expires_in)

    def _log_failure(
        self, request: Request, exc: Exception, trace_id: str, started: float
    ) -> None:
        """请求抛异常：业务异常（已被处理器记过）不重复记，其余记一条带堆栈的 ERROR。"""
        from ..errors import ApiError  # 延迟导入：只为拿类型

        if isinstance(exc, ApiError):
            return
        self._access.exception(
            "请求处理异常",
            method=request.method,
            path=request.url.path,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            trace_id=trace_id,
        )

    @property
    def _access(self) -> BaseLogger:
        """访问日志实例；没指定就用 ``api.access`` 那个（用完即取，不缓存）。"""
        return self._logger if self._logger is not None else api_logger(ACCESS_LOGGER_NAME)
