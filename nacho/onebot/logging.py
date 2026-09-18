"""onebot 的日志接入点。

接的是 ``nacho.core.logger`` 那套进程门面，用名字 ``onebot``（相对核心 ``nacho`` -> ``nacho.onebot``）::

    from nacho.onebot import attach_onebot_logging

    attach_onebot_logging(Path("logs/onebot.log"))   # 先挂载、再取实例（落回配置创建即冻结）

它把 :class:`~nacho.core.logger.processors.LocalFileLogProcessor` 挂到 ``onebot`` 这个名字上，
于是 onebot 层的日志单独进 ``logs/onebot.log``。onebot 是**独立进程**，不写主程序的
``logs/nacho.log`` —— 两个进程往同一个文件写会互相踩（切割 / 交错）。
"""
from __future__ import annotations

from pathlib import Path

from nacho.core.logger import BaseLogger, LocalFileLogProcessor, attach_mount, get_logger

#: onebot 层日志的名字（**相对核心**：核心名 nacho -> nacho.onebot）
ONEBOT_LOGGER_NAME: str = "onebot"


def onebot_logger(name: str = ONEBOT_LOGGER_NAME) -> BaseLogger:
    """取 onebot 层的日志实例（没 ``configure`` 过会顺带建一个默认核心）。"""
    return get_logger(name)


def attach_onebot_logging(
    path: Path | str,
    *,
    name: str = ONEBOT_LOGGER_NAME,
    buffer_size: int = 200,
    flush_interval: float = 2.0,
    max_bytes: int | None = None,
    backup_count: int = 3,
) -> BaseLogger:
    """把 onebot 的日志单独落一个文件：挂到 ``name`` 上（自层覆盖，不进核心的日志）。

    **先挂载、再取实例**：落回配置是派生那一刻复制、创建即冻结的，挂载晚了已经取出的实例
    拿不到这个出口。

    :param path: 日志文件路径；``max_bytes`` 给值时按大小切割，保留 ``backup_count`` 份历史。
    :return: 挂好出口的那个日志实例（``get_logger(name)``）。
    """
    processor = LocalFileLogProcessor(
        Path(path),
        name=f"{name}.file",
        buffer_size=buffer_size,
        flush_interval=flush_interval,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )
    attach_mount(name, processor)
    return get_logger(name)
