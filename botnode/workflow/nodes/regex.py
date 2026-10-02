"""正则节点：从文本里提取信息（验证码 / ID / 片段），或按正则做替换（脱敏 / 改写）。

JSON 节点管结构化数据，这个管「长得不规整的文本」—— 两者是同一层的搭档。

config:
    text:    待处理的文本 —— **没接线时**的手填值（接线优先，字段名与端口同名）
    pattern: 正则表达式（必填：接线或手填；拼错在保存时拦 ``INVALID_REGEX``）
    action:  动作（缺省 ``extract``）：``extract`` = 提取第一个匹配；``replace`` = 替换所有匹配
    replace: 替换文本（``replace`` 动作才用；支持 ``\\1`` / ``\\g<名字>`` 反向引用，
             留空 = 把匹配删掉；字段名与端口同名，也能接线）
    flags:   正则旗标（可组合：``i`` 忽略大小写 / ``m`` 多行 / ``s`` 点号匹配换行），缺省无

端口（数据沿连线走）：

    ``text``        数据入口：待处理文本从上游来（比如 ``http_body`` / ``start`` 的 ``message``）；
    ``pattern``     数据入口：正则也能从上游来（按内容动态选规则）；
    ``replace``     数据入口：替换文本同样可接线；
    ``regex_value`` 处理结果（出口）：``extract`` = 第一个匹配（**有捕获组取第 1 组**，
                    组没参与匹配时回落整体匹配）；``replace`` = 替换后的完整文本。

**抽不到 = 业务失败**（与 ``json`` 节点同一口径）：

* 文本 / 正则为空、正则语法错（线上来的）、一个都没匹配上 → 抛
  :class:`~botnode.workflow.nodes.base.NodeFailure`：引擎**停止它向下传播**（下游整段跳过），
  不再送空串。每条失败都写进日志与 ``ctx.log``。

小抄：

    extract: pattern=(\\d{4,6})    text=验证码 123456   -> 123456
    extract: pattern=id=([\\w-]+)  text=id=u-42         -> u-42
    extract: pattern=(?:ftp|https?)://\\S+             -> 没组就取整体匹配
    replace: pattern=(\\d{4})\\d{4}  replace=\\1****     -> 手机号脱敏（1380****）
    replace: pattern=\\s+            replace=""          -> 把空白挤掉
"""
from __future__ import annotations

import re
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

#: 允许的动作，**顺序即画布下拉顺序**
REGEX_ACTION_ORDER: tuple[str, ...] = ("extract", "replace")

#: 允许的旗标字母 -> 正则模块的常量（字符串里按字母组合，如 ``im``）
REGEX_FLAG_LETTERS: dict[str, int] = {
    "i": re.IGNORECASE,
    "m": re.MULTILINE,
    "s": re.DOTALL,
}

#: 日志 / ctx.log 里展示结果时的截断长度（长文本不刷屏）
CLIP_CHARS: int = 120


def _fail(ctx: NodeExecutionContext, node_id: str, reason: str) -> NoReturn:
    """抽不到：抛业务失败（引擎停止它向下传播，见 :class:`NodeFailure`）。"""
    ctx.logger.warning(f"[regex:{node_id}] {reason}")
    ctx.log.append(f"[regex] {node_id}: {reason}")
    raise NodeFailure(reason)


