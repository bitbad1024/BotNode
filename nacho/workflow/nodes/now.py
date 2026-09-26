"""当前时间节点：产出「现在」—— 格式化文本 + Unix 时间戳（cron 流程打时间戳、拼报告头）。

时间取的是**服务器本地时区**的当前时刻（``datetime.now()``），格式按 ``strftime`` 指令写。

config:
    format: 时间格式（strftime 指令，缺省 ``%Y-%m-%d %H:%M:%S``）；字段名与端口同名，
            也能接线覆盖（按上游数据动态选格式的场景）

端口（数据沿连线走）：

    ``format``    数据入口：时间格式从上游来；
    ``now_text``  格式化后的时间文本（出口）；
    ``now_ts``    Unix 时间戳（出口，整数秒，如 1758888888 —— 下游拿到的是 int）。

小抄（strftime 常用指令）：

    %Y-%m-%d        2026-09-26
    %H:%M:%S        14:30:05
    %Y%m%d_%H%M%S   20260926_143005（当文件名后缀用）
    %A              Saturday（星期几）

这个节点没有失败分支：本地时间永远拿得到；格式指令写错时 ``strftime`` 大多原样输出、
不抛错（各地 Python 的表现略有差异，拼报告前先在画布上试跑一次）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..models import WorkflowNode
from .base import TRIGGER_PORT, ConfigField, NodeExecutionContext, PortSpec, input_value
from .registry import register_node

#: 缺省时间格式（strftime 指令）
DEFAULT_TIME_FORMAT: str = "%Y-%m-%d %H:%M:%S"


@register_node(
    "now",
    label="当前时间",
    order=100,
    # format 既是字段名也是数据入口：接线优先，没接线才用手填值
    inputs=[TRIGGER_PORT, PortSpec("format", "message", "时间格式")],
    outputs=[
        TRIGGER_PORT,
        PortSpec("now_text", "message", "格式化时间"),
        PortSpec("now_ts", "message", "Unix 时间戳（秒）"),
    ],
    fields=[ConfigField("format", "时间格式", default=DEFAULT_TIME_FORMAT)],
)
async def exec_now(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """产出当前时刻：按 ``format`` 格式化 + Unix 时间戳（整数秒）。"""
    fmt = str(input_value(node, ctx, "format", default=DEFAULT_TIME_FORMAT))
    moment = datetime.now()
    text = moment.strftime(fmt)
    timestamp = int(moment.timestamp())
    ctx.logger.info(f"[now:{node.id}] {text}（ts={timestamp}）")
    ctx.log.append(f"[now] {node.id}: {text}（ts={timestamp}）")
    return {"now_text": text, "now_ts": timestamp}
