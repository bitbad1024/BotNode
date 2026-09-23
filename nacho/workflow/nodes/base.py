"""写一个节点要用到的东西：执行函数的形状 + 它的运行时上下文 + 模板渲染。

**这一份是对外契约**：别人写自己的节点时只从这里（以及 :mod:`.registry`）import，
不需要碰框架里别的文件::

    from nacho.workflow.nodes import NodeExecutionContext, register_node, render_variables

    @register_node("dingtalk")
    async def exec_dingtalk(node, ctx: NodeExecutionContext) -> dict[str, object]:
        text = render_variables(str(node.config.get("text", "")), ctx.variables)
        ctx.logger.info("发钉钉消息", node_id=node.id, text=text)
        return {"sent": True}      # 产出给下游 {{sent}} 引用

函数签名就是 :data:`NodeExecutor`：收「节点 + 上下文」，返回**本节点产出的变量**（会被
合并进 ``ctx.variables``，下游用 ``{{名字}}`` 引用）。
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from nacho.core.logger import BaseLogger, get_logger
from nacho.core.scheduler import TaskManager

from ..models import WorkflowNode

#: 节点执行函数：(节点, 上下文) -> 该节点的输出变量 dict
NodeExecutor = Callable[[WorkflowNode, "NodeExecutionContext"], Awaitable[dict[str, Any]]]

#: 配置字符串里的变量引用：``{{ name }}``
_VARIABLE_RE: re.Pattern[str] = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def render_variables(template: str, variables: dict[str, Any]) -> str:
    """把模板里的 ``{{name}}`` 换成变量值（变量不存在就留空串）。

    节点配置里凡是让用户填文本的地方都走它，口径才一致：只有字母 / 下划线开头、后面是
    字母数字下划线的名字算变量，其余原样保留。
    """

    def sub(match: re.Match[str]) -> str:
        return str(variables.get(match.group(1), ""))

    return _VARIABLE_RE.sub(sub, template)


class NodeExecutionContext:
    """节点运行时上下文：上游输出变量 + 日志 + 调度器。

    :param variables: 累积的变量上下文（上游节点的 outputs 合并进来）；
    :param logger: 业务日志实例（log 节点写这里）；
    :param scheduler: 调度器（time-trigger 节点把流程图登记到这里）；
    :param run: 触发整条流程的回调，time-trigger 到点时调用。
    """

    def __init__(
        self,
        *,
        logger: BaseLogger | None = None,
        scheduler: TaskManager | None = None,
        run: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.variables: dict[str, Any] = {}
        self.log: list[str] = []  # 节点产出的文字日志（供测试 / 前端回显）
        self._logger: BaseLogger = logger if logger is not None else get_logger("workflow")
        self._scheduler: TaskManager | None = scheduler
        self._run: Callable[[], Awaitable[None]] | None = run

    @property
    def logger(self) -> BaseLogger:
        return self._logger

    @property
    def scheduler(self) -> TaskManager | None:
        return self._scheduler

    async def run_workflow(self) -> None:
        """time-trigger 到点时触发整条流程的回调。"""
        if self._run is not None:
            await self._run()
