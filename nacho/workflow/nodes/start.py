"""开始节点：图的起点；``config.trigger`` 决定触发方式。

config:
    trigger: ``time``（cron 定时触发）或 ``message``（消息触发，缺省）
    cron:    ``trigger=time`` 时必填，5 / 6 段 cron 表达式
    name:    调度任务显示名（可选，缺省用节点 id）

``time`` 不自己"到点执行"：把整条流程图登记到
:class:`~nacho.core.scheduler.TaskManager`，由调度器按 cron 触发整条流程；``message`` 被动
等消息接入（消息源留待后续），发布 / 试跑时只写一条开始日志。

校验规则（trigger 枚举 / time 时 cron 必填且合法）在 :func:`validate_start_node` 里，
随注册一起挂进注册表，校验器框架代码不认识具体类型。
"""
from __future__ import annotations

from typing import Any

from ..models import ValidationIssue, WorkflowNode
from .base import ConfigField, NodeExecutionContext
from .registry import register_node

#: start 节点的触发方式：time = cron 定时触发（需配 cron）；message = 消息触发（缺省）
START_TRIGGERS: frozenset[str] = frozenset({"time", "message"})


def validate_start_node(node: WorkflowNode) -> list[ValidationIssue]:
    """start 配置校验：trigger 只能是 time/message（缺省 message）；time 时 cron 必填且合法。"""
    trigger = node.config.get("trigger", "message")
    if not isinstance(trigger, str) or trigger not in START_TRIGGERS:
        return [
            ValidationIssue(
                node_id=node.id,
                code="INVALID_TRIGGER",
                message=f"start 节点 {node.id} 的触发方式 {trigger!r} 不合法",
                suggestion="config.trigger 只能是 time（定时）或 message（消息）",
            )
        ]
    if trigger != "time":
        return []
    return validate_time_cron(node)


def validate_time_cron(node: WorkflowNode) -> list[ValidationIssue]:
    """``trigger=time`` 的配置：cron 必填且合法。"""
    cron = node.config.get("cron")
    if not isinstance(cron, str) or not cron.strip():
        return [
            ValidationIssue(
                node_id=node.id,
                code="MISSING_CONFIG",
                message=f"start 节点 {node.id} 选择了时间触发，但缺少必填配置 cron（cron 表达式）",
                suggestion="在 config.cron 中补充 5/6 段 cron 表达式，如 */5 * * * *",
            )
        ]

    from nacho.core.scheduler import CronExpr, CronError  # 局部导入避免循环依赖

    try:
        CronExpr.parse(cron.strip())
    except CronError as exc:
        return [
            ValidationIssue(
                node_id=node.id,
                code="INVALID_CRON",
                message=f"start 节点 {node.id} 的 cron 表达式不合法：{exc}",
                suggestion="cron 用 5 段（分 时 日 月 周）或 6 段（秒 分 时 日 月 周），如 */5 * * * *",
            )
        ]
    return []


@register_node(
    "start",
    role="start",
    fields=[ConfigField("trigger", "触发方式", default="message")],
    validator=validate_start_node,
)
async def exec_start(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """开始节点：按 ``config.trigger`` 分流。"""
    trigger = str(node.config.get("trigger", "message"))
    if trigger == "time":
        return await _register_cron(node, ctx)

    ctx.log.append(f"[start] {node.id} 流程开始（消息触发）")
    ctx.logger.info("工作流开始（消息触发，等待消息进入）", node_id=node.id)
    return {}


async def _register_cron(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """把整条流程按 cron 登记到调度器（``trigger=time`` 的行为）。

    调度器没注入时只记日志、不实际登记（测试 / 离线场景）；登记的 task_id 固定为
    ``wf-<node.id>``，重复执行会先移除再登记（幂等）。
    """
    cron = str(node.config.get("cron", "")).strip()
    name = str(node.config.get("name", node.id))
    task_id = f"wf-{node.id}"

    if ctx.scheduler is None:
        ctx.logger.warning(
            f"[start:{node.id}] 时间触发未注入调度器，跳过登记",
            node_id=node.id,
            cron=cron,
        )
        ctx.log.append(f"[start:time] {node.id}: 未注入调度器，cron={cron}")
        return {"scheduled": False, "task_id": task_id, "cron": cron}

    # 幂等：先移除同名旧任务再登记（流程重跑 / 改 cron 时不残留）
    try:
        ctx.scheduler.remove(task_id)
    except KeyError:
        pass

    async def _trigger() -> None:
        """到点回调：跑整条流程。"""
        ctx.logger.info(f"[start:{node.id}] cron 触发，开始执行工作流", node_id=node.id)
        await ctx.run_workflow()

    task = ctx.scheduler.add(
        cron,
        _trigger,
        task_id=task_id,
        name=name,
        description=f"工作流开始节点（时间触发）{node.id}",
    )
    ctx.logger.info(
        f"[start:{node.id}] 已登记到调度器",
        node_id=node.id,
        cron=cron,
        task_id=task_id,
        next_run=str(task.next_run) if task.next_run else None,
    )
    ctx.log.append(f"[start:time] {node.id}: 已登记 cron={cron}, task_id={task_id}")
    return {"scheduled": True, "task_id": task_id, "cron": cron}
