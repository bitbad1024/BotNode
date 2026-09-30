"""bridge（平台总线）的日志接入点。

接的是 ``nacho.core.logger`` 那套进程门面，用名字 ``bridge``（相对核心 ``nacho`` ->
``nacho.bridge``）:::

    from nacho.platforms.bridge import bridge_logger

    bridge_logger().info("网关已就绪")

**业务模块一律不直接 ``default_core()``** —— 要日志实例就调 :func:`bridge_logger`；
装配层建完核心后，也可把实例通过构造参数（``logger=``）传入各适配器 / 网关，
由模块自己的 ``_log()`` 方法取用。
"""
from __future__ import annotations

from nacho.core.logger import BaseLogger, default_core

#: bridge 层日志的名字（**相对核心**：核心名 nacho -> nacho.bridge）
BRIDGE_LOGGER_NAME: str = "bridge"


def bridge_logger(name: str = BRIDGE_LOGGER_NAME) -> BaseLogger:
    """取 bridge 层的日志实例（没 ``configure`` 过会顺带建一个默认核心）。"""
    node = default_core().child(name)
    return node
