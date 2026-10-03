"""scheduler 的日志接入点。

接的是 ``tickneko.core.logger`` 那套进程门面，用名字 ``scheduler``（相对核心 ``tickneko`` ->
``tickneko.scheduler``）。**业务模块一律不直接 ``default_core()``** —— 要日志实例就调
:func:`scheduler_logger`，装配层也可经 ``logger=`` 传入。核心由装配层 :func:`set_core`
存进本模块槽位；``import`` 零副作用，没装配就调用会当场抛错（fail fast），不会默默按
默认参数建一份把配置定死的核心。日志接入的完整说明见 ``docs/scheduler/scheduler.md``。
"""
from __future__ import annotations

from typing import cast

from tickneko.core.logger import BaseLogger

#: scheduler 层日志的名字（**相对核心**：核心名 tickneko -> tickneko.scheduler）
SCHEDULER_LOGGER_NAME: str = "scheduler"

#: 装配槽位：组合根建好核心后存进来；``None`` = 尚未装配
_core: BaseLogger | None = None


def set_core(core: BaseLogger | None) -> None:
    """装配日志核心（传 ``None`` 清除槽位，测试收尾用）。"""
    global _core
    _core = core


def scheduler_logger(name: str = SCHEDULER_LOGGER_NAME) -> BaseLogger:
    """取 scheduler 层的日志实例；未装配时抛错（见模块文档）。

    ``child()`` 返回的是具体的 :class:`~tickneko.core.logger.ChildLogger`，这里收窄成
    ``BaseLogger`` 接口对外 —— 与各构造参数的 ``logger: BaseLogger | None`` 约定一致。
    """
    if _core is None:
        raise RuntimeError(
            "scheduler_logger 尚未装配：先调 tickneko.wiring.wire_loggers(core) 或 "
            "tickneko.core.scheduler.logging.set_core(core)"
        )
    return cast(BaseLogger, cast(object, _core.child(name)))
