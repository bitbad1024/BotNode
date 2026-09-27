"""日志过滤器。

过滤器**不属于处理机**。处理机的职责只有两件事：把收到的日志落地
（:meth:`~nacho.core.logger.processors.base.BaseLogProcessor.write`）与检索
（:meth:`~nacho.core.logger.processors.base.BaseLogProcessor.search`）；至于
「哪条日志该不该投给哪个出口」，是**分发器在查找分发时**做的判断——分发器持有
每个出口的过滤器，一条日志只有通过某个出口的过滤器，才会被投递给它。

这样过滤器与处理机彻底解耦：

* 处理机不再认识「级别 / ``extra`` / 模块」等业务过滤条件，只管落地；
* 同一个过滤器能挂到任意出口上，同一个处理机也能配不同过滤器；
* 过滤发生在**日志进入处理机之前**，被过滤掉的日志连处理机的缓冲区都不进。

用法（挂载出口时把过滤器交给日志系统，而不是写进处理机）::

    from nacho.core.logger import LogCore, ConsoleLogProcessor, LevelFilter

    core = LogCore(console=False)
    core.mount(ConsoleLogProcessor(), log_filter=LevelFilter("WARNING"))
    # 从此控制台只收 WARNING 及以上；分发器在路由时就把它过滤掉了
"""
from __future__ import annotations

import abc
from typing import override

from .models import LogLevel, LogRecord


class LogFilter(abc.ABC):
    """日志过滤器：分发器在**查找分发目标**时调用。

    子类只需实现 :meth:`match`：返回 ``True`` 表示这条日志允许投给挂着本过滤器的
    出口。过滤器由 :meth:`~nacho.core.logger.base.BaseLogger.mount` 的 ``log_filter``
    参数（或 :class:`~nacho.core.logger.models.Target` 上的那个）交给日志系统，
    分发时按它筛。
    """

    @abc.abstractmethod
    def match(self, record: LogRecord) -> bool:
        """这条日志是否允许投给该出口。"""

    def __call__(self, record: LogRecord) -> bool:
        """让过滤器本身可调用，等价于 :meth:`match`。"""
        return self.match(record)

    @override
    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"


class DenyAllFilter(LogFilter):
    """全拒过滤器：一条都不放行。

    单独一个类而不是拿 ``LevelFilter("CRITICAL")`` 凑数：语义是「这个出口对这份实例
    关闸」，与级别无关——就算出现比 CRITICAL 更高的级别也照拒。典型用途是
    :meth:`~nacho.core.logger.base.BaseLogger.mute`：把继承来的全局留存出口
    （如落库）在**某一路**日志上堵住，别的路照常投递。
    """

    @override
    def match(self, record: LogRecord) -> bool:
        return False

    @override
    def __repr__(self) -> str:
        return "<DenyAllFilter>"


#: 进程级共享的「全拒」单例（无状态，谁的 ``mute`` 都用它）
DENY_ALL: DenyAllFilter = DenyAllFilter()


class LevelFilter(LogFilter):
    """按最低级别过滤：只放行 ``record.level >= level`` 的日志。

    典型用途是「控制台只给粗粒度、文件全量」：同一个 :class:`LogCore` 上，
    给控制台出口挂 ``LevelFilter("WARNING")``，给文件出口不加过滤器即可。
    """

    def __init__(self, level: "LogLevel | str") -> None:
        self._level: LogLevel = LogLevel.parse(level)

    @property
    def level(self) -> LogLevel:
        """放行的最低级别。"""
        return self._level

    @override
    def match(self, record: LogRecord) -> bool:
        return record.level >= self._level

    @override
    def __repr__(self) -> str:
        return f"<LevelFilter level={self._level.name}>"
