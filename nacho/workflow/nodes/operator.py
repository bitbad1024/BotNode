"""运算节点：对两个操作数做一次算术 —— ``left 运算符 right``，结果从 ``operator_result`` 送下去。

config:
    left:     左值 —— **没接线时**的手填值（接线优先，字段名与端口同名）
    operator: 运算符（缺省 ``+``）：``+`` 加 / ``-`` 减 / ``*`` 乘 / ``/`` 除 / ``%`` 取余
    right:    右值（手填兜底，也能接线）

端口（数据沿连线走）：

    ``left`` / ``right``   数据入口：两个操作数（比如「数量 × 单价」里的数量）；
    ``operator_result``    运算结果（出口）：**文本形式**，整数值不带小数点。

口径：

* 两边都**转成数字**再算（整数 / 小数都认）；``/`` 是真除法（``7 / 2 = 3.5``），
  ``%`` 取余按 Python 语义（结果符号跟随除数）；
* 结果文本化：整数值（``6 / 2`` → ``3``）不带小数点，其余按最短往返表示
  （``0.1 + 0.2`` 就是 ``0.30000000000000004`` —— 不做「善意」四舍五入）；
* **算不出来 = 业务失败**：左 / 右值为空、非数字、除数为 0、结果溢出、运算符不合法
  （没走过校验的图）→ 抛 :class:`~nacho.workflow.nodes.base.NodeFailure`：引擎**停止它
  向下传播**（下游整段跳过），不再往下送空串 —— 空串会让下游拿着「没算出来」的结果
  继续跑（比如把空内容发出去）。

小抄::

    数量×单价: left="{{count}}" operator="*" right="{{price}}"
    奇偶分流:   left="{{now_ts}}" operator="%" right="2"    # 结果 0 = 偶数
"""
from __future__ import annotations

import math
from typing import Any, NoReturn

from ..models import ValidationIssue, WorkflowNode
from .base import (
    TRIGGER_PORT,
    ConfigField,
    NodeExecutionContext,
    NodeFailure,
    PortSpec,
    input_value,
)
from .registry import register_node

#: 允许的运算符，**顺序即画布下拉顺序**
OPERATOR_ORDER: tuple[str, ...] = ("+", "-", "*", "/", "%")

#: 日志里展示「不像数字的那些值」时的截断长度（避免一段长文本刷屏）
CLIP_CHARS: int = 40


def validate_operator_node(node: WorkflowNode) -> list[ValidationIssue]:
    """运算符必须是枚举里的一个（拼错保存时就拦，运行时不猜）。"""
    symbol = node.config.get("operator")
    if isinstance(symbol, str) and symbol.strip() and symbol.strip() not in OPERATOR_ORDER:
        return [
            ValidationIssue(
                node_id=node.id,
                code="INVALID_OPERATOR_SYMBOL",
                message=f"operator 节点 {node.id} 的运算符 {symbol!r} 不合法",
                suggestion=f"可选：{' '.join(OPERATOR_ORDER)}",
            )
        ]
    return []


def _number_of(raw: str) -> float | None:
    """文本转数字；空 / 非数字 / ``inf`` / ``nan`` 都算「没拿到」。"""
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _apply(symbol: str, left: float, right: float) -> float:
    """做一次算术（合法性 / 除零由调用方先查）。"""
    if symbol == "+":
        return left + right
    if symbol == "-":
        return left - right
    if symbol == "*":
        return left * right
    if symbol == "/":
        return left / right
    return left % right


def _stringify(value: float) -> str:
    """数字文本化：整数值不带小数点（``6 / 2`` → ``3``），其余按最短往返表示。"""
    if value.is_integer():
        return str(int(value))
    return str(value)


def _clip(raw: str) -> str:
    """日志里展示的值：太长截断（避免一段长文本刷屏）。"""
    return raw if len(raw) <= CLIP_CHARS else raw[:CLIP_CHARS] + "…"


def _fail(ctx: NodeExecutionContext, node_id: str, reason: str) -> NoReturn:
    """算不出来：抛业务失败（引擎停止它向下传播，见 :class:`NodeFailure`）。"""
    ctx.logger.warning(f"[operator:{node_id}] 算不出来（{reason}）")
    ctx.log.append(f"[operator] {node_id}: 算不出来（{reason}）")
    raise NodeFailure(f"算不出来（{reason}）")


@register_node(
    "operator",
    label="运算",
    order=130,
    category="data",
    # left / right 既是字段名也是数据入口（同 http.url）：接线或手填都行
    inputs=[
        TRIGGER_PORT,
        PortSpec("left", "message", "左值", required=True),
        PortSpec("right", "message", "右值", required=True),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("operator_result", "message", "运算结果"),
    ],
    fields=[
        # 两个操作数的「必填」由入口（PortSpec.required）管；这里只声明手填兜底
        ConfigField("left", "左值"),
        ConfigField("operator", "运算符", default="+", options=OPERATOR_ORDER),
        ConfigField("right", "右值"),
    ],
    validator=validate_operator_node,
)
async def exec_operator(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """算一次 ``left 运算符 right``，把结果文本送 ``operator_result``。"""
    symbol = str(node.config.get("operator", "")).strip() or "+"
    if symbol not in OPERATOR_ORDER:
        # 保存时会被校验拦住；这里兜住「没走过校验」的图
        _fail(ctx, node.id, f"运算符 {symbol!r} 不合法")

    left_raw = str(input_value(node, ctx, "left", default="")).strip()
    if not left_raw:
        _fail(ctx, node.id, "左值为空（上游没送值或送了空串）")
    left = _number_of(left_raw)
    if left is None:
        _fail(ctx, node.id, f"左值 {_clip(left_raw)!r} 不是数字")

    right_raw = str(input_value(node, ctx, "right", default="")).strip()
    if not right_raw:
        _fail(ctx, node.id, "右值为空（没接线也没手填）")
    right = _number_of(right_raw)
    if right is None:
        _fail(ctx, node.id, f"右值 {_clip(right_raw)!r} 不是数字")

    if symbol in ("/", "%") and right == 0:
        _fail(ctx, node.id, f"除数为 0（{symbol} 算不了）")

    result = _apply(symbol, left, right)
    if not math.isfinite(result):
        _fail(ctx, node.id, "结果超出数字范围")

    text = _stringify(result)
    ctx.logger.info(f"[operator:{node.id}] {left_raw} {symbol} {right_raw} -> {text}")
    ctx.log.append(f"[operator] {node.id}: {left_raw} {symbol} {right_raw} -> {text}")
    return {"operator_result": text}
