"""Scheduler：时间驱动的执行循环。

只认登记簿里 ``enabled=True`` 的任务：把它们的下一次触发点排进
:class:`~nacho.core.scheduler.timeline.TaskTimeline`（红黑树 + 哈希表二合一），
睡到最近那一刻，把到点的任务并发派发出去。增删改任务后唤醒循环立即重排，
不靠固定间隔轮询。

排程索引的代价：取最近触发点 O(1)（缓存最左节点）、按任务改排程 O(log n)；
O(n) 的全量对账只在启动和 :meth:`wake`（外部绕过 set_* 直接改了任务字段）时走。

行为约定：
- **单 / 多实例**：默认单实例 —— 上次还没跑完又到触发点就跳过本次并记一条 warning；
  ``multi_instance=True`` 的任务不受此限，到点照样开新实例，允许叠加；
- **错过不补**：停机 / 卡顿期间错过的触发点不补跑，醒来后从 ``next_after(now)`` 重排；
- **失败隔离**：任务抛异常只更新 :class:`Task` 的失败状态并打 error 日志，绝不带崩循环。
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime

from nacho.core.logger import BaseLogger
from nacho.core.scheduler.cron import CronError
from nacho.core.scheduler.logging import scheduler_logger
from nacho.core.scheduler.models import Task
from nacho.core.scheduler.timeline import TaskTimeline


class Scheduler:
    """执行核心；登记簿由 TaskManager 持有并传入，两边共享同一份。

    日志实例由装配层传入（``logger=``，也可以事后 :meth:`attach_logger`）；没传就用
    本模块的便捷函数（:func:`nacho.core.scheduler.logging.scheduler_logger`），
    业务代码不直接 ``default_core()``。
    """

    def __init__(
        self, registry: dict[str, Task], *, logger: BaseLogger | None = None
    ) -> None:
        self._registry: dict[str, Task] = registry  # task_id -> Task，与 TaskManager 共享
        self._running: bool = False
        self._loop_task: asyncio.Task[None] | None = None  # 主循环
        self._wake_event: asyncio.Event = asyncio.Event()  # 增删改后 set，循环立刻醒来重排
        self._inflight: set[asyncio.Task[None]] = set()  # 正在跑的任务，stop 时等它们收尾
        #: 排程队列：按 next_run 排序的红黑树 + task_id 哈希表
        self._timeline: TaskTimeline = TaskTimeline()
        self._dirty: bool = True  # 置 True 时循环下一圈先做一次全量对账
        self._logger: BaseLogger | None = logger

    # ---- 日志注入 ----
    def attach_logger(self, logger: BaseLogger) -> None:
        """注入日志实例（装配层建完核心后调用；单例场景用，构造参数传了就不用再调）。"""
        self._logger = logger

    def _log(self) -> BaseLogger:
        """业务日志实例：装配注入的优先，没传就取 ``scheduler`` 便捷函数（装配槽位）。"""
        return self._logger if self._logger is not None else scheduler_logger()

    # ---- 生命周期 ----
    async def start(self) -> None:
        """启动主循环；重复调用是空操作。"""
        if self._running:
            return
        self._running = True
        self._dirty = True  # 第一圈先把登记簿里的任务排上
        self._wake_event.set()  # 让循环立刻跑第一圈
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self, timeout: float = 5.0) -> None:
        """停主循环，并在 timeout 秒内等正在跑的任务收尾；到点没跑完的记 warning，不强杀。"""
        if not self._running:
            return
        self._running = False
        self._wake_event.set()  # 叫醒在睡的循环，好让它退出
        loop_task, self._loop_task = self._loop_task, None
        if loop_task is not None:
            await loop_task
        await self._drain(timeout)

    @property
    def running(self) -> bool:
        """循环是否在跑。"""
        return self._running

    # ---- 给 TaskManager 的钩子 ----
    def wake(self) -> None:
        """外部绕过 set_* 直接改了任务字段后调一下：唤醒循环，并做一次全量对账。"""
        self._dirty = True
        self._wake_event.set()

    def note_changed(self, task: Task) -> None:
        """任务登记进来了 / 定义改了（cron、启停）：循环在跑就增量重排，没跑等启动时排。"""
        if self._running:
            self._schedule(task, datetime.now())
        self._wake()

    def note_removed(self, task: Task) -> None:
        """任务从登记簿删掉了：从排程队列里摘掉（正在跑的那次不受影响）。"""
        self._timeline.discard(task.task_id)
        self._wake()

    # ---- 给 TaskManager 的手动触发 ----
    def run_once(self, task: Task) -> asyncio.Task[None]:
        """立即手动执行一次（TaskManager.run_now 用），不影响 cron 排程。

        手动触发是强制的：单实例任务正在跑也照跑不误（计入 :attr:`Task.active`）。
        """
        task.active += 1
        return self._launch(task)

    # ---- 内部 ----
    # NOTE(为什么用红黑树而不是每圈全扫): 循环每圈都要问「下一个该跑谁」，秒级 cron 下
    # 一天要问 86400 次 —— 全扫是 86400*n（n=2000 按原先分钟级 158ms/天 ×60 推 ≈9.5s/天），
    # 时间线是 O(1)，n=2000 实测 5ms/天；改一次排程 O(log n) ≈14µs。O(n) 的全量对账
    # 只剩启动和 wake()（外部直接改了任务字段）这两处低频路径。
    # NOTE(为什么不是堆): 堆改一次排程要配版本号 + 墓碑 + 压缩这套惰性失效，红黑树直接
    # 把节点摘下来重插就是 O(log n)，还顺带能按 ID O(1) 定位 —— 也就是 Linux CFS 那套
    # rb_tree + pid 哈希表的组合，见 nacho/core/scheduler/timeline.py。
    async def _loop(self) -> None:
        """主循环：对账 -> 派发到点任务 -> 睡到最近的触发点（或被增删改唤醒）。"""
        while self._running:
            try:
                now = datetime.now()
                if self._dirty:  # 有人直接改了任务字段：全量对一次账，把队列摆正
                    self._dirty = False
                    self._sync_registry(now)
                self._fire_due(now)
                self._wake_event.clear()
                due = self._timeline.due_time()
                if due is None:
                    await self._wake_event.wait()  # 队列是空的：等增删改唤醒
                    continue
                delay = max(0.0, (due - datetime.now()).total_seconds())
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=delay)
                except TimeoutError:
                    pass  # 到点了，回循环头去 fire
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 循环绝不能死
                self._log().error(
                    "调度循环出意外，继续运行", error=f"{type(exc).__name__}: {exc}"
                )
                await asyncio.sleep(0.1)

    def _schedule(self, task: Task, now: datetime) -> None:
        """按 cron 重算 next_run 并同步进队列（停用 / 排不了程就从队列里摘掉）。"""
        if not task.enabled:
            task.next_run = None
            self._timeline.discard(task.task_id)
            return
        try:
            task.next_run = task.cron.next_after(now)
        except CronError as exc:
            task.next_run = None
            self._timeline.discard(task.task_id)
            self._log().error(f"任务 {task.display_name} 的 cron 排不了程：{exc}")
            return
        self._timeline.upsert(task)

    def _sync_registry(self, now: datetime) -> None:
        """登记簿和队列对一次账：该排的排上、挪位的挪位、该摘的摘掉（O(n)，低频）。"""
        for task in self._registry.values():
            if not task.enabled:
                self._timeline.discard(task.task_id)
            elif task.next_run is None:
                self._schedule(task, now)  # 还没排过（新加的 / 刚启用 / 刚改了 cron）
            elif self._timeline.key_of(task.task_id) != task.next_run:
                self._timeline.upsert(task)  # 被外部直接改了触发点

    def _fire_due(self, now: datetime) -> None:
        """从队头一路取出 next_run <= now 的任务派发（派发前按实例策略做并发保护检查）。"""
        budget = len(self._registry) + 1  # 防御：万一 cron 算不出未来时刻，也不会空转死循环
        while budget > 0:
            task = self._timeline.first_task()
            if task is None or task.next_run is None or task.next_run > now:
                return
            budget -= 1
            self._timeline.discard(task.task_id)  # 先出队，免得重排后又落回这个过去点
            if task.running and not task.multi_instance:
                self._log().warning(
                    f"任务 {task.display_name} 上一次还没跑完，跳过 {task.next_run} 这次"
                )
                self._schedule(task, now)  # 别卡在过去的触发点上
                continue
            self._spawn(task, now)

    def _spawn(self, task: Task, now: datetime) -> None:
        """到点触发一次：先记实例数、排下一次（执行期间循环不空转），再派发。"""
        task.active += 1
        self._schedule(task, now)
        self._launch(task)

    def _wake(self) -> None:
        """只唤醒循环（队列已经同步好了，不用对账）。"""
        self._wake_event.set()

    def _launch(self, task: Task) -> asyncio.Task[None]:
        """把一次执行包成 asyncio.Task 放进 _inflight，供 stop 时等收尾。"""
        runner = asyncio.create_task(self._execute(task))
        self._inflight.add(runner)
        runner.add_done_callback(self._inflight.discard)
        return runner

    async def _execute(self, task: Task) -> None:
        """单次执行：更新运行状态 -> 跑函数（协程直接 await，同步的丢线程池）-> 记成败。"""
        task.last_run = datetime.now()
        task.run_count += 1
        try:
            if inspect.iscoroutinefunction(task.func):
                await task.func()
            else:
                await asyncio.to_thread(task.func)
        except Exception as exc:  # noqa: BLE001 任务失败只记账，不带崩循环
            task.last_ok = False
            task.fail_count += 1
            task.last_error = f"{type(exc).__name__}: {exc}"
            self._log().error(
                f"定时任务 {task.display_name} 执行失败", error=task.last_error
            )
        else:
            task.last_ok = True
            task.last_error = ""
        finally:
            task.active -= 1

    async def _drain(self, timeout: float) -> None:
        """等 _inflight 全部收尾，最多等 timeout 秒。"""
        if not self._inflight:
            return
        _, pending = await asyncio.wait(self._inflight, timeout=timeout)
        if pending:
            self._log().warning(
                f"停机时还有 {len(pending)} 个任务没跑完，不再等待（不强杀，让它们自然收尾）"
            )
