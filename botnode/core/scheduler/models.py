"""任务模型：:class:`Task` 一份任务的定义 + 运行状态。"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from botnode.core.scheduler.cron import CronExpr

#: 任务函数：同步函数（调度器丢线程池跑）或协程函数（直接 await）
TaskFunc = Callable[..., object]


def new_task_id() -> str:
    """自动生成任务 ID：``task-`` + 随机串。"""
    return f"task-{uuid4().hex}"


@dataclass
class Task:
    """一个定时任务。

    上半部分是**定义**（由 :class:`~botnode.core.scheduler.manager.TaskManager` 维护，
    用户可改）；下半部分是**运行状态**（由 Scheduler 维护，只读查询，重启归零）。
    """

    # ---- 定义 ----
    task_id: str  # 唯一 ID；add 时传入或自动生成
    cron: CronExpr  # 触发规则：5 段（分 时 日 月 周）或 6 段（秒开头）
    func: TaskFunc  # 到点执行的函数
    name: str = ""  # 显示名，空则展示时用 task_id
    description: str = ""  # 描述
    enabled: bool = True  # 停用后不排程、到点不执行
    #: 实例策略：False（默认，单实例）上一次没跑完就跳过本次；True（多实例）到点就开新的，允许叠加
    multi_instance: bool = False

    # ---- 运行状态 ----
    next_run: datetime | None = None  # 下次触发时刻；停用时为 None
    last_run: datetime | None = None  # 上次实际开始执行的时刻
    last_ok: bool | None = None  # 上次执行成败；没跑过为 None
    last_error: str = ""  # 上次失败的异常摘要（一行）
    run_count: int = 0  # 累计执行次数
    fail_count: int = 0  # 累计失败次数
    #: 此刻正在跑的实例数（Scheduler 维护；单实例任务只会在 0/1 之间来回）
    active: int = field(default=0, repr=False, compare=False)

    @property
    def running(self) -> bool:
        """是否有实例正在跑（= :attr:`active` > 0）。"""
        return self.active > 0

    @property
    def display_name(self) -> str:
        """展示名：name 优先，没写用 task_id。"""
        return self.name or self.task_id
