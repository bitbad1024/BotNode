"""写一个节点要用到的东西：执行函数的形状 + 它的运行时上下文 + 模板渲染 + 注册规格。

**这一份是对外契约**：别人写自己的节点时只从这里（以及 :mod:`.registry`）import，
不需要碰框架里别的文件::

    from nacho.workflow.nodes import (
        NodeExecutionContext, ConfigField, register_node, render_variables,
    )

    @register_node(
        "dingtalk",
        # 必填字段：缺失 / 空串在校验阶段直接报错
        fields=[ConfigField("text", "消息内容", required=True)],
    )
    async def exec_dingtalk(node, ctx: NodeExecutionContext) -> dict[str, object]:
        text = render_variables(str(node.config.get("text", "")), ctx.variables)
        ctx.logger.info("发钉钉消息", node_id=node.id, text=text)
        return {"sent": True}      # 产出给下游 {{sent}} 引用

函数签名就是 :data:`NodeExecutor`：收「节点 + 上下文」，返回**本节点产出的变量**（会被
合并进 ``ctx.variables``，下游用 ``{{名字}}`` 引用）。

**校验什么由注册方自己说了算**：必填字段 / 默认值通过 :class:`ConfigField` 声明，
表格化覆盖不了的规则（枚举、条件必填）写一个 :data:`NodeConfigValidator` 挂上来，
不用改校验器框架代码。
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from nacho.core.logger import BaseLogger, get_logger
from nacho.core.scheduler import TaskManager

from ..models import ValidationIssue, WorkflowNode

#: 节点执行函数：(节点, 上下文) -> 该节点的输出变量 dict
NodeExecutor = Callable[[WorkflowNode, "NodeExecutionContext"], Awaitable[dict[str, Any]]]

#: 节点在图中的拓扑角色：start=唯一入口 / end=终点 / normal=普通节点
NodeRole = Literal["start", "end", "normal"]

#: 节点配置校验器：收节点，返回校验问题列表（空列表 = 通过）
NodeConfigValidator = Callable[[WorkflowNode], list[ValidationIssue]]

#: 「字段没有声明默认值」的哨兵（None 也是合法默认值，不能拿 None 当缺省标记）
MISSING_DEFAULT: Any = object()


@dataclass(frozen=True)
class ConfigField:
    """节点 ``config`` 里的一个字段声明：必填规则与默认值在注册时定死。

    * ``required=True``：缺失 / None / 空白字符串 → 校验直接报 ``MISSING_CONFIG``；
    * 给了 ``default``：缺失（键不存在或值为 None）时由
      :func:`nacho.workflow.validator.apply_config_defaults` 在保存版本时填默认值；
    * 两个都不给：纯可选字段，校验器不碰。
    """

    name: str
    label: str = ""
    required: bool = False
    default: Any = MISSING_DEFAULT


@dataclass(frozen=True)
class NodeSpec:
    """一种节点类型的完整注册规格：执行器 + 配置字段规则 + 拓扑约束。

    :param node_type: 类型名（节点 JSON 的 ``type``）；
    :param executor: 执行函数；声明了但执行器还没实现时为 None（跑到它才报暂无执行器）；
    :param fields: :class:`ConfigField` 清单，必填 / 默认值都从这里推导；
    :param validator: 自定义配置校验器（枚举、条件必填这类表格盖不住的规则）；
    :param role: 拓扑角色，start 全图唯一、end 至少一个可达；
    :param min_outgoing: 出边条数下限（分流类节点要 ≥2）；
    :param max_outgoing: 出边条数上限（end 为 0），None 不限；
    :param expression_field: 该字段内容要交图级表达式语法检查器过一遍（expression 节点用）。
    """

    node_type: str
    executor: NodeExecutor | None = None
    fields: tuple[ConfigField, ...] = ()
    validator: NodeConfigValidator | None = None
    role: NodeRole = "normal"
    min_outgoing: int = 0
    max_outgoing: int | None = None
    expression_field: str | None = None

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

    ``variables`` 是**属性**不是入参：构造时为空，随节点执行不断合并上游产出（要预置变量
    直接写 ``ctx.variables["x"] = ...``，测试里常这么干）。

    :param logger: 业务日志实例（log 节点写这里）；
    :param scheduler: 调度器（时间触发的 start 节点把流程图登记到这里）；
    :param run: 触发整条流程的回调，cron 到点时调用。
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
        """cron 到点时触发整条流程的回调。"""
        if self._run is not None:
            await self._run()
