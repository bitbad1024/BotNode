"""等待节点：异步等一会儿再往下走（**触发进、触发出**）。

为什么叫「异步」：它用 ``await asyncio.sleep(...)`` 等 —— 只让**这一轮的后续节点**晚点跑，
同进程里的其它工作流 / 其它定时任务照常跑。换成 ``time.sleep`` 会把整个事件循环卡死，
那是事故（一个等待节点能拖停整个服务）。

典型用法：定时流程里「等一会儿再查结果」「把几次调用错开」。这类等待摆在图里比藏在代码里直观。

config:
    seconds: 等待秒数 —— **没接线时**的手填值（缺省 5）

端口：

    ``trigger``   触发进 / 触发出：只表达「先等一会儿再往下走」，不接值也不送值；
    ``seconds``   等待秒数（数据入口，字号跟 config 里那项同名）。**线上来的优先，没接线才用
                  手填的 config.seconds** —— 所以「等多久」可以由上游算出来（常量 / HTTP 结果 /
                  别的节点算的值都行），实现就是「字段名 = 端口名」那条约定见
                  :func:`nacho.workflow.nodes.base.input_value`。

数值要求（手填的在校验阶段报 ``INVALID_DELAY_SECONDS``；**从连线来的值保存时还不知道**，
只能运行期判断 —— 不合法当场抛，绝不静默截断）：

* ``0`` = 不等（临时把等待关掉），允许；
* 负数 / 非数字 / 空值 / 超过 :data:`MAX_WAIT_SECONDS`（1 小时）都不行 —— 防呆：多敲一个 0
  （999999）能把这条流程挂好几天没人知道。

两点要记住：

* 等待期间这一轮的后续节点都不跑（引擎是串行的）：**单实例**的流程要是等得比 cron 间隔还长，
  下一拍会被调度器的「上一次还没跑完，跳过本次」挡掉；
* 想让它并发叠着跑，把这个工作流的**实例策略**设成「多实例」（列表页的设置弹窗里那一项）。

停机时 ``asyncio.sleep`` 会被取消，调度器最多等 5 秒收尾（见 ``Scheduler.stop``）不强杀。
"""
from __future__ import annotations

import asyncio
import math
from typing import Any

from ..models import ValidationIssue, WorkflowNode
from .base import TRIGGER_PORT, ConfigField, NodeExecutionContext, PortSpec, input_value
from .registry import register_node

#: 既没接线、config 里也没有时的缺省等待秒数（也走 ConfigField 的默认值：新建节点就填好它）
DEFAULT_WAIT_SECONDS: float = 5.0

#: 单次等待的上限（秒）：防呆 —— 多敲一个 0（999999）能把这条流程挂好几天没人知道
MAX_WAIT_SECONDS: float = 3600.0


def _seconds_of(node: WorkflowNode, raw: object) -> float:
    """把等待时长收敛成秒数；空值 / 非数字 / 越界当场抛（线上来的值只能运行期判断）。"""
    text = "" if raw is None else str(raw)
    if not text.strip():
        raise ValueError(f"节点 {node.id} 的等待时长是空的（入口没接线，config 里也没填）")
    try:
        seconds = float(text)
    except ValueError as exc:
        raise ValueError(f"节点 {node.id} 的等待时长不是数字：{raw!r}") from exc
    if math.isnan(seconds) or seconds < 0:
        raise ValueError(f"节点 {node.id} 的等待时长不合法：{raw!r}（要 0 或正数）")
    if seconds > MAX_WAIT_SECONDS:
        raise ValueError(
            f"节点 {node.id} 的等待时长 {seconds:g} 秒超过上限 {MAX_WAIT_SECONDS:g} 秒"
        )
    return seconds


def _seconds_issue(node: WorkflowNode, message: str, suggestion: str) -> ValidationIssue:
    """等待时长的校验错误（一处拼错误结构，省得每条规则各写一遍）。"""
    return ValidationIssue(
        node_id=node.id,
        code="INVALID_DELAY_SECONDS",
        message=message,
        suggestion=suggestion,
    )


def validate_delay_node(node: WorkflowNode) -> list[ValidationIssue]:
    """校验**手填**的等待时长：数字、非负、不超上限（``0`` 允许 —— 等于把等待临时关掉）。

    接了线的话这里查的还是 config 那一份兜底值（线上的值要等运行期才知道，见
    :func:`_seconds_of`）。
    """
    raw = node.config.get("seconds")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return []  # 缺了由 ConfigField 的默认值补（校验前已补全），这里不重复报

    try:
        seconds = float(str(raw))
    except ValueError:
        return [
            _seconds_issue(
                node,
                f"等待节点 {node.id} 的时长 {raw!r} 不是数字",
                "填秒数，例如 5 / 0.5 / 30",
            )
        ]
    if math.isnan(seconds):
        return [
            _seconds_issue(
                node, f"等待节点 {node.id} 的时长不是有效数值", "填秒数，例如 5 / 0.5 / 30"
            )
        ]
    if seconds < 0:
        return [_seconds_issue(node, f"等待节点 {node.id} 的时长不能是负数", "填 0（不等）或正数")]
    if seconds > MAX_WAIT_SECONDS:
        return [
            _seconds_issue(
                node,
                f"等待节点 {node.id} 的时长 {seconds:g} 秒超过上限 {MAX_WAIT_SECONDS:g} 秒",
                "一次最多等 1 小时；确实要等更久就拆成几个等待节点",
            )
        ]
    return []


@register_node(
    "delay",
    label="等待",
    order=70,
    # seconds 既是字段名也是数据入口：上游把值接到这个端口就覆盖 config 里手填的秒数。
    # 入口不标 required —— 没接线还有手填兜底（与 http.url 那种「必须接线或手填」不同）
    inputs=[TRIGGER_PORT, PortSpec("seconds", "message", "等待时长（秒）")],
    outputs=[TRIGGER_PORT],
    fields=[
        # 手填那一份仍是必填（清空即 MISSING_CONFIG），新建节点时画布按默认值填 5
        ConfigField("seconds", "等待时长（秒）", required=True, default=DEFAULT_WAIT_SECONDS),
    ],
    validator=validate_delay_node,
)
async def exec_delay(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """异步等 ``seconds`` 秒再返回：**线上来的优先，没接线才用手填的 config**。"""
    raw = input_value(node, ctx, "seconds", default=DEFAULT_WAIT_SECONDS)
    seconds = _seconds_of(node, raw)
    if seconds <= 0:
        ctx.log.append(f"[delay] {node.id}: 不等待（seconds={seconds:g}）")
        return {}
    ctx.logger.info("等待", node_id=node.id, seconds=seconds)
    await asyncio.sleep(seconds)
    ctx.log.append(f"[delay] {node.id}: 等待 {seconds:g} 秒")
    return {}
