"""时间触发节点：按 cron 把整条流程登记到调度器。

config:
    cron:   cron 表达式（必填，5 或 6 段）
    name:   调度任务显示名（可选，缺省用工作流节点 id）

调度器没注入时只记日志、不实际登记（测试 / 离线场景）；登记的 task_id 固定为
``wf-<node.id>``，重复执行会先移除再登记（幂等）。
"""
from __future__ import annotations

from typing import Any

from ..models import WorkflowNode
from .base import NodeExecutionContext
from .registry import register_node


@register_node("time-trigger")
async def exec_time_trigger(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """时间触发节点：按 cron 把整条流程登记到调度器。"""
    cron = str(node.config.get("cron", "")).strip()
    name = str(node.config.get("name", node.id))
    task_id = f"wf-{node.id}"

    if ctx.scheduler is None:
        ctx.logger.warning(
            f"[time-trigger:{node.id}] 未注入调度器，跳过登记",
            node_id=node.id,
            cron=cron,
        )
        ctx.log.append(f"[time-trigger] {node.id}: 未注入调度器，cron={cron}")
        return {"scheduled": False, "task_id": task_id, "cron": cron}

    # 幂等：先移除同名旧任务再登记（流程重跑 / 改 cron 时不残留）
    try:
        ctx.scheduler.remove(task_id)
    except KeyError:
        pass

    async def _trigger() -> None:
        """到点回调：跑整条流程。"""
        ctx.logger.info(f"[time-trigger:{node.id}] cron 触发，开始执行工作流", node_id=node.id)
        await ctx.run_workflow()

    task = ctx.scheduler.add(
        cron,
        _trigger,
        task_id=task_id,
        name=name,
        description=f"工作流时间触发节点 {node.id}",
    )
    ctx.logger.info(
        f"[time-trigger:{node.id}] 已登记到调度器",
        node_id=node.id,
        cron=cron,
        task_id=task_id,
        next_run=str(task.next_run) if task.next_run else None,
    )
    ctx.log.append(f"[time-trigger] {node.id}: 已登记 cron={cron}, task_id={task_id}")
    return {"scheduled": True, "task_id": task_id, "cron": cron}
