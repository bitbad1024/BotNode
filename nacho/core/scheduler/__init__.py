"""定时任务：cron 表达式 + 任务管理器。

用法一瞥::

    from nacho.core.scheduler import scheduler, CronExpr

    await scheduler.start()
    scheduler.add("*/5 * * * *", my_check, task_id="check", name="巡检")
    scheduler.set_enabled("check", False)          # 停用
    scheduler.set_func("check", other_func)        # 换激活的函数
    await scheduler.stop()

业务代码通常只碰 :class:`~nacho.core.scheduler.manager.TaskManager`（进程级单例
``scheduler``）；:class:`~nacho.core.scheduler.core.Scheduler` 是它内部的时间驱动
循环，:class:`~nacho.core.scheduler.cron.CronExpr` 是表达式解析。任务函数同步协程
皆可——同步函数丢线程池跑，协程函数直接 await。
"""
from __future__ import annotations

from nacho.core.scheduler.cron import CronError, CronExpr, CronField
from nacho.core.scheduler.core import Scheduler
from nacho.core.scheduler.manager import TaskManager, scheduler
from nacho.core.scheduler.models import Task, TaskFunc

__all__ = [
    "CronError",
    "CronExpr",
    "CronField",
    "Scheduler",
    "Task",
    "TaskFunc",
    "TaskManager",
    "scheduler",
]
