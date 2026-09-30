"""onebot 的日志接入点。

接的是 ``nacho.core.logger`` 那套进程门面，用名字 ``onebot``（相对核心 ``nacho`` -> ``nacho.onebot``）::

    from nacho.platforms.onebot import onebot_logger

    onebot_logger().info("收到事件", post_type="message")

**不再单独落一个文件**：文件出口是整进程**一份**、按天分片的（见
:class:`~nacho.core.logger.processors.LocalFileLogProcessor`），接口层 / OneBot / 工作流的日志都进
那一份，靠记录里的 ``logger_name``（``nacho.onebot`` 这类）区分来源 —— 想只看某一路就按
``logger_name`` 检索，不必再拆文件。

**一个进程一份，前缀得各不相同**：片名由 ``[logging.file] prefix``（留空取 ``[app] name``）决定，
同机跑两个进程时它们会往**同一片**追加（行互相交错），``keep_days`` 清理时还会把对方正在写的片
当过期片删掉 —— 独立进程（例如主程序之外再单独跑 OneBot）要把 ``[app] name`` 或
``[logging.file] prefix`` 设成别的值（``nacho-onebot`` 这类）。

**业务模块一律不直接 ``default_core()``** —— 要日志实例就调 :func:`onebot_logger`；
装配层建完核心后，也可把实例通过构造参数（``logger=``）传入适配器，由模块自己的
``_log()`` 方法取用。

核心由装配层经 :func:`set_core` 存进本模块的槽位（组合根 ``nacho.bootstrap`` 或
:func:`nacho.wiring.wire_loggers` 负责）。``import`` 本模块**零副作用** —— 没装配就调用
:func:`onebot_logger` 会当场抛错（fail fast），不会默默按默认参数建一份把配置定死的核心。
"""
from __future__ import annotations

from typing import cast

from nacho.core.logger import BaseLogger

#: onebot 层日志的名字（**相对核心**：核心名 nacho -> nacho.onebot）
ONEBOT_LOGGER_NAME: str = "onebot"

#: 装配槽位：组合根建好核心后存进来；``None`` = 尚未装配
_core: BaseLogger | None = None


def set_core(core: BaseLogger | None) -> None:
    """装配日志核心（传 ``None`` 清除槽位，测试收尾用）。"""
    global _core
    _core = core


def onebot_logger(name: str = ONEBOT_LOGGER_NAME) -> BaseLogger:
    """取 onebot 层的日志实例；未装配时抛错（见模块文档）。

    ``child()`` 返回的是具体的 :class:`~nacho.core.logger.ChildLogger`，这里收窄成
    ``BaseLogger`` 接口对外 —— 与各构造参数的 ``logger: BaseLogger | None`` 约定一致。
    """
    if _core is None:
        raise RuntimeError(
            "onebot_logger 尚未装配：先调 nacho.wiring.wire_loggers(core) 或 "
            "nacho.platforms.onebot.logging.set_core(core)"
        )
    return cast(BaseLogger, cast(object, _core.child(name)))
