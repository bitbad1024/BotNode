"""工作流接口的请求体（只描述「进来长什么样」）。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tickneko.workflow import NAME_MAX_LENGTH, NOTE_MAX_LENGTH


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


class SetEnabledRequest(_Mutable):
    """拨运行开关：``true`` = 跑起来，``false`` = 停下来。

    **发布 ≠ 运行**：发布只挪发布指针，什么时候真的跑由这个开关说了算，默认不跑。
    """

    enabled: bool = Field(description="true = 跑起来（登记定时触发）；false = 停下来")


class UpdateSettingsRequest(_Mutable):
    """改工作流**设置**（现在只有「实例策略」一项）。

    以后再加设置就往这个请求体里加字段（``_Mutable`` 允许额外字段，前端先发也不会 422），
    存储侧跟着补列 / 补关键字参数即可 —— 弹窗那边是一组「一个设置一块」的结构，加一块就行。
    """

    multi_instance: bool = Field(
        default=False,
        description=(
            "实例策略：false = 单实例（上一次还没跑完就跳过本次）；"
            "true = 多实例（到点就开新实例，允许叠加）"
        ),
    )
