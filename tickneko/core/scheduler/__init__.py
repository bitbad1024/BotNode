"""定时任务：cron 表达式 + 任务管理器。

用法一瞥::

    from tickneko.core.scheduler import scheduler

    await scheduler.start()
    scheduler.add("*/5 * * * *", my_check, task_id="check", name="巡检")
    scheduler.set_enabled("check", False)          # 停用
    await scheduler.stop()

业务代码只碰 :class:`~tickneko.core.scheduler.manager.TaskManager`（进程级单例
``scheduler``）；:class:`~tickneko.core.scheduler.core.Scheduler` 是它内部的时间驱动
循环。cron 语法、单 / 多实例、失败隔离等完整说明见 ``docs/scheduler/scheduler.md``。
"""
from __future__ import annotations

from tickneko.core.scheduler.cron import CronError, CronExpr, CronField
from tickneko.core.scheduler.core import Scheduler
from tickneko.core.scheduler.logging import SCHEDULER_LOGGER_NAME, scheduler_logger
from tickneko.core.scheduler.manager import TaskManager, scheduler
from tickneko.core.scheduler.models import Task, TaskFunc
from tickneko.core.scheduler.timeline import TaskTimeline

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
