"""TaskManager 的单元测试：增删改查、校验报错、preview 与 run_now。不启动循环。"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from nacho.core.scheduler import CronError, CronExpr, Task, TaskManager


def noop() -> None:
    """占位任务函数。"""


class TestAddAndGet:
    def test_add_returns_registered_task(self) -> None:
        tm = TaskManager()
        task = tm.add("*/5 * * * *", noop, task_id="a", name="巡检", description="每5分钟")
        assert isinstance(task, Task)
        assert tm.get("a") is task
        assert task.task_id == "a"
        assert task.name == "巡检"
        assert task.description == "每5分钟"
        assert task.enabled is True

    def test_auto_task_id(self) -> None:
        tm = TaskManager()
        first = tm.add("* * * * *", noop)
        second = tm.add("* * * * *", noop)
        assert first.task_id.startswith("task-")
        assert second.task_id.startswith("task-")
        assert first.task_id != second.task_id

    def test_display_name_falls_back_to_id(self) -> None:
        tm = TaskManager()
        unnamed = tm.add("* * * * *", noop, task_id="x")
        named = tm.add("* * * * *", noop, task_id="y", name="点名")
        assert unnamed.display_name == "x"
        assert named.display_name == "点名"

    def test_duplicate_id_rejected(self) -> None:
        tm = TaskManager()
        tm.add("* * * * *", noop, task_id="dup")
        with pytest.raises(ValueError, match="已存在"):
            tm.add("*/5 * * * *", noop, task_id="dup")

    def test_accepts_parsed_cron_expr(self) -> None:
        tm = TaskManager()
        task = tm.add(CronExpr.parse("*/5 * * * *"), noop)
        assert task.cron == CronExpr.parse("*/5 * * * *")

    def test_bad_cron_rejected(self) -> None:
        tm = TaskManager()
        with pytest.raises(CronError):
            tm.add("60 * * * *", noop)

    def test_non_callable_rejected(self) -> None:
        tm = TaskManager()
        with pytest.raises(TypeError, match="可调用"):
            tm.add("* * * * *", "不是函数")  # pyright: ignore[reportArgumentType]


class TestRemoveAndGet:
    def test_remove_existing_returns_true(self) -> None:
        tm = TaskManager()
        tm.add("* * * * *", noop, task_id="gone")
        assert tm.remove("gone") is True
        assert tm.list() == []

    def test_remove_missing_returns_false(self) -> None:
        assert TaskManager().remove("nope") is False

    def test_get_missing_raises_with_existing_list(self) -> None:
        tm = TaskManager()
        tm.add("* * * * *", noop, task_id="here")
        with pytest.raises(KeyError, match="here"):
            tm.get("missing")


class TestList:
    def test_enabled_only_filter(self) -> None:
        tm = TaskManager()
        tm.add("* * * * *", noop, task_id="on")
        tm.add("* * * * *", noop, task_id="off", enabled=False)
        assert {task.task_id for task in tm.list()} == {"on", "off"}
        assert [task.task_id for task in tm.list(enabled_only=True)] == ["on"]

    def test_sorted_by_next_run_with_none_last(self) -> None:
        tm = TaskManager()
        soon = tm.add("* * * * *", noop, task_id="soon")
        later = tm.add("* * * * *", noop, task_id="later")
        pending = tm.add("* * * * *", noop, task_id="pending")
        soon.next_run = datetime(2026, 9, 15, 10, 0)
        later.next_run = datetime(2026, 9, 15, 11, 0)
        # pending.next_run 保持 None，排最后
        assert [task.task_id for task in tm.list()] == ["soon", "later", "pending"]


class TestSetters:
    def test_set_enabled_toggles(self) -> None:
        tm = TaskManager()
        task = tm.add("* * * * *", noop, task_id="t")
        tm.set_enabled("t", False)
        assert task.enabled is False
        assert task.next_run is None
        tm.set_enabled("t", True)
        assert task.enabled is True

    def test_set_func_swaps_function(self) -> None:
        tm = TaskManager()
        task = tm.add("* * * * *", noop, task_id="t")

        def replacement() -> None: ...

        tm.set_func("t", replacement)
        assert task.func is replacement

    def test_set_func_rejects_non_callable(self) -> None:
        tm = TaskManager()
        tm.add("* * * * *", noop, task_id="t")
        with pytest.raises(TypeError, match="可调用"):
            tm.set_func("t", 123)  # pyright: ignore[reportArgumentType]

    def test_set_cron_accepts_str_and_expr(self) -> None:
        tm = TaskManager()
        task = tm.add("* * * * *", noop, task_id="t")
        tm.set_cron("t", "*/10 * * * *")
        assert task.cron == CronExpr.parse("*/10 * * * *")
        expr = CronExpr.parse("0 0 * * *")
        tm.set_cron("t", expr)
        assert task.cron is expr

    def test_set_cron_rejects_bad_expression(self) -> None:
        tm = TaskManager()
        tm.add("* * * * *", noop, task_id="t")
        with pytest.raises(CronError):
            tm.set_cron("t", "* * * 13 *")

    def test_set_name_and_description(self) -> None:
        tm = TaskManager()
        tm.add("* * * * *", noop, task_id="t")
        tm.set_name("t", "新名字")
        tm.set_description("t", "新描述")
        task = tm.get("t")
        assert task.name == "新名字"
        assert task.description == "新描述"


class TestPreviewAndRunNow:
    def test_preview_returns_increasing_hits(self) -> None:
        tm = TaskManager()
        moments = tm.preview("*/15 * * * *", n=4)
        assert len(moments) == 4
        assert all(moment.minute % 15 == 0 for moment in moments)
        assert all(moment.second == 0 for moment in moments)
        assert moments == sorted(moments)
        assert len(set(moments)) == 4  # 严格递增，不重复

    def test_preview_accepts_expr(self) -> None:
        tm = TaskManager()
        assert len(tm.preview(CronExpr.parse("0 0 * * *"), n=2)) == 2

    def test_preview_bad_cron_raises(self) -> None:
        with pytest.raises(CronError):
            TaskManager().preview("0 0 31 2 *", n=1)

    async def test_run_now_executes_without_schedule(self) -> None:
        """run_now 立刻跑一次，next_run 不被挪动。"""
        tm = TaskManager()
        hits: list[int] = []

        async def job() -> None:
            hits.append(1)

        task = tm.add("0 0 1 1 *", job, task_id="rare")  # 一年一遇，本不会跑
        runner = tm.run_now("rare")
        await asyncio.wait_for(runner, timeout=2.0)
        assert hits == [1]
        assert task.run_count == 1
        assert task.last_ok is True

    async def test_run_now_missing_task_raises(self) -> None:
        tm = TaskManager()
        with pytest.raises(KeyError):
            tm.run_now("nope")
