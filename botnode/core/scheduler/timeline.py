"""时间线：红黑树 + 哈希表二合一的任务索引（仿 Linux CFS 的 ``rb_root`` + 任务哈希表）。

一个节点同时挂在两个结构里 —— 既按时间有序，又能按 ID 直接点名：取最近触发点 O(1)
（缓存最左节点）、按 task_id 定位 O(1)、改一次排程 O(log n)。:class:`TaskTimeline`
是面向任务的门面，调度器只跟它打交道。结构与代价的详情见
``docs/scheduler/scheduler.md``。
"""
from __future__ import annotations

from datetime import datetime
from typing import final, override

from botnode.core.scheduler.models import Task

#: 排序键：(触发时刻, task_id)。带上 ID 是为了让同一秒触发的多个任务也能共存
Key = tuple[datetime, str]


@final
class RBNode:
    """红黑树节点：key / value + 颜色 + 父与左右孩子，对标内核的 ``struct rb_node``。"""

    __slots__ = ("key", "value", "parent", "left", "right", "red")

    def __init__(self, key: Key, value: Task) -> None:
        self.key: Key = key
        self.value: Task = value
        self.parent: RBNode | None = None
        self.left: RBNode | None = None
        self.right: RBNode | None = None
        #: 新节点一律先染红（少破坏黑高，只有父子双红才需要修复）
        self.red: bool = True

    @property
    def black(self) -> bool:
        """是否黑节点。"""
        return not self.red

    @override
    def __repr__(self) -> str:  # pragma: no cover 调试时看一眼
        return f"<RBNode {self.key[0]:%H:%M:%S} {self.key[1]} {'red' if self.red else 'black'}>"


