"""开始节点：图的起点；``config.trigger`` 决定触发方式。

config:
    trigger: ``time``（cron 定时触发）或 ``message``（消息触发，缺省）
    cron:    ``trigger=time`` 时必填，5 / 6 段 cron 表达式
    name:    调度任务显示名（可选，缺省用节点 id）

输出端口：

    ``message``  消息触发时外部送进来的那条消息（``ctx.trigger_data["message"]``，没有就空串）；
                 时间触发没有消息，所以那份图别把线接到 ``message`` 出口上

``time`` 不自己"到点执行"：把整条流程图登记到
:class:`~nacho.core.scheduler.TaskManager`，由调度器按 cron 触发整条流程；``message`` 被动
等消息接入（消息源留待后续），发布 / 试跑时只写一条开始日志。

**加 / 摘任务只在「登记那一趟」做**（拨运行开关 / 启动载入 / 发布新版，见
:attr:`NodeExecutionContext.register_triggers`）；整图执行（cron 到点）那一趟不碰调度器
—— 它在派发前就已经排好了下一次。

校验规则（trigger 枚举 / time 时 cron 必填且合法）在 :func:`validate_start_node` 里，
随注册一起挂进注册表，校验器框架代码不认识具体类型。
"""
from __future__ import annotations

from typing import Any

from ..models import ValidationIssue, WorkflowNode
from .base import TRIGGER_PORT, ConfigField, NodeExecutionContext, PortSpec
from .registry import register_node

#: start 节点的触发方式，**顺序即画布下拉顺序**：message = 消息触发（缺省）；
#: time = cron 定时触发（需配 cron）
START_TRIGGER_ORDER: tuple[str, ...] = ("message", "time")

#: 触发方式集合（校验用；与上面的顺序表同一份内容）
START_TRIGGERS: frozenset[str] = frozenset(START_TRIGGER_ORDER)


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
    label="开始",
    order=10,
    role="start",
    # 端口按**缺省形态**（消息触发 = 触发 + 消息）声明；时间触发只出触发端口，由画布按
    # config.trigger 切换 —— start 是唯一一个端口随配置变的类型，前端为它留了特判。
    outputs=[TRIGGER_PORT, PortSpec("message", "message", "消息")],
    fields=[
        ConfigField("trigger", "触发方式", default="message", options=START_TRIGGER_ORDER),
        # cron / name 只在 trigger=time 时有意义（message 触发下既不校验也没用处），但仍然是
        # 这张图**认**的字段，所以照实声明 —— 画布拿到什么就渲染什么，不再自己猜。
        ConfigField("cron", "cron 表达式"),
        ConfigField("name", "调度任务名"),
    ],
    validator=validate_start_node,
)
async def exec_start(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """开始节点：按 ``config.trigger`` 分流。"""
    trigger = str(node.config.get("trigger", "message"))
    if trigger == "time":
        return await _register_cron(node, ctx)

    # 消息触发的消息是「外面送进来的」：调用方把它放在 ctx.trigger_data 里，这里原样从
    # message 出口送下去（消息源还没接，缺省就是空串）
    message = ctx.trigger_data.get("message", "")
    ctx.log.append(f"[start] {node.id} 流程开始（消息触发）")
    ctx.logger.info("工作流开始（消息触发，等待消息进入）", node_id=node.id)
    return {"message": message}


def workflow_task_id(workflow_id: str, node_id: str) -> str:
    """这条时间触发在调度器里的任务名：**工作流 + 节点**两级。

    只用节点 id 不够 —— 节点 id 只在**一张图内**唯一，两条工作流里都叫 ``s`` 的开始节点会
    互相顶掉（后登记的把先登记的移除）。带上工作流 id 之后，不同工作流、不同节点都不会撞。

    ``stop`` 那边照同一份口径算 id 去摘任务（见 :func:`nacho.workflow.runtime.
    stop_published_workflow`），所以**改这里的形状两边要一起改**。
    """
    return f"wf-{workflow_id}-{node_id}"


def _is_registered(ctx: NodeExecutionContext, task_id: str) -> bool:
    """调度器里有没有这个任务（没注入调度器 / 没这个 id 都算没有）。"""
    if ctx.scheduler is None:
        return False
    try:
        ctx.scheduler.get(task_id)
    except KeyError:
        return False
    return True


async def _register_cron(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
    """``trigger=time`` 的行为：**只在「登记那一趟」**把整条流程按 cron 登记到调度器。

    两趟分得很清（见 :attr:`NodeExecutionContext.register_triggers`）：

    * **登记那一趟**（拨运行开关 / 启动载入 / 发布新版）：加任务，或把同名旧任务换成新定义。
      调度器没注入时只记日志、不实际登记（测试 / 离线场景）；
    * **执行那一趟**（cron 到点跑整条流程）：**不碰调度器** —— 任务在里面排着，而调度器在派发
      前就会重排下一次（``nacho.core.scheduler.core.Scheduler._spawn``）。以前每次跑图都先摘
      再登记，等于每执行一次就新建一个任务对象：运行统计被清零，连「上一次还没跑完就跳过本次」
      的单实例保护也一并失效了。

    登记的 task_id 由 :func:`workflow_task_id` 定（``wf-<工作流 id>-<节点 id>``）。登记是幂等
    的：先移除同名旧任务再添加，改 cron / 改名字后重复登记不会残留旧任务。
    """
    cron = str(node.config.get("cron", "")).strip()
    name = str(node.config.get("name", node.id))
    task_id = workflow_task_id(ctx.workflow_id, node.id)

    if not ctx.register_triggers:
        ctx.log.append(f"[start:time] {node.id}: 执行中，调度器自己排下一次（cron={cron}）")
        return {"scheduled": _is_registered(ctx, task_id), "task_id": task_id, "cron": cron}

    if ctx.scheduler is None:
        ctx.logger.warning(
            f"[start:{node.id}] 时间触发未注入调度器，跳过登记",
            node_id=node.id,
            cron=cron,
        )
        ctx.log.append(f"[start:time] {node.id}: 未注入调度器，cron={cron}")
        return {"scheduled": False, "task_id": task_id, "cron": cron}

    ctx.scheduler.remove(task_id)  # 幂等：同名旧任务先摘掉（不在就返回 False，不抛）

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
