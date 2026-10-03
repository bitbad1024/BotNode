"""进程默认核心。

日志系统本体就是 :class:`~tickneko.core.logger.base.BaseLogger`（:data:`LogCore` 是它的
别名，构造时默认带一路控制台输出）；本模块只管**进程级那一层**：
:func:`default_core` / :func:`current_default_core` / :func:`set_default_core` 的存取。
业务模块不该各自新建核心，而应把出口挂到默认核心上，与主程序共用同一个队列与分发器。
分阶段启动 / 增量挂载的完整用法见 ``docs/logger/logger.md``。
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
