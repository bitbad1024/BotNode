"""kook 的日志接入点。

接的是 ``nacho.core.logger`` 那套进程门面，用名字 ``kook``（相对核心 ``nacho`` ->
``nacho.kook``）::

    from nacho.platforms.kook import kook_logger

    kook_logger().info("已连接网关", gateway="wss://www.kookapp.cn/gateway")

与 OneBot / 接口层同口径：日志进整进程共用的一份文件，靠 ``logger_name``（``nacho.kook``）
区分来源。
"""
from __future__ import annotations

from nacho.core.logger import BaseLogger, default_core

#: kook 层日志的名字（**相对核心**：核心名 nacho -> nacho.kook）
KOOK_LOGGER_NAME: str = "kook"


def kook_logger(name: str = KOOK_LOGGER_NAME) -> BaseLogger:
    """取 kook 层的日志实例（没 ``configure`` 过会顺带建一个默认核心）。"""
    return default_core().child(name)
