"""条件节点：比一次 ``left operator right``，二选一走 ``true`` / ``false`` 两个出口。

这是**分流节点**（注册时 ``branching=True``）：执行后引擎把「没选中的出口」的所有出边
置死，对岸的节点整段跳过（级联到它的下游），直到和另一条分支汇合（有活入边就照常执行）。
没走的分支在 ``ctx.log`` 里留下 ``[skip]`` 痕迹 —— 排查「后面为什么没跑」先看它。

config:
    left:     左值 —— **没接线时**的手填值（接线优先，字段名与端口同名）
    operator: 比较符（缺省 ``==``）：``==`` / ``!=`` / ``>`` / ``>=`` / ``<`` / ``<=`` /
              ``contains`` / ``not_contains``
    right:    右值（手填兜底，也能接线）

端口（数据沿连线走）：

    ``left``   数据入口：左值从上游来（比如 ``http`` 的 ``http_status``）；
    ``right``  数据入口：右值也能接线；
    ``true``   出口（触发）：条件成立时走这边；
    ``false``  出口（触发）：不成立时走这边。

比较口径：

* ``==`` / ``!=``：两边转成文本比；
* ``>`` / ``>=`` / ``<`` / ``<=``：**要数字** —— 两边都转得成数字才比，不然记 warning
  走 ``false``（不猜「字符串字典序」这种容易反直觉的结果）；
* ``contains`` / ``not_contains``：子串包含（文本；右值不能为空）。

拿不到值不算事故（与 ``json`` 节点同一口径）：左值为空、要数字却是文本、右值为空 →
记 warning 走 ``false``，不打断流程。

小抄：

    数字门槛: left={{http_status}} operator=">="  right="400"
    文本开关: left={{message}}     operator="contains" right="开播"
"""
from __future__ import annotations

from typing import Any

from ..models import ValidationIssue, WorkflowNode
from .base import TRIGGER_PORT, ConfigField, NodeExecutionContext, PortSpec, input_value
from .registry import register_node

#: 允许的比较符，**顺序即画布下拉顺序**
CONDITION_OPERATOR_ORDER: tuple[str, ...] = (
    "==",
    "!=",
    ">",
    ">=",
    "<",
    "<=",
    "contains",
    "not_contains",
)

#: 要数字的比较符（两边都转得成数字才比）与要非空右值的比较符
NUMERIC_OPERATORS: frozenset[str] = frozenset({">", ">=", "<", "<="})
CONTAINS_OPERATORS: frozenset[str] = frozenset({"contains", "not_contains"})


def validate_condition_node(node: WorkflowNode) -> list[ValidationIssue]:
    """比较符必须是枚举里的一个（拼错保存时就拦，运行时不猜）。"""
    operator = node.config.get("operator")
    if isinstance(operator, str) and operator.strip() and operator.strip() not in CONDITION_OPERATOR_ORDER:
        return [
            ValidationIssue(
                node_id=node.id,
                code="INVALID_CONDITION_OPERATOR",
                message=f"condition 节点 {node.id} 的比较符 {operator!r} 不合法",
                suggestion=f"可选：{' '.join(CONDITION_OPERATOR_ORDER)}",
            )
        ]
    return []


def _to_number(raw: str) -> float | None:
    """转数字；转不了返回 None（要不要数字由比较符决定）。"""
    try:
        return float(raw.strip())
    except ValueError:
        return None


def _compare(left: str, operator: str, right: str) -> bool | None:
    """执行一次比较；``None`` = 数值比较符遇到了非数字（调用方记 warning 走 false）。"""
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    if operator == "contains":
        return right in left
    if operator == "not_contains":
        return right not in left
    # 剩下的 > >= < <=：要数字（合法性由调用方先过一遍）
    left_num = _to_number(left)
    right_num = _to_number(right)
    if left_num is None or right_num is None:
        return None
    if operator == ">":
        return left_num > right_num
    if operator == ">=":
        return left_num >= right_num
    if operator == "<":
        return left_num < right_num
    return left_num <= right_num


@register_node(
    "condition",
    label="条件",
    order=110,
    category="control",
    # 分流节点：引擎按「选中了哪个出口」剪枝，没走的出口整段跳过
    branching=True,
    # 至少接一个出口：一个分支都不接的条件没有意义
    min_outgoing=1,
    inputs=[
        TRIGGER_PORT,
        PortSpec("left", "message", "左值", required=True),
        PortSpec("right", "message", "右值"),
    ],
    outputs=[
        PortSpec("true", "trigger", "满足"),
        PortSpec("false", "trigger", "不满足"),
    ],
    fields=[
        ConfigField("left", "左值"),
        ConfigField("operator", "比较符", default="==", options=CONDITION_OPERATOR_ORDER),
        ConfigField("right", "右值", default=""),
    ],
    validator=validate_condition_node,
)
async def exec_condition(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """比一次 ``left operator right``，只给选中的出口产出（引擎据此剪枝）。"""
    operator = str(node.config.get("operator", "")).strip() or "=="
    left = str(input_value(node, ctx, "left", default=""))
    right = str(input_value(node, ctx, "right", default=""))

    if operator not in CONDITION_OPERATOR_ORDER:
        # 保存时会被校验拦住；这里兜住「没走过校验」的图
        ctx.logger.warning(f"[condition:{node.id}] 比较符 {operator!r} 不合法，按不满足处理")
        ctx.log.append(f"[condition] {node.id}: 比较符不合法 -> false")
        return {"false": True}

    if not left:
        ctx.logger.warning(f"[condition:{node.id}] 左值为空（上游没送值或送了空串）")
        ctx.log.append(f"[condition] {node.id}: 左值为空 -> false")
        return {"false": True}

    if operator in CONTAINS_OPERATORS and not right:
        ctx.logger.warning(f"[condition:{node.id}] {operator} 的右值为空（没接线也没手填）")
        ctx.log.append(f"[condition] {node.id}: 右值为空 -> false")
        return {"false": True}

    outcome = _compare(left, operator, right)
    if outcome is None:
        ctx.logger.warning(
            f"[condition:{node.id}] {operator!r} 要数字：{left!r} / {right!r} 转不了数字，按不满足处理"
        )
        ctx.log.append(f"[condition] {node.id}: 非数字比较 -> false")
        return {"false": True}

    shown = "true" if outcome else "false"
    ctx.logger.info(f"[condition:{node.id}] {left} {operator} {right} -> {shown}")
    ctx.log.append(f"[condition] {node.id}: {left} {operator} {right} -> {shown}")
    return {shown: True}
