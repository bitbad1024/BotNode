"""bridge（平台总线）的日志接入点。

接的是 ``nacho.core.logger`` 那套进程门面，用名字 ``bridge``（相对核心 ``nacho`` ->
``nacho.bridge``）:::

    from nacho.platforms.bridge import bridge_logger

    bridge_logger().info("网关已就绪")

**业务模块一律不直接 ``default_core()``** —— 要日志实例就调 :func:`bridge_logger`；
装配层建完核心后，也可把实例通过构造参数（``logger=``）传入各适配器 / 网关，
由模块自己的 ``_log()`` 方法取用。

核心由装配层经 :func:`set_core` 存进本模块的槽位（组合根 ``nacho.bootstrap`` 或
:func:`nacho.wiring.wire_loggers` 负责）。``import`` 本模块**零副作用** —— 没装配就调用
:func:`bridge_logger` 会当场抛错（fail fast），不会默默按默认参数建一份把配置定死的核心。
"""
from __future__ import annotations

from typing import cast

from nacho.core.logger import BaseLogger

#: bridge 层日志的名字（**相对核心**：核心名 nacho -> nacho.bridge）
BRIDGE_LOGGER_NAME: str = "bridge"

#: 装配槽位：组合根建好核心后存进来；``None`` = 尚未装配
_core: BaseLogger | None = None


def set_core(core: BaseLogger | None) -> None:
    """装配日志核心（传 ``None`` 清除槽位，测试收尾用）。"""
    global _core
    _core = core


def bridge_logger(name: str = BRIDGE_LOGGER_NAME) -> BaseLogger:
    """取 bridge 层的日志实例；未装配时抛错（见模块文档）。

    ``child()`` 返回的是具体的 :class:`~nacho.core.logger.ChildLogger`，这里收窄成
    ``BaseLogger`` 接口对外 —— 与各构造参数的 ``logger: BaseLogger | None`` 约定一致。
    """
    if _core is None:
        raise RuntimeError(
            "bridge_logger 尚未装配：先调 nacho.wiring.wire_loggers(core) 或 "
            "nacho.platforms.bridge.logging.set_core(core)"
        )
    return cast(BaseLogger, cast(object, _core.child(name)))
