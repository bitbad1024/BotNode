"""定时任务：cron 表达式 + 任务管理器。

用法一瞥::

    from botnode.core.scheduler import scheduler

    await scheduler.start()
    scheduler.add("*/5 * * * *", my_check, task_id="check", name="巡检")
    scheduler.set_enabled("check", False)          # 停用
    await scheduler.stop()

业务代码只碰 :class:`~botnode.core.scheduler.manager.TaskManager`（进程级单例
``scheduler``）；:class:`~botnode.core.scheduler.core.Scheduler` 是它内部的时间驱动
循环。cron 语法、单 / 多实例、失败隔离等完整说明见 ``docs/scheduler/scheduler.md``。
"""
from __future__ import annotations

from botnode.core.scheduler.cron import CronError, CronExpr, CronField
from botnode.core.scheduler.core import Scheduler
from botnode.core.scheduler.logging import SCHEDULER_LOGGER_NAME, scheduler_logger
from botnode.core.scheduler.manager import TaskManager, scheduler
from botnode.core.scheduler.models import Task, TaskFunc
from botnode.core.scheduler.timeline import TaskTimeline

__all__ = [
    "CronError",
    "CronExpr",
    "CronField",
    "Scheduler",
    "SCHEDULER_LOGGER_NAME",
    "Task",
    "TaskFunc",
    "TaskManager",
    "TaskTimeline",
    "scheduler",
    "scheduler_logger",
]
