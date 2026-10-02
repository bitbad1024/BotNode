"""cache 的日志接入点。

接的是 ``nacho.core.logger`` 那套进程门面，用名字 ``cache``（相对核心 ``nacho`` ->
``nacho.cache``）。**业务模块一律不直接 ``default_core()``** —— 要日志实例就调
:func:`cache_logger`，装配层也可经 ``Cache(..., logger=)`` 传入。核心由装配层
:func:`set_core` 存进本模块槽位；``import`` 零副作用，没装配就调用会当场抛错（fail
fast），不会默默按默认参数建一份把配置定死的核心。详见 ``docs/cache/cache.md``。
"""
from __future__ import annotations

from typing import cast

from nacho.core.logger import BaseLogger

#: cache 层日志的名字（**相对核心**：核心名 nacho -> nacho.cache）
CACHE_LOGGER_NAME: str = "cache"

#: 装配槽位：组合根建好核心后存进来；``None`` = 尚未装配
_core: BaseLogger | None = None


def set_core(core: BaseLogger | None) -> None:
    """装配日志核心（传 ``None`` 清除槽位，测试收尾用）。"""
    global _core
    _core = core


def cache_logger(name: str = CACHE_LOGGER_NAME) -> BaseLogger:
    """取 cache 层的日志实例；未装配时抛错（见模块文档）。

    ``child()`` 返回的是具体的 :class:`~nacho.core.logger.ChildLogger`，这里收窄成
    ``BaseLogger`` 接口对外 —— 与各构造参数的 ``logger: BaseLogger | None`` 约定一致。
    """
    if _core is None:
        raise RuntimeError(
            "cache_logger 尚未装配：先调 nacho.wiring.wire_loggers(core) 或 "
            "nacho.core.cache.logging.set_core(core)"
        )
    return cast(BaseLogger, cast(object, _core.child(name)))
