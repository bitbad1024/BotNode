"""日志管理器：进程级门面，管理默认核心与按名缓存的子实例。

这里是「便捷用法」的入口，底层就是 :class:`~nacho.core.logger.core.LogCore`：
:func:`configure` 建立（或复用）进程默认核心，:func:`get_logger` 从它派生子实例，
子实例与核心共享同一个消息队列与分发器。子实例自己挂了出口就只投那份（自层覆盖），
没挂才整份走派生那一刻从核心**复制**的落回配置（创建即冻结），因此不需要重复启动分发器。

重复调用 :func:`configure` 不再静默丢弃参数：新传入的处理机会增量挂到已有核心上，
同名的会被替换（换输出路径时用得上）。

需要「一个模块一种输出路径」时，用
:func:`~nacho.core.logger.core.attach_mount` 按模块挂载，或直接用
:class:`~nacho.core.logger.core.LogCore` 建多套互不干扰的日志系统。
"""
from __future__ import annotations

from typing import TextIO

from .base import BaseLogger
from .core import LogCore, current_default_core, set_default_core
from .models import LogLevel
from .processors.base import BaseLogProcessor
from .queue import AsyncLogQueue, OverflowPolicy


class LogManager:
    """进程级日志管理器：管理默认核心与按名缓存的子实例。"""

    def __init__(self) -> None:
        self._loggers: dict[str, BaseLogger] = {}

    @property
    def core(self) -> LogCore | None:
        """当前默认核心实例；未配置时为 ``None``。"""
        return current_default_core()

    @property
    def root(self) -> BaseLogger | None:
        """默认核心实例（:attr:`core` 的旧名别名）。"""
        return current_default_core()

    @property
    def loggers(self) -> dict[str, BaseLogger]:
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
            for processor in processors or []:
                _ = existing.attach(processor, replace=True)
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

    def get_logger(self, name: str | None = None) -> BaseLogger:
        """获取日志实例；未配置时先按默认参数建立默认核心。

        非核心名的实例是核心的 :meth:`~nacho.core.logger.base.BaseLogger.child`，
        名字**相对核心**：``get_logger("api.robot")`` 得到的名字是 ``nacho.api.robot``
        （核心名 ``nacho``）；写全名也行。名字按 ``.`` 逐段派生（``nacho.api`` ->
        ``nacho.api.robot``），所以每一层的落回配置来自它上一层。

        实例与核心共享同一个队列与分发器；落回配置是**派生那一刻复制的副本**，
        创建即冻结：先 ``configure`` / ``attach`` 再 ``get_logger``，否则模块
        拿不到之后才挂的出口。实例自己 :meth:`~nacho.core.logger.base.BaseLogger.attach`
        了出口后就只投那份（自层覆盖），不再带上核心的文件出口。同名实例只建一次，
        重复调用返回同一个对象。
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


def get_logger(name: str | None = None) -> BaseLogger:
    """便捷函数，等价于 ``manager.get_logger(name)``。"""
    return manager.get_logger(name)
