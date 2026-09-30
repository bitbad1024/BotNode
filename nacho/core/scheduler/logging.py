"""scheduler 的日志接入点。

接的是 ``nacho.core.logger`` 那套进程门面，用名字 ``scheduler``（相对核心 ``nacho`` ->
``nacho.scheduler``）。**业务模块一律不直接 ``default_core()``** —— 要日志实例就调
:func:`scheduler_logger`；装配层建完核心后，也可把实例通过 ``Scheduler(..., logger=)``
传入，由模块自己的 ``_log()`` 方法取用。
"""
from __future__ import annotations

from nacho.core.logger import BaseLogger, default_core

#: scheduler 层日志的名字（**相对核心**：核心名 nacho -> nacho.scheduler）
SCHEDULER_LOGGER_NAME: str = "scheduler"


def scheduler_logger(name: str = SCHEDULER_LOGGER_NAME) -> BaseLogger:
    """取 scheduler 层的日志实例（没 ``configure`` 过会顺带建一个默认核心）。"""
    return default_core().child(name)
