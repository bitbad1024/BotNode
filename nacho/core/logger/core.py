"""进程默认核心。

日志系统本体就是 :class:`~nacho.core.logger.base.BaseLogger`（:data:`LogCore` 是它的
别名，构造时默认带一路控制台输出）；本模块只负责**进程级那一层**：
:func:`default_core` / :func:`current_default_core` / :func:`set_default_core` ——
进程默认核心的存取。业务模块不该各自新建核心，而应把出口挂到这个默认核心上，
从而与主程序共用同一个队列与分发器。

典型用法（分阶段启动 + 增量挂载）::

    from sqlalchemy.ext.asyncio import create_async_engine

    from nacho.core.logger import DatabaseLogProcessor, LogCore, LocalFileLogProcessor
    from nacho.db import SqlLogStore

    logger = LogCore()                          # 最小化启动：只有控制台
    await logger.start()

    db = create_async_engine("sqlite+aiosqlite:///logs/nacho.db")
    logger.mount(DatabaseLogProcessor(SqlLogStore(db)))   # 运行期挂载，自动启动
    logger.mount(LocalFileLogProcessor("logs", prefix="nacho"))   # 按天分片：nacho-<日期>.log

    logger.info("机器人已启动", robot_id="r-001")
    await logger.stop()                         # 自动冲刷余量

取日志实例只有两条路：:meth:`~nacho.core.logger.base.BaseLogger.child`（命名层级、
同名缓存）与 :meth:`~nacho.core.logger.base.BaseLogger.bind`（上下文视图）—— 没有
按名取回、没有发布登记。
"""
from __future__ import annotations

from .base import BaseLogger

#: 进程默认核心实例，由 :func:`default_core` 懒创建
_default_core: BaseLogger | None = None

#: 日志系统本体（默认带一路控制台）。曾经它是一个带控制台的子类，如今构造函数把控制台
#: 收进本体，这里留作别名，老代码 ``LogCore(...)`` 照旧可用。
LogCore = BaseLogger


def current_default_core() -> LogCore | None:
    """已创建的进程默认核心；未创建时返回 ``None``（不触发创建）。"""
    return _default_core


def default_core() -> LogCore:
    """获取进程默认核心，首次调用时按默认参数创建（带控制台输出）。

    子模块不该各自新建核心实例，而应把出口挂到这个默认核心上，从而与主程序共用
    同一个队列与分发器。
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