@final
class RBTree:
    """按 :data:`Key` 排序的红黑树；key 完全相同的一律插到右子树（保持插入顺序）。"""

    def __init__(self) -> None:
        self._root: RBNode | None = None
        self._min: RBNode | None = None  # 内核的 rb_leftmost
        self._size: int = 0

    def __len__(self) -> int:
        """节点数。"""
        return self._size

    def __bool__(self) -> bool:
        """空树为假。"""
        return self._size > 0

    @property
    def first(self) -> RBNode | None:
        """key 最小的节点（最左）；空树返回 None。O(1)。"""
        return self._min

    def insert(self, key: Key, value: Task) -> RBNode:
        """插入一个节点并返回它，O(log n)。"""
        node = RBNode(key, value)
        parent: RBNode | None = None
        cursor = self._root
        while cursor is not None:  # 沿树走到该挂的位置
            parent = cursor
            cursor = cursor.right if cursor.key <= key else cursor.left
        node.parent = parent
        if parent is None:
            self._root = node
        elif parent.key <= key:
            parent.right = node
        else:
            parent.left = node
        self._size += 1
        if self._min is None or key < self._min.key:
            self._min = node
        self._fixup_after_insert(node)
        return node

    def remove(self, node: RBNode) -> None:
        """摘掉一个节点，O(log n)；节点会被断开所有指针，别再用它。"""
        if self._min is node:  # 先记下继任者，摘完就找不着了
            self._min = self._successor(node)
        borrowed_red = node.red
        if node.left is None:
            child, parent, from_left = node.right, node.parent, self._is_left_child(node)
            self._transplant(node, node.right)
        elif node.right is None:
            child, parent, from_left = node.left, node.parent, self._is_left_child(node)
            self._transplant(node, node.left)
        else:
            # 两个孩子：找中序后继顶上来（后继一定没有左孩子）
            heir = self._minimum(node.right)
            borrowed_red = heir.red
            child = heir.right
            if heir.parent is node:
                parent, from_left = heir, False  # 后继是右孩子时，它的右孩子接它的位置
            else:
                parent, from_left = heir.parent, self._is_left_child(heir)
                self._transplant(heir, heir.right)
                heir.right = node.right
                heir.right.parent = heir
            self._transplant(node, heir)
            heir.left = node.left
            heir.left.parent = heir
            heir.red = node.red  # 顶替者继承被摘节点的颜色
        self._size -= 1
        node.parent = node.left = node.right = None
        if not borrowed_red:
            self._fixup_after_remove(child, parent, from_left)
        if self._root is not None:
            self._root.red = False

    def pop_first(self) -> RBNode | None:
        """摘掉并返回最小节点；空树返回 None。"""
        node = self._min
        if node is not None:
            self.remove(node)
        return node

    def keys(self) -> list[Key]:
        """按序遍历的全部 key（调试 / 测试用）。"""
        result: list[Key] = []
        stack: list[RBNode] = []
        cursor = self._root
        while stack or cursor is not None:
            while cursor is not None:
                stack.append(cursor)
                cursor = cursor.left
            cursor = stack.pop()
            result.append(cursor.key)
            cursor = cursor.right
        return result

    # ---- 内部：结构操作 ----
    def _transplant(self, old: RBNode, new: RBNode | None) -> None:
        """用 new 整棵子树替换 old 的位置（不修颜色）。"""
        if old.parent is None:
            self._root = new
        elif old is old.parent.left:
            old.parent.left = new
        else:
            old.parent.right = new
        if new is not None:
            new.parent = old.parent

    def _rotate_left(self, node: RBNode) -> None:
        """左旋：node 下沉，它的右孩子上来。"""
        child = node.right
        if child is None:
            return
        node.right = child.left
        if child.left is not None:
            child.left.parent = node
        child.parent = node.parent
        if node.parent is None:
            self._root = child
        elif node is node.parent.left:
            node.parent.left = child
        else:
            node.parent.right = child
        child.left = node
        node.parent = child

    def _rotate_right(self, node: RBNode) -> None:
        """右旋：node 下沉，它的左孩子上来。"""
        child = node.left
        if child is None:
            return
        node.left = child.right
        if child.right is not None:
            child.right.parent = node
        child.parent = node.parent
        if node.parent is None:
            self._root = child
        elif node is node.parent.right:
            node.parent.right = child
        else:
            node.parent.left = child
        child.right = node
        node.parent = child

    def _fixup_after_insert(self, node: RBNode) -> None:
        """插入后修复：父子双红就靠变色 / 旋转往上推。"""
        while node.parent is not None and node.parent.red:
            parent = node.parent
            grand = parent.parent
            if grand is None:  # 父是根，不该是红的；兜底退出
                break
            if parent is grand.left:
                uncle = grand.right
                if uncle is not None and uncle.red:  # 叔叔也是红：一起染黑，往上推
                    parent.red = False
                    uncle.red = False
                    grand.red = True
                    node = grand
                    continue
                if node is parent.right:  # 内侧：先转成外侧
                    node = parent
                    self._rotate_left(node)
                    parent = node.parent
                    if parent is None:
                        break
                    grand = parent.parent
                    if grand is None:
                        break
                parent.red = False
                grand.red = True
                self._rotate_right(grand)
            else:
                uncle = grand.left
                if uncle is not None and uncle.red:
                    parent.red = False
                    uncle.red = False
                    grand.red = True
                    node = grand
                    continue
                if node is parent.left:
                    node = parent
                    self._rotate_right(node)
                    parent = node.parent
                    if parent is None:
                        break
                    grand = parent.parent
                    if grand is None:
                        break
                parent.red = False
                grand.red = True
                self._rotate_left(grand)
        if self._root is not None:
            self._root.red = False

    def _fixup_after_remove(
        self, child: RBNode | None, parent: RBNode | None, from_left: bool
    ) -> None:
        """删除后修复：摘走的是黑节点时，把「少一黑」沿路径向上消化。

        child 可能是 None（那个位置空了），所以父与方向由调用方一并传进来。
        """
        while child is not self._root and (child is None or child.black):
            if parent is None:  # 不该发生：非根节点一定有父
                break
            if from_left:
                sibling = parent.right
                if sibling is not None and sibling.red:
                    sibling.red = False
                    parent.red = True
                    self._rotate_left(parent)
                    sibling = parent.right
                if sibling is None or (  # 兄弟一家都是黑：把黑往上推一层
                    (sibling.left is None or sibling.left.black)
                    and (sibling.right is None or sibling.right.black)
                ):
                    if sibling is not None:
                        sibling.red = True
                    child, parent = parent, parent.parent
                    from_left = parent is not None and child is parent.left
                    continue
                if sibling.right is None or sibling.right.black:
                    if sibling.left is not None:
                        sibling.left.red = False
                    sibling.red = True
                    self._rotate_right(sibling)
                    sibling = parent.right
                    if sibling is None:
                        break
                sibling.red = parent.red
                parent.red = False
                if sibling.right is not None:
                    sibling.right.red = False
                self._rotate_left(parent)
                break
            else:
                sibling = parent.left
                if sibling is not None and sibling.red:
                    sibling.red = False
                    parent.red = True
                    self._rotate_right(parent)
                    sibling = parent.left
                if sibling is None or (
                    (sibling.right is None or sibling.right.black)
                    and (sibling.left is None or sibling.left.black)
                ):
                    if sibling is not None:
                        sibling.red = True
                    child, parent = parent, parent.parent
                    from_left = parent is not None and child is parent.left
                    continue
                if sibling.left is None or sibling.left.black:
                    if sibling.right is not None:
                        sibling.right.red = False
                    sibling.red = True
                    self._rotate_left(sibling)
                    sibling = parent.left
                    if sibling is None:
                        break
                sibling.red = parent.red
                parent.red = False
                if sibling.left is not None:
                    sibling.left.red = False
                self._rotate_right(parent)
                break
        if child is not None:
            child.red = False

    @staticmethod
    def _is_left_child(node: RBNode) -> bool:
        """node 是不是它爹的左孩子（没爹当 False）。"""
        return node.parent is not None and node is node.parent.left

    @staticmethod
    def _minimum(node: RBNode) -> RBNode:
        """以 node 为根的子树里最小的节点：一路向左。"""
        while node.left is not None:
            node = node.left
        return node

    def _successor(self, node: RBNode) -> RBNode | None:
        """中序后继：右子树的最小值，否则往上找第一个「从左子树上来」的拐点。"""
        if node.right is not None:
            return self._minimum(node.right)
        parent = node.parent
        while parent is not None and node is parent.right:
            node, parent = parent, parent.parent
        return parent


