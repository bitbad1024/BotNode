"""工作流的落库实现：定义 / 版本两张表，用 SQLModel 描述、AsyncSession 读写。

表结构（类型 / 约束写在 Python 里，DDL 按方言生成，sqlite 与 mariadb 共用一份）::

    workflow_definitions          一个工作流一行（元数据 + 版本指针）
        id                 VARCHAR(64)  PRIMARY KEY
        owner_id           VARCHAR(64)  INDEX            归属用户（多用户隔离的过滤列）
        name               VARCHAR(128)                  同归属下唯一
        status             VARCHAR(16)  DEFAULT 'draft'  draft / published
        current_version    INTEGER      DEFAULT 0        最近保存的版本号
        published_version  INTEGER      DEFAULT 0        已发布版本号（0 = 没发布过）
        created_at / updated_at        FLOAT             Unix 秒
        UNIQUE(owner_id, name)

    workflow_versions             每次保存一张不可变图快照
        id            VARCHAR(64)  PRIMARY KEY
        workflow_id   VARCHAR(64)  INDEX
        owner_id      VARCHAR(64)  INDEX  冗余归属，列表 / 鉴权少一次 join
        version       INTEGER             同一工作流内自增
        graph_json    TEXT                规范 JSON 快照
        checksum      VARCHAR(64)         graph_json 的 sha256（内容没变不新增版本）
        note          VARCHAR(255)
        created_at    FLOAT
        UNIQUE(workflow_id, version)

引擎由外部注入（同用户 / 会话 / 令牌存储的惯例），本模块不建引擎、不读配置。
"""
from __future__ import annotations

import time
from uuid import uuid4

from sqlalchemy import Column, Text, UniqueConstraint
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.sql import func
from sqlmodel import Field, SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from .models import (
    WorkflowDefinitionRecord,
    WorkflowVersionRecord,
)

#: 名称长度上限（建表长度与接口校验对齐用）
NAME_MAX_LENGTH: int = 128
NOTE_MAX_LENGTH: int = 255


class WorkflowError(Exception):
    """工作流存储层错误的基类（消息直接给人看）。"""


class WorkflowNameConflict(WorkflowError):
    """同归属下已存在同名工作流（UNIQUE(owner_id, name) 撞了）。"""

    def __init__(self, owner_id: str, name: str) -> None:
        super().__init__(f"已存在同名工作流：{name}")
        self.owner_id = owner_id
        self.name = name


class WorkflowDefinitionTable(SQLModel, table=True):
    """``workflow_definitions`` 表：工作流元数据与版本指针。"""

    # SQLModel 默认按类名生成表名，显式钉死；库自身类型缺陷故定向忽略（同其它表）。
    __tablename__ = "workflow_definitions"  # pyright: ignore[reportAssignmentType, reportUnannotatedClassAttribute]
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_workflow_owner_name"),)

    id: str = Field(primary_key=True, max_length=64)
    owner_id: str = Field(index=True, max_length=64)
    name: str = Field(max_length=NAME_MAX_LENGTH)
    # 不用 Literal 标注：SQLModel 建列不接受非 class 的类型（issubclass 报错），取值约束在业务层
    status: str = Field(default="draft", max_length=16)
    current_version: int = Field(default=0)
    published_version: int = Field(default=0)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class WorkflowVersionTable(SQLModel, table=True):
    """``workflow_versions`` 表：图的不可变版本快照。"""

    __tablename__ = "workflow_versions"  # pyright: ignore[reportAssignmentType, reportUnannotatedClassAttribute]
    __table_args__ = (UniqueConstraint("workflow_id", "version", name="uq_workflow_version"),)

    id: str = Field(primary_key=True, max_length=64)
    workflow_id: str = Field(index=True, max_length=64)
    owner_id: str = Field(index=True, max_length=64)
    version: int
    #: 图快照原文；sqlite/mariadb 都用 TEXT 存 JSON 字符串（与 logs.extra、users.roles 同惯例）
    graph_json: str = Field(sa_column=Column(Text(), nullable=False))
    checksum: str = Field(max_length=64)
    note: str = Field(default="", max_length=NOTE_MAX_LENGTH)
    created_at: float = Field(default_factory=time.time)


