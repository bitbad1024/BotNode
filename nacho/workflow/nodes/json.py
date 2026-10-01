"""JSON 节点：把 JSON 文本解析出来、按点路径取值（**HTTP 节点的天然搭档**）。

典型用法：一段接口响应从 ``http_body`` 送到这里，取 ``data.user.name`` 再拿去拼消息 ——
「从响应里扒一个字段」不用每次都写一段代码。

config:
    json: JSON 文本 —— **没接线时**的手填值（接线优先，字段名与端口同名）
    path: 取值路径（点分段；数字段按数组下标解释，负数从后往前数），留空 = 取整个文档

端口（数据沿连线走，没有全局变量）：

    ``json``       数据入口：JSON 文本从上游来（比如 ``http`` 的 ``http_body``）；
    ``path``       数据入口：取值路径也能从上游来（按消息内容动态选字段的场景）；
    ``json_value`` 提取结果（出口）：**字符串化**——字符串原样、标量走 JSON 字面量
                   （``true`` / ``3`` / ``null``）、对象 / 数组紧凑序列化（中文不转义）。

**取不到不算事故**（与 ``http`` 的 4xx / 5xx 同类：是「数据的样子」，不是环境问题）：

* 文本为空（上游没送值 / 送了空串）→ warning + ``json_value`` 送空串，流程继续；
* 文本不是合法 JSON → warning + 空串（比如对方回了一个错误页）；
* 路径在文档里不存在 → warning + 空串（拼错字段名、对方改了结构）。

想「拿不到就失败」的话，把 ``json_value`` 接到日志先看；这个节点选择不打断流程，
每条警告都写进日志，排查时先看它。

小抄（``{"code":0,"data":{"users":[{"name":"小明"}]}}``）：

    path=data.users.0.name   -> 小明
    path=data.users.-1.name  -> 最后一个元素
    path=（留空）             -> 整个文档的紧凑形式
"""
from __future__ import annotations

import json
from typing import Any

from ..models import ValidationIssue, WorkflowNode
from .base import TRIGGER_PORT, ConfigField, NodeExecutionContext, PortSpec, input_value
from .registry import register_node

#: 日志 / ctx.log 里展示结果时的截断长度（长 JSON 不刷屏）
CLIP_CHARS: int = 120

#: 「路径没走到」的哨兵（与「值真的是 null」区分开——null 是有值，取不到才是空手）
_MISSING: object = object()


def _stringify(value: object) -> str:
    """把提取到的值字符串化：字符串原样；其余走 JSON 字面量（``ensure_ascii=False`` 读得懂中文）。"""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _dig(data: object, path: str) -> object:
    """按点路径逐段下钻；任何一段落空返回 :data:`_MISSING`（调用方据此区分「取到 null」）。"""
    value: object = data
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list):
            try:
                index = int(part)
            except ValueError:
                return _MISSING
            if -len(value) <= index < len(value):
                value = value[index]
            else:
                return _MISSING
        else:
            return _MISSING
    return value


def validate_json_node(node: WorkflowNode) -> list[ValidationIssue]:
    """两块手填值的防呆：``path`` 的点分段写法、``json`` 文本的 JSON 语法。

    线上来的值保存时还不知道，靠运行期的宽松分支兜住（见模块文档「取不到不算事故」）。
    """
    issues: list[ValidationIssue] = []

    path = node.config.get("path")
    if isinstance(path, str):
        text = path.strip()
        if text and (text.startswith(".") or text.endswith(".") or ".." in text):
            issues.append(
                ValidationIssue(
                    node_id=node.id,
                    code="INVALID_JSON_PATH",
                    message=f"json 节点 {node.id} 的取值路径 {path!r} 不合法",
                    suggestion="用点分段，例如 data.user.name / items.0.title；留空表示取整个文档",
                )
            )

    raw = node.config.get("json")
    if isinstance(raw, str) and raw.strip():
        try:
            json.loads(raw)
        except json.JSONDecodeError as exc:
            issues.append(
                ValidationIssue(
                    node_id=node.id,
                    code="INVALID_JSON_TEXT",
                    message=(
                        f"json 节点 {node.id} 手填的 JSON 文本不合法：{exc.msg}"
                        f"（第 {exc.lineno} 行第 {exc.colno} 列）"
                    ),
                    suggestion="修好 JSON 语法；或者清空这段手填值、改从 json 入口连线送值",
                )
            )

    return issues


@register_node(
    "json",
    label="JSON",
    order=80,
    category="data",
    # json / path 既是字段名也是数据入口：上游把值接到这两个端口，就覆盖 config 里手填的内容
    inputs=[
        TRIGGER_PORT,
        # json 是必填入口（同 http.url）：接线或手填都行，两个都没有才报 INPUT_NOT_CONNECTED
        PortSpec("json", "message", "JSON 文本", required=True),
        PortSpec("path", "message", "取值路径"),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("json_value", "message", "取值结果"),
    ],
    fields=[
        # json 的「必填」由入口（PortSpec.required）管；这里只声明手填兜底与默认路径
        ConfigField("json", "JSON 文本"),
        ConfigField("path", "取值路径", default=""),
    ],
    validator=validate_json_node,
)
async def exec_json(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """解析 JSON 文本并按 ``path`` 取值，产出 ``json_value``（字符串化后的结果）。"""
    text = str(input_value(node, ctx, "json", default="")).strip()
    path = str(input_value(node, ctx, "path", default="")).strip()

    if not text:
        ctx.logger.warning(f"[json:{node.id}] 没拿到 JSON 文本（上游没送值或送了空串）")
        ctx.log.append(f"[json] {node.id}: 没拿到 JSON 文本，输出空串")
        return {"json_value": ""}

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        ctx.logger.warning(f"[json:{node.id}] JSON 解析失败：{exc}（{len(text)} 字符）")
        ctx.log.append(f"[json] {node.id}: JSON 解析失败（{exc.msg}），输出空串")
        return {"json_value": ""}

    value = _dig(data, path) if path else data
    if value is _MISSING:
        ctx.logger.warning(f"[json:{node.id}] 路径 {path!r} 在文档里不存在")
        ctx.log.append(f"[json] {node.id}: 路径 {path!r} 取不到，输出空串")
        return {"json_value": ""}

    out = _stringify(value)
    shown = out if len(out) <= CLIP_CHARS else out[:CLIP_CHARS] + "…"
    ctx.logger.info(f"[json:{node.id}] path={path or '(整个文档)'} -> {len(out)} 字符")
    ctx.log.append(f"[json] {node.id}: {path or '(整个文档)'} -> {shown}")
    return {"json_value": out}
