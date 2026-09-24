"""工作流接口的请求体（只描述「进来长什么样」）。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nacho.workflow import NAME_MAX_LENGTH, NOTE_MAX_LENGTH


class _Mutable(BaseModel):
    """请求体公共底：允许额外字段（前端画布可能带坐标等 UI 数据，校验器会自行忽略）。"""

    model_config = ConfigDict(extra="allow")


class CreateWorkflowRequest(_Mutable):
    """新建工作流：只要名字（图是之后一版一版存进去的）。"""

    name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH, description="工作流名称（同账号下唯一）")


class RenameWorkflowRequest(_Mutable):
    """改名。"""

    name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH)


class SaveVersionRequest(_Mutable):
    """提交一个版本：图 + 备注。入库前先过校验流水线，不过不写库。"""

    graph: dict[str, Any] = Field(description="画布图：{nodes, edges}")
    note: str = Field(default="", max_length=NOTE_MAX_LENGTH)


class SaveDraftRequest(_Mutable):
    """暂存：把编辑中的图存进暂存区。**不做校验**（半张图也能存），提交版本时才校验。"""

    graph: dict[str, Any] = Field(description="画布图：{nodes, edges}（允许编辑到一半）")


class ValidateRequest(_Mutable):
    """只校验不保存（画布上点「校验」时用）。"""

    graph: dict[str, Any]


class PublishRequest(_Mutable):
    """发布指定版本；不传 version 就发布当前最新版本。"""

    version: int | None = Field(default=None, ge=1)