def validate_regex_node(node: WorkflowNode) -> list[ValidationIssue]:
    """手填值的防呆：动作枚举 / 旗标字母 / 正则语法（能编译才算数）。

    线上来的值保存时还不知道，靠运行期的宽松分支兜住（见模块文档「抽不到不算事故」）。
    """
    issues: list[ValidationIssue] = []

    action = node.config.get("action")
    if isinstance(action, str) and action.strip() and action.strip() not in REGEX_ACTION_ORDER:
        issues.append(
            ValidationIssue(
                node_id=node.id,
                code="INVALID_REGEX_ACTION",
                message=f"regex 节点 {node.id} 的动作 {action!r} 不合法",
                suggestion="可选动作：extract（提取第一个匹配）/ replace（替换所有匹配）",
            )
        )

    flags = node.config.get("flags")
    if isinstance(flags, str):
        unknown = [letter for letter in flags.strip() if letter not in REGEX_FLAG_LETTERS]
        if unknown:
            issues.append(
                ValidationIssue(
                    node_id=node.id,
                    code="INVALID_REGEX_FLAGS",
                    message=f"regex 节点 {node.id} 的旗标 {flags!r} 含不认识的字母：{' '.join(unknown)}",
                    suggestion="只认 i（忽略大小写）/ m（多行）/ s（点号匹配换行），可组合，如 im",
                )
            )

    pattern = node.config.get("pattern")
    if isinstance(pattern, str) and pattern.strip():
        try:
            re.compile(pattern)
        except re.error as exc:
            issues.append(
                ValidationIssue(
                    node_id=node.id,
                    code="INVALID_REGEX",
                    message=f"regex 节点 {node.id} 的正则不合法：{exc}",
                    suggestion="检查括号 / 方括号 / 量词；可以先在正则工具里试一遍再粘过来",
                )
            )

    return issues


@register_node(
    "regex",
    label="正则",
    order=90,
    category="data",
    # text / pattern / replace 既是字段名也是数据入口：接线优先，没接线才用手填值
    inputs=[
        TRIGGER_PORT,
        PortSpec("text", "message", "待处理文本", required=True),
        PortSpec("pattern", "message", "正则表达式", required=True),
        PortSpec("replace", "message", "替换文本"),
    ],
    outputs=[
        TRIGGER_PORT,
        PortSpec("regex_value", "message", "处理结果"),
    ],
    fields=[
        # text / pattern 的「必填」由入口（PortSpec.required）管：接线或手填都行
        ConfigField("text", "待处理文本"),
        ConfigField("pattern", "正则表达式"),
        ConfigField("action", "动作", default="extract", options=REGEX_ACTION_ORDER),
        ConfigField("replace", "替换文本", default=""),
        ConfigField("flags", "旗标", default=""),
    ],
    validator=validate_regex_node,
)
async def exec_regex(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """按 ``action`` 提取第一个匹配或替换所有匹配，产出 ``regex_value``。"""
    text = str(input_value(node, ctx, "text", default=""))
    pattern = str(input_value(node, ctx, "pattern", default="")).strip()
    action = str(node.config.get("action", "")).strip() or "extract"
    replace_text = str(input_value(node, ctx, "replace", default=""))
    flags_text = str(node.config.get("flags") or "").strip()

    if not text:
        _fail(ctx, node.id, "没拿到文本（上游没送值或送了空串）")
    if not pattern:
        _fail(ctx, node.id, "正则为空（入口没接线、config 里也没填）")

    flags = 0
    for letter in flags_text:
        flags |= REGEX_FLAG_LETTERS.get(letter, 0)
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        _fail(ctx, node.id, f"正则编译失败：{exc}")

    if action == "replace":
        out = compiled.sub(replace_text, text)
        shown = out if len(out) <= CLIP_CHARS else out[:CLIP_CHARS] + "…"
        ctx.logger.info(f"[regex:{node.id}] replace -> {len(out)} 字符")
        ctx.log.append(f"[regex] {node.id}: 替换 -> {shown}")
        return {"regex_value": out}

    match = compiled.search(text)
    if match is None:
        _fail(ctx, node.id, f"文本里没有匹配 {pattern!r}")

    # 有捕获组取第 1 组；组没参与匹配（可选组）时回落整体匹配
    groups = match.groups()
    out = groups[0] if groups and groups[0] is not None else match.group(0)
    shown = out if len(out) <= CLIP_CHARS else out[:CLIP_CHARS] + "…"
    ctx.logger.info(f"[regex:{node.id}] extract -> {len(out)} 字符")
    ctx.log.append(f"[regex] {node.id}: 提取 -> {shown}")
    return {"regex_value": out}
