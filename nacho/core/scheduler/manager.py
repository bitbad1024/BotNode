"""TaskManager：任务管理器 —— 登记、增删改查、启停，以及 Scheduler 的持有者。

用法一瞥::

    scheduler = TaskManager()          # 或直接用下面的进程级单例 scheduler
    await scheduler.start()
    scheduler.add("*/5 * * * *", my_check, task_id="check", name="巡检", description="每5分钟")
    scheduler.set_enabled("check", False)
    scheduler.set_func("check", other_func)
    scheduler.remove("check")
    await scheduler.stop()

next_run 统一由 :class:`~nacho.core.scheduler.core.Scheduler` 排：登记 / 改动只把
它清空并唤醒循环重算，所以循环没启动时看到的一直是 None，start 后才落定。
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from nacho.core.logger import BaseLogger
from nacho.core.scheduler.core import Scheduler
from nacho.core.scheduler.cron import CronExpr
from nacho.core.scheduler.models import Task, TaskFunc, new_task_id


class TaskManager:
    """任务登记簿 + 执行核心的门面；改定义的操作都会顺手唤醒循环重排。

    日志实例由装配层传入（``logger=``），原样交给内部的 :class:`Scheduler`；没传就由
    Scheduler 走本模块的便捷函数（:func:`nacho.core.scheduler.logging.scheduler_logger`）。
    """

    def __init__(self, *, logger: BaseLogger | None = None) -> None:
        self._tasks: dict[str, Task] = {}  # task_id -> Task，与 Scheduler 共享
        self._scheduler: Scheduler = Scheduler(self._tasks, logger=logger)

    # ---- 生命周期 ----
    async def start(self) -> None:
        """启动执行循环；重复调用是空操作。"""
        await self._scheduler.start()

    async def stop(self, timeout: float = 5.0) -> None:
        """停执行循环（正在跑的任务在 timeout 内收尾）。"""
        await self._scheduler.stop(timeout)

    @property
    def running(self) -> bool:
        """执行循环是否在跑。"""
        return self._scheduler.running

    def wake(self) -> None:
        """叫醒执行循环重排（不走 set_* 而是直接改了任务状态后用）。"""
        self._scheduler.wake()

    # ---- 增删查 ----
    def add(
        self,
        cron: str | CronExpr,
        func: TaskFunc,
        *,
        task_id: str | None = None,
        name: str = "",
        description: str = "",
        enabled: bool = True,
        multi_instance: bool = False,
    ) -> Task:
        """登记一个任务并返回它。

        multi_instance 默认 False（单实例：上次没跑完就跳过本次）；置 True 则到点
        就开新实例，允许叠加。task_id 缺省自动生成；task_id 重复抛
        :class:`ValueError`，cron 不合法抛 :class:`CronError`，func 不是可调用对象
        抛 :class:`TypeError`。
        """
        expr = self._parse_cron(cron)
        if not callable(func):
            raise TypeError(  # pyright: ignore[reportUnreachable]
                f"func 要可调用对象，收到 {type(func).__name__}"
            )
        final_id = task_id or new_task_id()
        if final_id in self._tasks:
            raise ValueError(
                f"任务 ID {final_id!r} 已存在（{self._tasks[final_id].display_name!r}）"
            )
        task = Task(
            task_id=final_id,
            cron=expr,
            func=func,
            name=name,
            description=description,
            enabled=enabled,
            multi_instance=multi_instance,
        )
        self._tasks[final_id] = task
        self._scheduler.note_changed(task)  # 循环醒来看看，把新任务排上
        return task

    def remove(self, task_id: str) -> bool:
        """删除任务；存在并删掉了返回 True，不存在返回 False。正在跑的那次不受影响。"""
        task = self._tasks.pop(task_id, None)
        if task is None:
            return False
        self._scheduler.note_removed(task)
        return True

    def get(self, task_id: str) -> Task:
        """取单个任务；不存在抛 :class:`KeyError`（提示里带上现有任务清单）。"""
        return self._require(task_id)

    def list(self, *, enabled_only: bool = False) -> list[Task]:
        """任务清单，按 next_run 从近到远排序（None 排最后），供展示与调试。"""
        tasks = [task for task in self._tasks.values() if task.enabled or not enabled_only]
        return sorted(tasks, key=lambda task: (task.next_run is None, task.next_run))

    # ---- 改定义（都通过 _require 找任务，改完唤醒重排）----
    def set_enabled(self, task_id: str, enabled: bool) -> None:
        """启用 / 停用；next_run 清空交给循环重排，停用后保持 None。"""
        task = self._require(task_id)
        task.enabled = enabled
        task.next_run = None
        self._scheduler.note_changed(task)

    def set_func(self, task_id: str, func: TaskFunc) -> None:
        """修改任务激活的函数（排程不变，下一拍就跑新的）。"""
        if not callable(func):
            raise TypeError(  # pyright: ignore[reportUnreachable]
                f"func 要可调用对象，收到 {type(func).__name__}"
            )
        self._require(task_id).func = func

    def set_multi_instance(self, task_id: str, multi_instance: bool) -> None:
        """切换单 / 多实例（True = 上次没跑完也照开新的；False = 跳过本次）。"""
        self._require(task_id).multi_instance = multi_instance

    def set_cron(self, task_id: str, cron: str | CronExpr) -> None:
        """改触发规则（收 str 或已解析的 CronExpr，解析失败抛 :class:`CronError`）。"""
        task = self._require(task_id)
        task.cron = self._parse_cron(cron)
        task.next_run = None
        self._scheduler.note_changed(task)

    def set_name(self, task_id: str, name: str) -> None:
        """改显示名。"""
        self._require(task_id).name = name

    def set_description(self, task_id: str, description: str) -> None:
        """改描述。"""
        self._require(task_id).description = description

    # ---- 调试辅助 ----
    def run_now(self, task_id: str) -> asyncio.Task[None]:
        """立即手动触发一次（等价于一次到点执行），不影响 cron 排程。

        手动触发是强制的：单实例任务正在跑时也照跑不误，会多出一个并发实例。
        要在事件循环里调用（调度循环没启动也行）。
        """
        return self._scheduler.run_once(self._require(task_id))

    def preview(self, cron: str | CronExpr, n: int = 3) -> list[datetime]:
        """不建任务，预览某表达式接下来 n 次触发点；表达式不合法抛 :class:`CronError`。"""
        expr = self._parse_cron(cron)
        moments: list[datetime] = []
        cursor = datetime.now()
        for _ in range(n):
            cursor = expr.next_after(cursor)
            moments.append(cursor)
        return moments

    # ---- 内部 ----
    def _require(self, task_id: str) -> Task:
        """按 ID 取任务，不存在抛 :class:`KeyError`；所有 set_*/remove 共用。"""
        try:
            return self._tasks[task_id]
        except KeyError:
            existing = ", ".join(sorted(self._tasks)) or "（无）"
            raise KeyError(f"任务 {task_id!r} 不存在；现有任务：{existing}") from None

    def _parse_cron(self, cron: str | CronExpr) -> CronExpr:
        """str 就解析成 CronExpr，本来就是就直接用。"""
        return cron if isinstance(cron, CronExpr) else CronExpr.parse(cron)


#: 进程级默认任务管理器；业务代码直接 from nacho.core.scheduler import scheduler
scheduler: TaskManager = TaskManager()