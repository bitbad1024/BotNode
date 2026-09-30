"""workflow 的日志接入点。

接的是 ``nacho.core.logger`` 那套进程门面，用名字 ``workflow``（相对核心 ``nacho`` ->
``nacho.workflow``）:::

    from nacho.workflow import workflow_logger

    workflow_logger().info("工作流已登记", workflow_id=...)

**业务模块一律不直接 ``default_core()``** —— 要日志实例就调 :func:`workflow_logger`；
``runtime.py`` 里的模块级 ``_log()`` 与节点上下文的 ``logger`` 都走这一口。
"""
from __future__ import annotations

from nacho.core.logger import BaseLogger, default_core

#: workflow 层日志的名字（**相对核心**：核心名 nacho -> nacho.workflow）
WORKFLOW_LOGGER_NAME: str = "workflow"


def workflow_logger(name: str = WORKFLOW_LOGGER_NAME) -> BaseLogger:
    """取 workflow 层的日志实例（没 ``configure`` 过会顺带建一个默认核心）。"""
    return default_core().child(name)
