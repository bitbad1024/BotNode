"""路由的通用依赖：跟具体业务无关的那些。

业务模块自己的注入件（怎么拿到它的服务、令牌从哪个头来）在各自的模块里；这一份只放
每个路由都要用、又不认识任何业务的东西 —— 目前是 ``trace_id``。
"""
from __future__ import annotations

from fastapi import Request

from .models import DEFAULT_TRACE_ID


def trace_id_of(request: Request) -> str:
    """这次请求的编号（日志中间件生成；没过中间件就用占位符）。"""
    return str(getattr(request.state, "trace_id", "") or DEFAULT_TRACE_ID)  # noqa: B009 可能没挂上
