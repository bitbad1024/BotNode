"""workflow 的日志接入点。

接的是 ``nacho.core.logger`` 那套进程门面，用名字 ``workflow``（相对核心 ``nacho`` ->
``nacho.workflow``）：``workflow_logger().info("工作流已登记", workflow_id=...)``。
**业务模块一律不直接 ``default_core()``** —— ``runtime.py`` 的 ``_log()`` 与节点上下文的
``logger`` 都走这一口。核心由装配层 :func:`set_core` 存进本模块槽位；``import`` 零副作用，
没装配就调用会当场抛错（fail fast），不会默默按默认参数建一份把配置定死的核心。
日志实例**用到才取**（模块级取会把默认核心定死）。详见 ``docs/workflow/workflow.md`` 第 7.6 节。
"""
from __future__ import annotations

from typing import cast

from nacho.core.logger import BaseLogger

#: workflow 层日志的名字（**相对核心**：核心名 nacho -> nacho.workflow）
WORKFLOW_LOGGER_NAME: str = "workflow"

#: 装配槽位：组合根建好核心后存进来；``None`` = 尚未装配
_core: BaseLogger | None = None


def set_core(core: BaseLogger | None) -> None:
    """装配日志核心（传 ``None`` 清除槽位，测试收尾用）。"""
    global _core
    _core = core


def workflow_logger(name: str = WORKFLOW_LOGGER_NAME) -> BaseLogger:
    """取 workflow 层的日志实例；未装配时抛错（见模块文档）。

    ``child()`` 返回的是具体的 :class:`~nacho.core.logger.ChildLogger`，这里收窄成
    ``BaseLogger`` 接口对外 —— 与各构造参数的 ``logger: BaseLogger | None`` 约定一致。
    """
    if _core is None:
        raise RuntimeError(
            "workflow_logger 尚未装配：先调 nacho.wiring.wire_loggers(core) 或 "
            "nacho.workflow.logging.set_core(core)"
        )
    return cast(BaseLogger, cast(object, _core.child(name)))
