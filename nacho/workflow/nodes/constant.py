"""常量节点：把字面量集中放在这里，下游用 ``{{名字}}`` 引用（必须连了线才引用得到）。

为什么要有它：同一串字符串（地址、模板、固定文案）散在各个节点的 config 里，改一次要翻整张
图。写进常量节点，谁要用就连一根线过来读 —— **配置里尽量只写「怎么连」，值放在常量节点上**。

config 的形状是**一组「名字 -> 值」，直接平铺在 config 上**::

    {"base_url": "https://api.example.com", "greeting": "你好"}

即「config 里每一个键就是一个常量」。执行时把它们作为变量产出，键名即变量名；下游写
``{{base_url}}`` 就能拿到 —— 前提是那条线连上了（变量作用域校验会管，见
:mod:`nacho.workflow.validator` 的 ``VARIABLE_OUT_OF_SCOPE``）。

值默认就是字面量，但也走同一套 ``{{变量}}`` 渲染：想拿上游的值拼一个常量也行，并且校验口径
与别处一致（引用不到的变量在保存时就报错，不会等到跑起来才发现）。

常量名要写进节点的 ``outputs``（下游才引用得到）：前端画布会自动同步，手写图/脚本记得自己填。
"""
from __future__ import annotations

import re
from typing import Any

from ..models import ValidationIssue, WorkflowNode
from .base import TRIGGER_PORT, NodeExecutionContext, PortSpec, render_variables
from .registry import register_node

#: 常量名（也就是变量名）的规则：与 :func:`render_variables` 认的那套保持一致
_CONSTANT_NAME: re.Pattern[str] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_constant_node(node: WorkflowNode) -> list[ValidationIssue]:
    """至少要有一个常量，且每个键都得能当变量名用（下游靠 ``{{名字}}`` 引用）。"""
    if not node.config:
        return [
            ValidationIssue(
                node_id=node.id,
                code="MISSING_CONFIG",
                message=f"常量节点 {node.id} 一个常量都没写",
                suggestion="在 config 里写「名字: 值」，下游用 {{名字}} 引用",
            )
        ]
    issues: list[ValidationIssue] = []
    for name in node.config:
        if not _CONSTANT_NAME.match(str(name)):
            issues.append(
                ValidationIssue(
                    node_id=node.id,
                    code="INVALID_CONSTANT_NAME",
                    message=f"常量节点 {node.id} 的常量名 {name!r} 不能当变量用",
                    suggestion=(
                        "名字用字母 / 数字 / 下划线，且不能以数字开头 —— 下游是 {{名字}} 引用的"
                    ),
                )
            )
    return issues


@register_node(
    "constant",
    label="常量",
    order=30,
    inputs=[TRIGGER_PORT],
    outputs=[TRIGGER_PORT, PortSpec("message", "message", "值")],
    validator=validate_constant_node,
)
async def exec_constant(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """把 config 里的「名字 -> 值」作为变量产出（值走同一套 ``{{变量}}`` 渲染）。"""
    constants: dict[str, Any] = {
        str(name): render_variables(str(value), ctx.variables)
        for name, value in node.config.items()
    }
    # 只记名字不记值：常量里可能有令牌 / 口令这类东西，值不该进日志
    ctx.logger.debug(
        f"[constant:{node.id}] 产出常量", node_id=node.id, names=sorted(constants)
    )
    return constants
