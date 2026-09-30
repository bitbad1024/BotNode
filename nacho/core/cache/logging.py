"""cache 的日志接入点。

接的是 ``nacho.core.logger`` 那套进程门面，用名字 ``cache``（相对核心 ``nacho`` ->
``nacho.cache``）。**业务模块一律不直接 ``default_core()``** —— 要日志实例就调
:func:`cache_logger`；装配层建完核心后，也可把实例通过 ``Cache(..., logger=)`` 传入，
由模块自己的 ``_log()`` 方法取用。
"""
from __future__ import annotations

from nacho.core.logger import BaseLogger, default_core

#: cache 层日志的名字（**相对核心**：核心名 nacho -> nacho.cache）
CACHE_LOGGER_NAME: str = "cache"


def cache_logger(name: str = CACHE_LOGGER_NAME) -> BaseLogger:
    """取 cache 层的日志实例（没 ``configure`` 过会顺带建一个默认核心）。"""
    return default_core().child(name)
