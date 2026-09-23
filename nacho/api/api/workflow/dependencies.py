"""工作流路由的注入件：取存储、认归属。

隔离口径与 OneBot 管理一致：

* 普通用户只能碰 **owner_id = 自己 id** 的工作流；管理员（``admin`` 角色）不限；
* 列表：普通用户强制只看自己；管理员默认看全部，可用 ``?owner_id=`` 缩到某个归属；
* 按 id 取：不存在和「是别人的」走**同一个 404**——不拿 id 试探出别人有没有工作流。
"""
from __future__ import annotations

from typing import Annotated, Protocol, cast

from fastapi import Depends, Request

from ...common.errors import ApiError, ErrorCode
from ...services.auth.models import CurrentUser
from ..auth.dependencies import CurrentUserDep
from fastapi import status as http_status

from nacho.workflow import SqlWorkflowStore, WorkflowDefinitionRecord

#: 管理员角色名（与演示账号约定一致）
ADMIN_ROLE: str = "admin"


class _AppState(Protocol):
    """挂在 ``app.state`` 上、本模块要用到的东西（由 :func:`nacho.api.create_app` 写入）。"""

    workflow_store: SqlWorkflowStore


class _App(Protocol):
    state: _AppState


def get_workflow_store(request: Request) -> SqlWorkflowStore:
    """取工作流存储：装配时挂在 ``app.state.workflow_store`` 上。"""
    app = cast("_App", request.app)
    return app.state.workflow_store


#: 依赖简写：路由函数里写 ``store: WorkflowStoreDep`` 即可
WorkflowStoreDep = Annotated[SqlWorkflowStore, Depends(get_workflow_store)]


def is_admin(user: CurrentUser) -> bool:
    """管理员能跨归属操作。"""
    return ADMIN_ROLE in user.user.roles


async def get_in_scope(
    store: SqlWorkflowStore, user: CurrentUser, workflow_id: str
) -> WorkflowDefinitionRecord:
    """按 id 取工作流并做归属把关；不存在 / 越界统一 **404**（不泄露存在性）。"""
    record = await store.get(workflow_id)
    if record is None or (not is_admin(user) and record.owner_id != user.user.id):
        raise ApiError(
            ErrorCode.HTTP_ERROR, "没有这个工作流", status_code=http_status.HTTP_404_NOT_FOUND
        )
    return record


def owner_filter_of(user: CurrentUser, owner_id: str | None) -> str | None:
    """列表的归属过滤：普通用户永远只看自己（忽略 query）；管理员默认全部。"""
    if is_admin(user):
        return owner_id
    return user.user.id


__all__ = [
    "ADMIN_ROLE",
    "CurrentUserDep",
    "WorkflowStoreDep",
    "get_workflow_store",
    "get_in_scope",
    "is_admin",
    "owner_filter_of",
]
