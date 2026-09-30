"""进程默认核心与模块挂载便捷入口。

日志系统本体就是 :class:`~nacho.core.logger.base.BaseLogger`（:data:`LogCore` 是它的
别名，构造时默认带一路控制台输出）；本模块只负责**进程级那一层**：

* :func:`default_core` / :func:`current_default_core` / :func:`set_default_core`：
  进程默认核心的存取。业务模块不该各自新建核心，而应通过 :func:`mount_module`
  把自己的出口挂到这个默认核心上，从而与主程序共用同一个队列与分发器；
* :func:`mount_module`：给一个模块单独定去处（等价于发布一条具名路由）。

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

模块解耦（推荐给子模块）::

    from nacho.core.logger import get_logger, LocalFileLogProcessor
    from nacho.core.logger import mount_module

    mount_module("module_a", LocalFileLogProcessor("logs", prefix="module_a"))
    get_logger("module_a").info("模块内日志")   # 只进 module_a 那份

绑定才是常态：一段执行要把字段带上、要换去处、要定级别，都在
:meth:`~nacho.core.logger.base.BaseLogger.bind` 上做 —— 没有派生实例，也没有「先挂载
再取实例」的顺序坑。
"""
from __future__ import annotations

from .base import BaseLogger
from .models import LogLevel, Target
from .processors.base import BaseLogProcessor

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

    子模块不该各自新建核心实例，而应通过 :func:`mount_module` 把输出挂到
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
    level: "LogLevel | str | None" = None,
) -> None:
    """给一个模块单独定去处：发布一条具名路由（名字**相对默认核心**）。

    等价于给 ``core.route(name)`` 绑上这份目标并发布：那条路上的日志从此只投这里，
    不再跟着 root 的默认目标走。

    :param name: 模块名字，``"module_a"`` 即 ``"nacho.module_a"``（写全名也行）。
    :param processor: 这条路的出口。
    :param core: 挂到哪个核心；默认进程默认核心，见 :func:`default_core`。
    :param level: 出口级最低级别（落在 ``Target.level``，与 :meth:`mount` 同语义），
        低于它的记录直接跳过；``None`` = 全收。已传 :class:`Target` 则用自己的
        ``level``。
    """
    target: LogCore = core if core is not None else default_core()
    outlet: Target = (
        processor if isinstance(processor, Target) else Target(processor, level=level)
    )
    target.route(name, targets=[outlet])
