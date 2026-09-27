"""onebot 的日志接入点。

接的是 ``nacho.core.logger`` 那套进程门面，用名字 ``onebot``（相对核心 ``nacho`` -> ``nacho.onebot``）::

    from nacho.onebot import onebot_logger

    onebot_logger().info("收到事件", post_type="message")

**不再单独落一个文件**：文件出口是整进程**一份**、按天分片的（见
:class:`~nacho.core.logger.processors.LocalFileLogProcessor`），接口层 / OneBot / 工作流的日志都进
那一份，靠记录里的 ``logger_name``（``nacho.onebot`` 这类）区分来源 —— 想只看某一路就按
``logger_name`` 检索，不必再拆文件。

**一个进程一份，前缀得各不相同**：片名由 ``[logging.file] prefix``（留空取 ``[app] name``）决定，
同机跑两个进程时它们会往**同一片**追加（行互相交错），``keep_days`` 清理时还会把对方正在写的片
当过期片删掉 —— 独立进程（例如主程序之外再单独跑 OneBot）要把 ``[app] name`` 或
``[logging.file] prefix`` 设成别的值（``nacho-onebot`` 这类）。
"""
from __future__ import annotations

from nacho.core.logger import BaseLogger, get_logger

#: onebot 层日志的名字（**相对核心**：核心名 nacho -> nacho.onebot）
ONEBOT_LOGGER_NAME: str = "onebot"


def onebot_logger(name: str = ONEBOT_LOGGER_NAME) -> BaseLogger:
    """取 onebot 层的日志实例（没 ``configure`` 过会顺带建一个默认核心）。"""
    return get_logger(name)