@final
class TaskTimeline:
    """任务时间线：一棵按触发时间排序的红黑树 + 一张 task_id -> 节点的哈希表。

    树负责「下一个是谁」（O(1)），表负责「某个任务在哪」（O(1)）。一个任务最多在树里
    挂一个节点（就是它的下一次触发点），改排程 = 换位置。
    """

    def __init__(self) -> None:
        self._tree: RBTree = RBTree()
        self._nodes: dict[str, RBNode] = {}

    def __len__(self) -> int:
        """排在队列里的任务数。"""
        return len(self._tree)

    def __contains__(self, task_id: str) -> bool:
        """这个任务在不在队列里。"""
        return task_id in self._nodes

    def upsert(self, task: Task) -> None:
        """按 task.next_run 把任务排进队列（已在队列里就挪位置）；next_run 不能是 None。"""
        if task.next_run is None:
            raise ValueError(f"任务 {task.display_name} 还没排程（next_run 是 None），排不进队列")
        key: Key = (task.next_run, task.task_id)  # 带上 ID：同一秒的多个任务也能共存
        node = self._nodes.get(task.task_id)
        if node is not None:
            if node.key == key:
                return  # 位置没变，省一次树的调整
            self._tree.remove(node)
        self._nodes[task.task_id] = self._tree.insert(key, task)

    def discard(self, task_id: str) -> None:
        """把任务从队列里摘掉；本来就不在就算了。"""
        node = self._nodes.pop(task_id, None)
        if node is not None:
            self._tree.remove(node)

    def clear(self) -> None:
        """清空队列。"""
        self._tree = RBTree()
        self._nodes.clear()

    def first_task(self) -> Task | None:
        """下一个该跑的任务（触发时间最早）；队列空返回 None。O(1)。"""
        node = self._tree.first
        return None if node is None else node.value

    def due_time(self) -> datetime | None:
        """最近的触发时刻；队列空返回 None。O(1)。"""
        node = self._tree.first
        return None if node is None else node.key[0]

    def key_of(self, task_id: str) -> datetime | None:
        """这个任务当前排在什么时刻（不在队列里返回 None）；对账用。"""
        node = self._nodes.get(task_id)
        return None if node is None else node.key[0]
