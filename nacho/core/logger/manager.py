"""日志管理器：进程级门面，管理默认核心与按名取回的那份绑定。

这里是「便捷用法」的入口，底层就是 :class:`~nacho.core.logger.core.LogCore`：
:func:`configure` 建立（或复用）进程默认核心，:func:`get_logger` 按名字取一条
**具名绑定**（:class:`~nacho.core.logger.base.BoundLogger`），与核心共享同一个
队列与分发器，所以不需要重复启动分发器。

重复调用 :func:`configure` 不再静默丢弃参数：新传入的处理机会挂到已有核心上，
同名的会被替换（换输出路径时用得上）。

需要「一个模块一种输出路径」时，用 :func:`~nacho.core.logger.core.mount_module`
按模块发布一条具名路由。
"""
from __future__ import annotations

from typing import TextIO

from .base import BaseLogger, BoundLogger
from .core import LogCore, current_default_core, set_default_core
from .models import LogLevel
from .processors.base import BaseLogProcessor
from .queue import AsyncLogQueue, OverflowPolicy


class LogManager:
    """进程级日志管理器：管理默认核心与按名取回的绑定。"""

    def __init__(self) -> None:
        self._loggers: dict[str, BaseLogger | BoundLogger] = {}

    @property
    def core(self) -> LogCore | None:
        """当前默认核心实例；未配置时为 ``None``。"""
        return current_default_core()

    @property
    def root(self) -> BaseLogger | None:
        """默认核心实例（:attr:`core` 的旧名别名）。"""
        return current_default_core()

    @property
    def loggers(self) -> dict[str, BaseLogger | BoundLogger]:
        return dict(self._loggers)

    def configure(
        self,
        name: str = "nacho",
        *,
        level: "LogLevel | str" = LogLevel.INFO,
        processors: "list[BaseLogProcessor] | None" = None,
        console: bool = True,
        console_stream: TextIO | None = None,
        console_level: "LogLevel | str | None" = None,
        console_color: bool = False,
        queue: AsyncLogQueue | None = None,
        overflow_policy: "OverflowPolicy | str" = OverflowPolicy.DROP_OLDEST,
        queue_maxsize: int = 10000,
        dispatch_batch_size: int = 200,
        dispatch_timeout: float = 0.5,
    ) -> LogCore:
        """创建（或复用）进程默认核心，并增量挂载传入的处理机。

        已存在默认核心时不会重建，也不会丢弃参数：``processors`` 中与已挂载通道
        同名的会被替换，其余追加挂载。
        """
        existing: LogCore | None = current_default_core()
        if existing is not None:
            # 重复 configure：同名换成新的（换输出路径 / 热重载），其余追加
            existing.mount(*(processors or []), replace=True)
            return existing

        core = LogCore(
            name,
            level=level,
            console=console,
            console_stream=console_stream,
            console_level=console_level,
            console_color=console_color,
            processors=processors,
            queue=queue,
            overflow_policy=overflow_policy,
            queue_maxsize=queue_maxsize,
            dispatch_batch_size=dispatch_batch_size,
            dispatch_timeout=dispatch_timeout,
        )
        set_default_core(core)
        self._loggers[name] = core
        return core

    def get_logger(self, name: str | None = None) -> BaseLogger | BoundLogger:
        """按名字取一条绑定；未配置时先按默认参数建立默认核心。

        非核心名走 :meth:`~nacho.core.logger.base.BaseLogger.route`：名字**相对核心**
        （``get_logger("api.robot")`` -> ``nacho.api.robot``，写全名也行），发布过就
        一直返回同一份视图，没发布过就跟着 root 的默认目标走。

        视图与核心共享同一个队列与分发器，**没有落回配置、没有冻结**，所以也不再有
        「先挂载、再取实例」的顺序要求。同名只建一次，重复调用返回同一个对象。
        """
        core: LogCore = current_default_core() or self.configure()
        if name is None or name == core.name:
            return core

        # 就是一份「带名字的绑定」，没有派生实例这回事了
        view = core.route(name)
        self._loggers[view.name] = view
        return view

    async def start(self) -> None:
        core = current_default_core()
        if core is not None:
            _ = await core.start()

    async def stop(self, *, timeout: float = 5.0) -> None:
        core = current_default_core()
        if core is not None:
            await core.stop(timeout=timeout)

    def reset(self) -> None:
        """清空管理器状态与进程默认核心（主要用于测试）。

        只解除引用，**不会**停止原核心；需要优雅停机请先 ``await manager.stop()``。
        """
        self._loggers.clear()
        set_default_core(None)


#: 模块级默认管理器
manager: LogManager = LogManager()


def configure(
    name: str = "nacho",
    *,
    level: "LogLevel | str" = LogLevel.INFO,
    processors: "list[BaseLogProcessor] | None" = None,
    console: bool = True,
    console_stream: TextIO | None = None,
    console_level: "LogLevel | str | None" = None,
    console_color: bool = False,
    queue: AsyncLogQueue | None = None,
    overflow_policy: "OverflowPolicy | str" = OverflowPolicy.DROP_OLDEST,
    queue_maxsize: int = 10000,
    dispatch_batch_size: int = 200,
    dispatch_timeout: float = 0.5,
) -> LogCore:
    """便捷函数，等价于 ``manager.configure(...)``。"""
    return manager.configure(
        name,
        level=level,
        processors=processors,
        console=console,
        console_stream=console_stream,
        console_level=console_level,
        console_color=console_color,
        queue=queue,
        overflow_policy=overflow_policy,
        queue_maxsize=queue_maxsize,
        dispatch_batch_size=dispatch_batch_size,
        dispatch_timeout=dispatch_timeout,
    )


def get_logger(name: str | None = None) -> BaseLogger | BoundLogger:
    """便捷函数，等价于 ``manager.get_logger(name)``。"""
    return manager.get_logger(name)