def _definition_to_record(row: WorkflowDefinitionTable) -> WorkflowDefinitionRecord:
    return WorkflowDefinitionRecord(
        id=row.id,
        owner_id=row.owner_id,
        name=row.name,
        status=row.status,
        current_version=int(row.current_version),
        published_version=int(row.published_version),
        created_at=float(row.created_at),
        updated_at=float(row.updated_at),
    )


def _version_to_record(row: WorkflowVersionTable) -> WorkflowVersionRecord:
    return WorkflowVersionRecord(
        id=row.id,
        workflow_id=row.workflow_id,
        owner_id=row.owner_id,
        version=int(row.version),
        graph_json=row.graph_json,
        checksum=row.checksum,
        note=row.note,
        created_at=float(row.created_at),
    )


class SqlWorkflowStore:
    """工作流放数据库：定义 + 版本两张表（用法同
    :class:`~nacho.api.services.user.store_sql.SqlUserStore`，引擎由入口层注入）。

    所有查询都要求显式给归属条件——本层不区分管理员（跨归属是路由层的决定），
    但方法签名把 ``owner_id`` 放明面上，漏写隔离条件这件事在代码审查时一眼可见。
    """

    def __init__(self, engine: AsyncEngine) -> None:
        """:param engine: 异步引擎（由入口层按 DatabaseSettings 建好传进来）。"""
        self._engine: AsyncEngine = engine
        # commit 后不把属性置过期：异步下再取属性会触发隐式刷新（lazy load）而报错
        self._sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )

    # ------------------------------------------------------------------ 启动
    async def ensure_schema(self) -> None:
        """建表（幂等）：DDL 按方言生成，已有的表跳过。"""
        async with self._engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    # ------------------------------------------------------------------ 定义
    async def create(self, owner_id: str, name: str) -> WorkflowDefinitionRecord:
        """新建一个空工作流（还没有任何版本）；同归属同名 -> :class:`WorkflowNameConflict`。"""
        now = time.time()
        row = WorkflowDefinitionTable(
            id=uuid4().hex,
            owner_id=owner_id,
            name=name,
            status="draft",
            current_version=0,
            published_version=0,
            created_at=now,
            updated_at=now,
        )
        try:
            async with self._sessions() as session:
                session.add(row)
                await session.commit()
        except IntegrityError as exc:
            raise WorkflowNameConflict(owner_id, name) from exc
        return _definition_to_record(row)

    async def get(self, workflow_id: str) -> WorkflowDefinitionRecord | None:
        """按 id 取定义（不做归属判断；调用方负责隔离）。"""
        async with self._sessions() as session:
            row = await session.get(WorkflowDefinitionTable, workflow_id)
            return _definition_to_record(row) if row is not None else None

    async def list(
        self,
        *,
        owner_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[WorkflowDefinitionRecord]:
        """列定义；``owner_id`` 给值只列该归属（普通用户），``None`` = 全归属（管理员视图）。"""
        statement = select(WorkflowDefinitionTable)
        if owner_id is not None:
            statement = statement.where(WorkflowDefinitionTable.owner_id == owner_id)
        statement = statement.order_by(WorkflowDefinitionTable.updated_at.desc())
        statement = statement.limit(limit).offset(offset)
        async with self._sessions() as session:
            rows = await session.exec(statement)
            return [_definition_to_record(row) for row in rows.all()]

    async def rename(self, workflow_id: str, name: str) -> WorkflowDefinitionRecord | None:
        """改名；不存在返回 None，同归属同名撞唯一键抛 :class:`WorkflowNameConflict`。"""
        try:
            async with self._sessions() as session:
                row = await session.get(WorkflowDefinitionTable, workflow_id)
                if row is None:
                    return None
                row.name = name
                row.updated_at = time.time()
                session.add(row)
                await session.commit()
                return _definition_to_record(row)
        except IntegrityError as exc:
            async with self._sessions() as session:
                current = await session.get(WorkflowDefinitionTable, workflow_id)
                owner = current.owner_id if current is not None else ""
            raise WorkflowNameConflict(owner, name) from exc

    async def delete(self, workflow_id: str) -> bool:
        """删定义及其**全部版本**（一个事务）；删过了 / 不存在返回 False。"""
        async with self._sessions() as session:
            row = await session.get(WorkflowDefinitionTable, workflow_id)
            if row is None:
                return False
            versions = await session.exec(
                select(WorkflowVersionTable).where(
                    WorkflowVersionTable.workflow_id == workflow_id
                )
            )
            for version_row in versions.all():
                await session.delete(version_row)
            await session.delete(row)
            await session.commit()
            return True

    # ------------------------------------------------------------------ 版本
    async def add_version(
        self,
        definition: WorkflowDefinitionRecord,
        *,
        graph_json: str,
        checksum: str,
        note: str = "",
    ) -> tuple[WorkflowVersionRecord, bool]:
        """存一个新版本。

        * 内容摘要与**当前最新版本**相同 -> 不新增行，返回 ``(那条版本, False)``
          （前端连点保存、图没动，不产生垃圾版本）；
        * 否则在一个事务里取 ``max(version)+1`` 插快照，并把定义的
          ``current_version`` 指针挪过去，返回 ``(新版本, True)``。
        """
        async with self._sessions() as session:
            latest = (
                await session.exec(
                    select(WorkflowVersionTable)
                    .where(WorkflowVersionTable.workflow_id == definition.id)
                    .order_by(WorkflowVersionTable.version.desc())
                    .limit(1)
                )
            ).first()
            if latest is not None and latest.checksum == checksum:
                return _version_to_record(latest), False

            definition_row = await session.get(WorkflowDefinitionTable, definition.id)
            if definition_row is None:
                # 定义在并发中已被删：不插版本，交给上层按「不存在」处理
                raise WorkflowError(f"工作流不存在：{definition.id}")

            next_version = (latest.version + 1) if latest is not None else 1
            row = WorkflowVersionTable(
                id=uuid4().hex,
                workflow_id=definition.id,
                owner_id=definition.owner_id,
                version=next_version,
                graph_json=graph_json,
                checksum=checksum,
                note=note[:NOTE_MAX_LENGTH],
                created_at=time.time(),
            )
            session.add(row)
            definition_row.current_version = next_version
            definition_row.updated_at = time.time()
            session.add(definition_row)
            await session.commit()
            return _version_to_record(row), True

    async def list_versions(self, workflow_id: str) -> list[WorkflowVersionRecord]:
        """版本历史（版本号倒序）。"""
        async with self._sessions() as session:
            rows = await session.exec(
                select(WorkflowVersionTable)
                .where(WorkflowVersionTable.workflow_id == workflow_id)
                .order_by(WorkflowVersionTable.version.desc())
            )
            return [_version_to_record(row) for row in rows.all()]

    async def get_version(
        self, workflow_id: str, version: int
    ) -> WorkflowVersionRecord | None:
        """取指定版本快照。"""
        async with self._sessions() as session:
            row = (
                await session.exec(
                    select(WorkflowVersionTable)
                    .where(WorkflowVersionTable.workflow_id == workflow_id)
                    .where(WorkflowVersionTable.version == version)
                )
            ).first()
            return _version_to_record(row) if row is not None else None

    async def publish(
        self, workflow_id: str, version: int
    ) -> WorkflowDefinitionRecord | None:
        """把 ``version`` 标记为已发布（status=published + 挪 published_version 指针）。

        版本不存在（含没存过任何版本的 0）时定义原样返回 None，由上层报 404 / 409。
        """
        async with self._sessions() as session:
            version_row = (
                await session.exec(
                    select(WorkflowVersionTable)
                    .where(WorkflowVersionTable.workflow_id == workflow_id)
                    .where(WorkflowVersionTable.version == version)
                )
            ).first()
            if version_row is None:
                return None
            row = await session.get(WorkflowDefinitionTable, workflow_id)
            if row is None:
                return None
            row.status = "published"
            row.published_version = version
            row.updated_at = time.time()
            session.add(row)
            await session.commit()
            return _definition_to_record(row)

    # 供上层做「最新版本号」之类判断时少写一次查询（保留位，目前版本自增在 add_version 内部）
    async def max_version(self, workflow_id: str) -> int:
        """当前最大版本号；没有版本时为 0。"""
        async with self._sessions() as session:
            value = await session.exec(
                select(func.max(WorkflowVersionTable.version)).where(
                    WorkflowVersionTable.workflow_id == workflow_id
                )
            )
            return int(value.first() or 0)
