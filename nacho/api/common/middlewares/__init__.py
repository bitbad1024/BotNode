"""HTTP 中间件：请求进出时统一要做的事（跟具体业务无关）。

现在只有一份::

    request_log.py  RequestLogMiddleware  给请求编号 + 记访问日志

中间件是**请求维度**的横切逻辑（每个请求都要过一遍），跟「异常怎么翻成响应」
（``common/errors/``）分开：前者管流程，后者管出口形状。
"""
from __future__ import annotations

from .request_log import RequestLogMiddleware

__all__ = ["RequestLogMiddleware"]
