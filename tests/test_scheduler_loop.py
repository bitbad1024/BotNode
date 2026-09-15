"""Scheduler 执行循环的集成测试：起真循环、注入到期时间验证派发，全部秒级完成。"""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

from nacho.core.scheduler import TaskManager


async def wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    """轮询等待条件成立，验证循环这种异步副作用用。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


def past() -> datetime:
    """一个刚刚过去的时刻：塞给 next_run 就是「到点了」。"""
    return datetime.now() - timedelta(seconds=1)


@pytest.fixture
def tm() -> TaskManager:
    return TaskManager()


async def test_due_task_runs_and_reschedules(tm: TaskManager) -> None:
    hits: list[str] = []

    async def job() -> None:
        hits.append("x")

    task = tm.add("* * * * *", job, task_id="j")
    assert task.next_run is None  # 循环没启动，还没排程
    await tm.start()
    try:
        assert await wait_until(lambda: task.next_run is not None)  # 启动后排程落定
        task.next_run = past()
        tm.wake()
        assert await wait_until(lambda: task.run_count == 1)
        assert hits == ["x"]
        assert task.last_ok is True
        assert task.last_error == ""
        assert task.next_run is not None and task.next_run > past()  # 已重排到未来
    finally:
        await tm.stop()


async def test_sync_function_runs_in_thread(tm: TaskManager) -> None:
    result: dict[str, bool] = {}

    def job() -> None:
        result["off_loop"] = threading.current_thread() is not threading.main_thread()

    task = tm.add("* * * * *", job, task_id="sync")
    await tm.start()
    try:
        task.next_run = past()
        tm.wake()
        assert await wait_until(lambda: task.run_count == 1)
        assert result["off_loop"] is True
    finally:
        await tm.stop()


async def test_coroutine_function_awaited_in_loop(tm: TaskManager) -> None:
    result: dict[str, bool] = {}

    async def job() -> None:
        # 在事件循环线程里跑说明是被 await 的协程，不是丢线程池跑的
        result["in_loop"] = threading.current_thread() is threading.main_thread()

    task = tm.add("* * * * *", job, task_id="async")
    await tm.start()
    try:
        task.next_run = past()
        tm.wake()
        assert await wait_until(lambda: task.run_count == 1)
        assert result["in_loop"] is True
        assert task.last_ok is True
    finally:
        await tm.stop()


async def test_failure_is_recorded_and_isolated(tm: TaskManager) -> None:
    good_hits: list[int] = []

    async def bad() -> None:
        raise ValueError("boom")

    async def good() -> None:
        good_hits.append(1)

    bad_task = tm.add("* * * * *", bad, task_id="bad")
    good_task = tm.add("* * * * *", good, task_id="good")
    await tm.start()
    try:
        bad_task.next_run = past()
        good_task.next_run = past()
        tm.wake()
        assert await wait_until(lambda: good_task.run_count == 1)  # 坏任务没拖垮循环
        assert bad_task.last_ok is False
        assert bad_task.fail_count == 1
        assert "ValueError" in bad_task.last_error and "boom" in bad_task.last_error
        assert good_task.last_ok is True
    finally:
        await tm.stop()


async def test_running_task_skips_next_hit(tm: TaskManager) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow() -> None:
        started.set()
        release.wait(2.0)

    task = tm.add("* * * * *", slow, task_id="slow")
    await tm.start()
    try:
        task.next_run = past()
        tm.wake()
        assert await wait_until(lambda: task.running)  # 第一次执行已开跑
        task.next_run = past()  # 执行期间又到点
        tm.wake()
        await asyncio.sleep(0.3)
        assert task.run_count == 1  # 被跳过，没有并发叠跑
        release.set()
        assert await wait_until(lambda: not task.running)
        assert task.run_count == 1
    finally:
        release.set()
        await tm.stop()


async def test_disabled_task_never_fires(tm: TaskManager) -> None:
    hits: list[int] = []

    async def job() -> None:
        hits.append(1)

    task = tm.add("* * * * *", job, task_id="off", enabled=False)
    await tm.start()
    try:
        task.next_run = past()  # 就算被塞了个过去的点也不该跑
        tm.wake()
        await asyncio.sleep(0.2)
        assert task.run_count == 0
        assert task.last_run is None
    finally:
        await tm.stop()


async def test_removed_task_does_not_fire(tm: TaskManager) -> None:
    hits: list[int] = []

    async def job() -> None:
        hits.append(1)

    task = tm.add("* * * * *", job, task_id="doomed")
    task.next_run = past()
    await tm.start()
    try:
        tm.remove("doomed")
        tm.wake()
        await asyncio.sleep(0.2)
        assert hits == []
        assert task.run_count == 0
    finally:
        await tm.stop()


async def test_stop_waits_for_running_task(tm: TaskManager) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow() -> None:
        started.set()
        release.wait(2.0)

    tm.add("* * * * *", slow, task_id="slow")
    await tm.start()
    task = tm.get("slow")
    task.next_run = past()
    tm.wake()
    assert await wait_until(lambda: task.running)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await tm.stop(timeout=3.0)
    release.set()
    assert loop.time() - t0 >= 0.3  # 确实等了一段时间才返回
    assert tm.running is False


async def test_start_is_idempotent(tm: TaskManager) -> None:
    hits: list[int] = []

    async def job() -> None:
        hits.append(1)

    task = tm.add("* * * * *", job, task_id="j")
    await tm.start()
    await tm.start()  # 第二次 start 不该起第二个循环
    try:
        task.next_run = past()
        tm.wake()
        assert await wait_until(lambda: task.run_count == 1)
        await asyncio.sleep(0.2)
        assert task.run_count == 1  # 也只跑一次
    finally:
        await tm.stop()
