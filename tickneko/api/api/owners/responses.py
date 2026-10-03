"""归属清单的响应体。"""
from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class OwnerData(BaseModel):
    """一个归属（用户）—— 筛选下拉里的一行。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    #: 归属标识：工作流 / 机器人上存的就是它（用户 id）
    owner_id: str = Field(description="归属标识（用户 id）")
    #: 登录账号：管理员认人时比昵称更可靠（昵称可能重名、也可能没设过）
    account: str = Field(default="", description="登录账号")
    #: 昵称：列表 / 下拉优先显示它，空串表示没设过
    nickname: str = Field(default="", description="昵称（空串 = 没设过）")
