"""日志核心实例：实例化即可用，输出通道按需增量挂载。

与 :class:`~nacho.core.logger.manager.LogManager` 的分工：

* :class:`LogCore` 是**日志系统本体**，不依赖任何全局状态，可以自由实例化
  （一个进程里想要几套互不干扰的日志系统就建几个）；构造时默认挂一路控制台
  输出，所以 :meth:`~nacho.core.logger.base.BaseLogger.start` 之后立刻就有
  可见输出，不必先把文件 / 数据库后端准备好；
* :class:`LogManager` 是**进程级门面**：管理一个默认核心，并按名字缓存子实例，
  供 ``get_logger("api.robot")`` 这类调用使用。

典型用法（分阶段启动 + 增量挂载）::

    from sqlalchemy.ext.asyncio import create_async_engine

    from nacho.core.logger import DatabaseLogProcessor, LogCore, LocalFileLogProcessor
    from nacho.db import SqlLogStore

    logger = LogCore()                          # 最小化启动：只有控制台
    await logger.start()

    db = create_async_engine("sqlite+aiosqlite:///logs/nacho.db")
    logger.attach(DatabaseLogProcessor(SqlLogStore(db)))   # 运行期挂载，自动启动
    logger.attach(LocalFileLogProcessor("logs", prefix="nacho"))   # 按天分片：nacho-<日期>.log

    logger.info("机器人已启动", robot_id="r-001")
    await logger.stop()                         # 自动冲刷余量

模块解耦（推荐给子模块）::

    from nacho.core.logger import attach_mount, get_logger, LocalFileLogProcessor

    attach_mount("module_a", LocalFileLogProcessor("logs", prefix="module_a"))   # 名字相对核心 = nacho.module_a
    get_logger("module_a").info("模块内日志")   # 只进 module_a 的片（外加控制台），不再进核心那份

名字是纯字符串，**相对核心**（``"module_a"`` 即核心名下的 ``"nacho.module_a"``，写全名也行）。
一个名字对应一个日志实例：它一旦挂了自层出口（如 ``attach_mount`` 给的出口），写日志就
**只投自层那些**，不再带上核心的文件出口——即「一个模块一处落点」；没挂自层出口时，
才整份走派生那一刻从核心复制的**落回配置**。所以 ``attach_mount`` 要**在取实例之前**调用，
模块才拿得到这个出口。
"""
from __future__ import annotations

from typing import TextIO, override

from .base import BaseLogger, LoggerStats
from .filters import LevelFilter
from .models import LogLevel
from .processors.base import BaseLogProcessor
from .processors.console import ConsoleLogProcessor
from .queue import AsyncLogQueue, OverflowPolicy


class CoreStats(LoggerStats):
    """核心实例的运行状态快照（比基类多一个控制台开关）。"""

    console: bool


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
        if console:
            # 控制台也是一个普通目标：级别判定交给挂在它上面的 LevelFilter（给出口挂一层
            # 过滤，而不是让处理机自己认级别）。装配层已经挂过控制台就不重复挂一次。
            if not any(isinstance(target.processor, ConsoleLogProcessor) for target in self.targets):
                self.mount(
                ConsoleLogProcessor(stream=console_stream, color=console_color),
                log_filter=LevelFilter(level if console_level is None else console_level),
            )

    @property
    def console_enabled(self) -> bool:
        """构造时是否要求了默认控制台输出。"""
        return self._console_enabled

    @property
    @override
    def stats(self) -> CoreStats:
        return CoreStats(**super().stats, console=self._console_enabled)


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


def mount_module(
    name: str,
    processor: "Target | BaseLogProcessor",
    *,
    core: LogCore | None = None,
) -> None:
    """给一个模块单独定去处：发布一条具名路由（名字**相对默认核心**）。

    等价于给 ``core.route(name)`` 绑上这份目标并发布：那条路上的日志从此只投这里，
    不再跟着 root 的默认目标走。

    :param name: 模块名字，``"module_a"`` 即 ``"nacho.module_a"``（写全名也行）。
    :param processor: 这条路的出口。
    :param core: 挂到哪个核心；默认进程默认核心，见 :func:`default_core`。
    """
    target: LogCore = core if core is not None else default_core()
    target.route(name, targets=[processor])
