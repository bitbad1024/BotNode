"""接口层需要的那点工作流能力（**结构化协议**，不认实现类）。

为什么要绕一层协议：工作流是**另一块业务**，实现 :class:`botnode.workflow.SqlWorkflowStore`
连着建表 / 迁移 / 会话池，往后还可能换落点（内存、别的库）。接口层只负责「把图存取出来」，
不该把自己焊在某个实现上——所以这里只声明「要用到哪些方法」，由实现**结构化满足**，
:func:`botnode.api.create_app` 只认这个协议（同一套路子见
:mod:`botnode.api.api.onebot.protocols` 与 :mod:`botnode.api.services.user.protocols`）。

与 OneBot 那边不同的是：**数据对象仍是 ``botnode.workflow`` 的模型**
（``WorkflowDefinitionRecord`` / ``WorkflowVersionRecord``）。那是接口层与图形层的**契约**
（字段语义、JSON 形状），不是实现——接口层本来就在用 ``botnode.workflow`` 的校验器与规范序列化，
再复制一层 DTO 只会多两处要对齐。

装配时主程序把存储挂到 ``app.state.workflow_store`` 上，路由按这里的协议取用
（见 :func:`.dependencies.get_workflow_store`）。
"""
from __future__ import annotations

from typing import Protocol

from botnode.workflow import WorkflowDefinitionRecord, WorkflowVersionRecord


class WorkflowStoreLike(Protocol):
    """工作流存储：**接口层用到的这部分**（定义增删查改 + 暂存 / 版本 / 发布）。

    签名与 :class:`botnode.workflow.SqlWorkflowStore` 一一对应（那边就是默认实现）。
    只列**这一层用得上的**：建表留了个口（``ensure_schema``，启动兜底要调），运行期读取
    （``max_version`` 等）是运行层的活，不进这份协议。
    """

    async def ensure_schema(self) -> None:
        """建表（幂等）。路由不调，是启动时的兜底建表（见 ``create_app`` 的 lifespan）。"""
        ...

    async def create(self, owner_id: str, name: str) -> WorkflowDefinitionRecord:
        """新建一个空工作流（还没有任何版本）；同归属同名抛 ``WorkflowNameConflict``。"""
        ...

    async def get(self, workflow_id: str) -> WorkflowDefinitionRecord | None:
        """按 id 取定义（不做归属判断；隔离由路由层把关）。"""
        ...

    async def list(
        self,
        *,
        owner_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[WorkflowDefinitionRecord]:
        """列定义；``owner_id`` 给值只列该归属，``None`` = 全归属（管理员视图）。"""
        ...

    async def rename(self, workflow_id: str, name: str) -> WorkflowDefinitionRecord | None:
        """改名；不存在返回 ``None``，同归属同名抛 ``WorkflowNameConflict``。"""
        ...

    async def delete(self, workflow_id: str) -> bool:
        """删定义及其**全部版本**；不在 / 删过了返回 ``False``。"""
        ...

    async def save_draft(
        self, workflow_id: str, graph_json: str
    ) -> WorkflowDefinitionRecord | None:
        """把图存进**暂存区**（覆盖式，不校验、不产生版本），并把指针切到 ``draft``。"""
        ...

    async def add_version(
        self,
        definition: WorkflowDefinitionRecord,
        *,
        graph_json: str,
        checksum: str,
        note: str = "",
    ) -> tuple[WorkflowVersionRecord, bool]:
        """存一个新版本；返回 ``(版本, 这次是否真的新增)``（同 checksum 不新增，去重）。"""
        ...

    async def list_versions(self, workflow_id: str) -> list[WorkflowVersionRecord]:
        """版本历史（版本号倒序）。"""
        ...

    async def get_version(
        self, workflow_id: str, version: int
    ) -> WorkflowVersionRecord | None:
        """取指定版本快照；没有返回 ``None``。"""
        ...

    async def publish(
        self, workflow_id: str, version: int
    ) -> WorkflowDefinitionRecord | None:
        """把 ``version`` 标记为已发布（status + 挪发布指针）；版本不存在返回 ``None``。"""
        ...

    async def set_enabled(
        self, workflow_id: str, enabled: bool
    ) -> WorkflowDefinitionRecord | None:
        """拨**运行开关**（发布 ≠ 运行）；不存在返回 ``None``。"""
        ...

    async def update_settings(
        self, workflow_id: str, *, multi_instance: bool
    ) -> WorkflowDefinitionRecord | None:
        """改工作流**设置**（现在是实例策略：单实例 / 多实例）；不存在返回 ``None``。"""
        ...


class WorkflowTriggerLike(Protocol):
    """启停某个已发布版本的定时触发（对应 :class:`botnode.workflow.runtime.WorkflowTriggers`）。

    接口层的「运行开关」靠它**即时生效**：拨开就登记、关掉就摘任务。没注入实现时（直接
    ``create_app`` 的测试 / 示例）开关只落库，生效点推迟到下次启动载入 —— 所以路由里这份
    依赖是**可空**的，有就即时启停、没有就只管落库。
    """

    async def start(self, workflow_id: str, version: int) -> int:
        """登记这一版的时间触发（幂等），返回跑过的开始节点数量。"""
        ...

    async def stop(self, workflow_id: str, version: int) -> int:
        """摘掉这一版登记过的定时任务（重复调用无害），返回摘掉的数量。"""
        ...
