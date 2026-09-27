"""日志处理机：日志的最终落地端。

每种处理机负责一种落地方案，由日志系统统一调度、隔离与统计：

* :class:`ConsoleLogProcessor`：控制台（最小化启动的默认出口）；
* :class:`LocalFileLogProcessor`：本地文件（JSON Lines，**按天分片**、片内按时间 / 大小换片）；
* :class:`DatabaseLogProcessor`：数据库（依赖
  :class:`~nacho.core.logger.interfaces.LogStore` 协议，落地实现在 ``nacho.db``）。

自定义处理机只需继承 :class:`BaseLogProcessor` 并实现 ``write`` 与 ``search``；
处理机**不做路由也不做过滤**——路由由日志实例自己那份配置副本负责
（见 :meth:`~nacho.core.logger.base.BaseLogger.child`），内容过滤由分发器持有的
:class:`~nacho.core.logger.filters.LogFilter` 在查找分发时完成（挂载出口时通过
``log_filter`` 传入，见 :meth:`~nacho.core.logger.base.BaseLogger.mount`）。
"""

from .base import BaseLogProcessor
from .console import ConsoleLogProcessor
from .database import DatabaseLogProcessor
from .local import LocalFileLogProcessor

__all__ = [
    "BaseLogProcessor",
    "ConsoleLogProcessor",
    "DatabaseLogProcessor",
    "LocalFileLogProcessor",
]
