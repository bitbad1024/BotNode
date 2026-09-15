"""Scheduler：时间驱动的执行循环。

只认登记簿里 ``enabled=True`` 的任务：算出最近的 :attr:`Task.next_run`，睡到那
一刻，把到点的任务并发派发出去。增删改任务后由 :meth:`wake` 唤醒立即重排，
不靠固定间隔轮询。

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

from nacho.core.logger import get_logger
from nacho.core.scheduler.cron import CronError
from nacho.core.scheduler.models import Task


class Scheduler:
    """执行核心；登记簿由 TaskManager 持有并传入，两边共享同一份。"""

    def __init__(self, registry: dict[str, Task]) -> None:
        self._registry: dict[str, Task] = registry  # task_id -> Task，与 TaskManager 共享
        self._running: bool = False
        self._loop_task: asyncio.Task[None] | None = None  # 主循环
        self._wake_event: asyncio.Event = asyncio.Event()  # 增删改后 set，循环立刻醒来重排
        self._inflight: set[asyncio.Task[None]] = set()  # 正在跑的任务，stop 时等它们收尾

    # ---- 生命周期 ----
    async def start(self) -> None:
        """启动主循环；重复调用是空操作。"""
        if self._running:
            return
        self._running = True
        self._wake_event.set()  # 让循环立刻跑第一圈，把已有任务排上
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
        """任务增删改 / 启停后调一下，让循环立刻重算最近的触发点。"""
        self._wake_event.set()

    # ---- 给 TaskManager 的手动触发 ----
    def run_once(self, task: Task) -> asyncio.Task[None]:
        """立即手动执行一次（TaskManager.run_now 用），不影响 cron 排程。

        手动触发是强制的：单实例任务正在跑也照跑不误（计入 :attr:`Task.active`）。
        """
        task.active += 1
        return self._launch(task)

    # ---- 内部 ----
    # NOTE(性能：别急着改成堆): 这里每圈都 O(n) 全扫一遍取最近的 next_run，看着朴素，
    # 但对 cron（分钟粒度）来说恰恰是对的 —— 一天的 tick 次数被锁死在 <=1440，跟任务数
    # 无关，所以每天开销是 T*n <= 1440*n（n=2000 实测 ~158ms/天，摊到每秒 1.8 微秒）。
    # 换最小堆反而更慢：堆的开销是 F*log2(n)（F = 触发总次数），任务一旦挤在同一分钟
    # （0 0 * * * / * * * * * 这类最最常见），就是给每个任务单独付一次 log n，实测慢
    # 2~3 倍；还得额外处理改 cron / 停用 / 删除留下的失效条目（版本号 + 墓碑 + 压缩）。
    # TODO(换堆的时机): 支持秒级或亚秒级 cron（T 上限变 86400+）、任务数上 10 万且触发点
    # 高度分散、或每次扫描要碰 DB/IO 时，再把这里换成最小堆 + 惰性失效。
    async def _loop(self) -> None:
        """主循环：排程 -> 派发到点任务 -> 睡到最近的触发点（或被增删改唤醒）。"""
        while self._running:
            try:
                now = datetime.now()
                for task in list[Task](self._registry.values()):
                    if task.enabled and task.next_run is None:
                        self._reschedule(task, now)
                self._fire_if_due(now)
                self._wake_event.clear()
                due = [
                    task.next_run
                    for task in self._registry.values()
                    if task.enabled and task.next_run is not None
                ]
                if due:
                    delay = max(0.0, (min(due) - datetime.now()).total_seconds())
                    try:
                        await asyncio.wait_for(self._wake_event.wait(), timeout=delay)
                    except TimeoutError:
                        pass  # 到点了，回循环头去 fire
                else:
                    await self._wake_event.wait()  # 一个任务都没有：等增删改唤醒
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 循环绝不能死
                get_logger("scheduler").error(
                    "调度循环出意外，继续运行", error=f"{type(exc).__name__}: {exc}"
                )
                await asyncio.sleep(0.1)

    def _reschedule(self, task: Task, now: datetime) -> None:
        """按 cron 重算 task.next_run（停用 / 排不了程则置 None）。"""
        if not task.enabled:
            task.next_run = None
            return
        try:
            task.next_run = task.cron.next_after(now)
        except CronError as exc:
            task.next_run = None
            get_logger("scheduler").error(f"任务 {task.display_name} 的 cron 排不了程：{exc}")

    def _fire_if_due(self, now: datetime) -> None:
        """扫一遍启用任务，把 next_run <= now 的派发出去（派发前按实例策略做并发保护检查）。"""
        for task in list(self._registry.values()):
            if not task.enabled or task.next_run is None or task.next_run > now:
                continue
            if task.running and not task.multi_instance:
                get_logger("scheduler").warning(
                    f"任务 {task.display_name} 上一次还没跑完，跳过 {task.next_run} 这次"
                )
                self._reschedule(task, now)  # 别卡在过去的触发点上
                continue
            self._spawn(task, now)

    def _spawn(self, task: Task, now: datetime) -> None:
        """到点触发一次：先记实例数、排下一次（执行期间循环不空转），再派发。"""
        task.active += 1
        self._reschedule(task, now)
        self._launch(task)

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
            get_logger("scheduler").error(
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
            get_logger("scheduler").warning(
                f"停机时还有 {len(pending)} 个任务没跑完，不再等待（不强杀，让它们自然收尾）"
            )
