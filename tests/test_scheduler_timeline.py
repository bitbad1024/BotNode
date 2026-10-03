"""时间线的单元测试：红黑树的不变量（随机增删）+ 任务队列的门面。全部用固定数据。"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest

from tickneko.core.scheduler import CronExpr
from tickneko.core.scheduler.models import Task
from tickneko.core.scheduler.timeline import Key, RBNode, RBTree, TaskTimeline

BASE = datetime(2026, 9, 15, 10, 0, 0)


def noop() -> None: ...


def make_task(task_id: str, at: datetime | None) -> Task:
    """造一个 next_run 已定好的任务（时间线只关心 next_run）。"""
    task = Task(task_id=task_id, cron=CronExpr.parse("* * * * *"), func=noop)
    task.next_run = at
    return task


def key_at(seconds: int, tag: str = "t") -> Key:
    """第 seconds 秒的排序键；tag 用来区分同一秒的不同任务。"""
    return (BASE + timedelta(seconds=seconds), tag)


def seconds_of(keys: list[Key]) -> list[float]:
    """把 key 列表换成「距基准多少秒」，断言时更好读。"""
    return [(key[0] - BASE).total_seconds() for key in keys]


def check_tree(tree: RBTree) -> None:
    """校验红黑树的全部不变量；写错了就炸在这儿（测试专用，所以看私有属性）。"""
    root = tree._root
    if root is None:
        assert len(tree) == 0 and tree.first is None
        return
    assert root.black, "根节点必须是黑的"

    def walk(node: RBNode | None) -> int:
        """返回到叶子的黑高，顺带查「红节点不能有红孩子」和「左右黑高相等」。"""
        if node is None:
            return 1
        left, right = walk(node.left), walk(node.right)
        assert left == right, f"{node.key} 左右黑高不一致：{left} / {right}"
        if node.red:
            assert node.left is None or node.left.black, f"{node.key} 红节点有红孩子"
            assert node.right is None or node.right.black, f"{node.key} 红节点有红孩子"
            return left
        return left + 1

    walk(root)
    keys = tree.keys()
    assert keys == sorted(keys), "中序遍历必须有序"
    assert len(keys) == len(tree)
    assert tree.first is not None and tree.first.key == keys[0], "first 必须是最小的 key"


class TestRBTree:
    def test_insert_keeps_sorted_order(self) -> None:
        tree = RBTree()
        task = make_task("t", BASE)
        for seconds in (5, 1, 9, 3, 7, 0):
            tree.insert(key_at(seconds, f"t{seconds}"), task)
        assert seconds_of(tree.keys()) == [0, 1, 3, 5, 7, 9]
        assert len(tree) == 6
        assert tree.first is not None and tree.first.key == key_at(0, "t0")

    def test_duplicate_keys_allowed(self) -> None:
        tree = RBTree()
        first, second = make_task("a", BASE), make_task("b", BASE)
        tree.insert(key_at(1, "same"), first)
        tree.insert(key_at(1, "same"), second)  # 完全相同的 key 插到右子树，不丢
        assert len(tree) == 2
        assert [node.value for node in (tree.first, tree._successor(tree.first))] == [first, second]

    def test_pop_first_yields_sorted_order(self) -> None:
        rng = random.Random(7)
        keys = [key_at(rng.randrange(500), f"t{i}") for i in range(200)]
        tree = RBTree()
        task = make_task("t", BASE)
        for key in keys:
            tree.insert(key, task)
        popped: list[Key] = []
        while True:
            node = tree.pop_first()
            if node is None:
                break
            popped.append(node.key)
        assert popped == sorted(keys)
        assert len(tree) == 0

    def test_remove_roots_and_leaves(self) -> None:
        tree = RBTree()
        task = make_task("t", BASE)
        for seconds in range(20):
            tree.insert(key_at(seconds, f"t{seconds}"), task)
        for seconds in (0, 19, 10, 5, 15):  # 最小的、最大的、中间的
            node = tree.first
            while node is not None and node.key != key_at(seconds, f"t{seconds}"):
                node = tree._successor(node)
            assert node is not None
            tree.remove(node)
            check_tree(tree)
        assert seconds_of(tree.keys()) == [s for s in range(20) if s not in (0, 5, 10, 15, 19)]

    def test_random_insert_delete_keeps_invariants(self) -> None:
        """随机插删 400 步，每一步都验一遍不变量。"""
        rng = random.Random(2026)
        tree = RBTree()
        live: dict[Key, RBNode] = {}
        for index in range(400):
            if live and rng.random() < 0.35:
                key = rng.choice(sorted(live))
                tree.remove(live.pop(key))
            else:
                key = key_at(rng.randrange(1000), f"t{index}")
                if key in live:
                    continue
                live[key] = tree.insert(key, make_task(key[1], key[0]))
            check_tree(tree)
        assert len(tree) == len(live)
        for node in list(live.values()):
            tree.remove(node)
        assert len(tree) == 0
        assert tree.first is None
        check_tree(tree)

    def test_empty_tree(self) -> None:
        tree = RBTree()
        assert not tree
        assert tree.pop_first() is None
        assert tree.keys() == []


class TestTaskTimeline:
    def test_first_task_is_earliest(self) -> None:
        timeline = TaskTimeline()
        late = make_task("late", BASE + timedelta(minutes=5))
        early = make_task("early", BASE + timedelta(seconds=30))
        timeline.upsert(late)
        timeline.upsert(early)
        assert timeline.first_task() is early
        assert timeline.due_time() == BASE + timedelta(seconds=30)
        assert len(timeline) == 2

    def test_upsert_moves_task_without_duplicating(self) -> None:
        timeline = TaskTimeline()
        task = make_task("t", BASE + timedelta(minutes=5))
        timeline.upsert(task)
        assert len(timeline) == 1
        task.next_run = BASE  # 挪到最早
        timeline.upsert(task)
        assert len(timeline) == 1  # 是挪位置，不是又插一个
        assert timeline.first_task() is task
        assert timeline.key_of("t") == BASE

    def test_same_second_tasks_coexist(self) -> None:
        timeline = TaskTimeline()
        first = make_task("a", BASE)
        second = make_task("b", BASE)
        timeline.upsert(first)
        timeline.upsert(second)
        assert len(timeline) == 2  # 同一秒靠 task_id 区分
        assert timeline.first_task() in (first, second)

    def test_discard_and_contains(self) -> None:
        timeline = TaskTimeline()
        task = make_task("t", BASE)
        timeline.upsert(task)
        assert "t" in timeline
        timeline.discard("t")
        assert "t" not in timeline
        assert timeline.first_task() is None
        assert timeline.due_time() is None
        assert timeline.key_of("t") is None
        timeline.discard("t")  # 重复摘也不炸

    def test_clear(self) -> None:
        timeline = TaskTimeline()
        for index in range(5):
            timeline.upsert(make_task(f"t{index}", BASE + timedelta(seconds=index)))
        timeline.clear()
        assert len(timeline) == 0
        assert timeline.due_time() is None

    def test_upsert_without_next_run_rejected(self) -> None:
        timeline = TaskTimeline()
        with pytest.raises(ValueError, match="排不进队列"):
            timeline.upsert(make_task("t", None))

    def test_order_after_removals(self) -> None:
        """删掉一批之后再问「下一个是谁」，答案要一直对。"""
        rng = random.Random(11)
        timeline = TaskTimeline()
        tasks = [
            make_task(f"t{i}", BASE + timedelta(seconds=rng.randrange(100))) for i in range(50)
        ]
        for task in tasks:
            timeline.upsert(task)
        remaining = list(tasks)
        while remaining:
            earliest = min(remaining, key=lambda task: (task.next_run, task.task_id))  # type: ignore[arg-type,return-value]
            assert timeline.first_task() is earliest
            timeline.discard(earliest.task_id)
            remaining.remove(earliest)
        assert timeline.first_task() is None
