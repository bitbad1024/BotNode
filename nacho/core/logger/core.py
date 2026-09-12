"""日志核心实例：实例化即可用，输出通道按需增量挂载。

与 :class:`~nacho.core.logger.manager.LogManager` 的分工：

* :class:`LogCore` 是**日志系统本体**，不依赖任何全局状态，可以自由实例化
  （一个进程里想要几套互不干扰的日志系统就建几个）；构造时默认挂一路控制台
  输出，所以 :meth:`~nacho.core.logger.base.BaseLogger.start` 之后立刻就有
  可见输出，不必先把文件 / 数据库后端准备好；
* :class:`LogManager` 是**进程级门面**：管理一个默认核心，并按名字缓存子实例，
  供 ``get_logger("api.robot")`` 这类调用使用。

典型用法（分阶段启动 + 增量挂载）::

    from nacho.core.logger import LogCore, LocalFileLogProcessor

    logger = LogCore()                          # 最小化启动：只有控制台
    await logger.start()

    db = Database(...)
    await db.connect()
    logger.attach(DatabaseLogProcessor(db))     # 运行期挂载，自动启动
    logger.attach(LocalFileLogProcessor("logs/nacho.log"))

    logger.info("机器人已启动", robot_id="r-001")
    await logger.stop()                         # 自动冲刷余量

模块解耦（推荐给子模块）::

    from nacho.core.logger import attach_mount, get_logger, LocalFileLogProcessor

    attach_mount("module_a", LocalFileLogProcessor("logs/module_a.log"))   # 名字相对核心 = nacho.module_a
    get_logger("module_a").info("模块内日志")                              # 只进 module_a.log

名字是纯字符串，**相对核心**（``"module_a"`` 即核心名下的 ``"nacho.module_a"``，写全名也行），
日志系统内部维护「字符串名字 -> 转发列表」：同一个名字永远对应同一条列表，父模块没给它挂
出口时会警告一次，日志按默认全量输出投递，不影响程序继续跑。
"""
from __future__ import annotations

from typing import TextIO, override

from .base import BaseLogger
from .filters import LevelFilter
from .models import LogLevel
from .processors.base import BaseLogProcessor
from .processors.console import ConsoleLogProcessor
from .queue import AsyncLogQueue, OverflowPolicy


class LogCore(BaseLogger):
    """日志核心实例：默认挂控制台，其余输出通道运行时挂载。"""

    def __init__(
        self,
        name: str = "nacho",
        *,
        level: "LogLevel | str" = LogLevel.INFO,
        console: bool = True,
        console_stream: TextIO | None = None,
        console_level: "LogLevel | str | None" = None,
        console_color: bool = True,
        processors: "list[BaseLogProcessor] | None" = None,
        queue: AsyncLogQueue | None = None,
        overflow_policy: "OverflowPolicy | str" = OverflowPolicy.DROP_OLDEST,
        queue_maxsize: int = 10000,
        dispatch_batch_size: int = 200,
        dispatch_timeout: float = 0.2,
    ) -> None:
        """
        :param console: 是否默认挂一路控制台输出，默认 ``True``。
            库 / 服务端不想要任何标准输出时传 ``False``。
        :param console_stream: 控制台输出流，默认 ``sys.stdout``。
        :param console_level: 控制台最低输出级别，默认与 ``level`` 一致
            （控制台通常只给人看，可以比文件出口更粗）。它会变成一个
            :class:`~nacho.core.logger.filters.LevelFilter` 交给分发器，
            由分发器在查找分发时过滤——控制台处理机自己不做过滤。
        :param console_color: 控制台是否用 ANSI 颜色区分级别。
        :param processors: 额外要挂的输出通道，等价于构造后逐个 :meth:`attach`。
        """
        super().__init__(
            name,
            level=level,
            queue=queue,
            processors=processors,
            overflow_policy=overflow_policy,
            queue_maxsize=queue_maxsize,
            dispatch_batch_size=dispatch_batch_size,
            dispatch_timeout=dispatch_timeout,
        )
        self._console_enabled: bool = console
        if console and self.get_processor(ConsoleLogProcessor.name) is None:
            self.attach(  # pyright: ignore[reportUnusedCallResult]
                ConsoleLogProcessor(stream=console_stream, color=console_color),
                log_filter=LevelFilter(level if console_level is None else console_level),
            )

    @property
    def console_enabled(self) -> bool:
        """构造时是否要求了默认控制台输出。"""
        return self._console_enabled

    @property
    @override
    def stats(self) -> dict[str, object]:
        data = super().stats
        data["console"] = self._console_enabled
        return data


#: 进程默认核心实例，由 :func:`default_core` 懒创建
_default_core: LogCore | None = None


def current_default_core() -> LogCore | None:
    """已创建的进程默认核心；未创建时返回 ``None``（不触发创建）。"""
    return _default_core


def default_core() -> LogCore:
    """获取进程默认核心，首次调用时按默认参数创建（带控制台输出）。

    子模块不该各自新建核心实例，而应通过 :func:`attach_mount` 把输出挂到
    这个默认核心上，从而与主程序共用同一个队列与分发器。
    """
    global _default_core
    if _default_core is None:
        _default_core = LogCore()
    return _default_core


def set_default_core(core: LogCore | None) -> None:
    """替换 / 清除进程默认核心。

    注意：传入 ``None`` 只是解除引用，**不会**停止原核心——停止是异步操作，
    需要调用方自己 ``await core.stop()``。
    """
    global _default_core
    _default_core = core


def attach_mount(
    name: str,
    processor: BaseLogProcessor,
    *,
    core: LogCore | None = None,
) -> BaseLogProcessor:
    """子模块挂载自己的日志出口（模块解耦的便捷入口）。

    等价于 ``core.attach(processor, name=core.qualify(name), replace=True)``：名字**相对核心**
    （``"module_a"`` -> 核心名下的 ``"nacho.module_a"``，写全名也行），处理机写进该名字的
    转发列表，只有这个名字（及其 ``.`` 子名字）的日志会进它，因此各模块的输出文件互不混杂；
    同一个名字永远对应同一条转发列表（有则载入），重复挂载会替换同名通道（模块热重载 / 换路径）。

    :param name: 模块名字，**相对默认核心**：``"module_a"`` 收 ``module_a`` 与其
        ``.`` 子名字的日志（写成 ``"nacho.module_a"`` 这样的全名也行）。
    :param processor: 要挂载的处理机，建议先构造好再传进来。
    :param core: 挂到哪个核心实例；默认进程默认核心，见 :func:`default_core`。
    """
    target: LogCore = core if core is not None else default_core()
    return target.attach(processor, name=target.qualify(name), replace=True)
