"""机器人管理接口的响应体（泛化版：在 OneBot 令牌模型上补 ``platform`` 字段）。

P5 过渡：底层仍走 OneBot 的令牌注册表（多实例 / Kook 在 P5-4 / P6 才真正落库），
这里先把「机器人」这个名字与 ``platform`` 字段立起来——列表 / 签发带上平台标识，
OneBot 语义不变。明文令牌只在 :class:`IssuedBotData` 里出现一次。
"""
from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class BotData(BaseModel):
    """一条机器人记录（**不含明文**）。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    #: 这一行的主键（bot_id）
    id: str
    #: 底层适配器：onebot / kook（缺省 onebot）
    platform: str = Field(default="onebot", description="底层适配器（onebot / kook）")
    #: 归属用户（谁的）
    owner_id: str = Field(default="", description="归属用户（谁的）")
    #: 机器人账号（OneBot 是接入 WS 的机器人号）
    account: str = Field(default="", description="机器人账号")
    enabled: bool = True
    remark: str = ""
    created_at: float = 0.0
    #: 归属那个 id 在用户表里的昵称（查不到就是空串）
    nickname: str = Field(default="", description="归属的昵称")
    #: 派生态：此刻有没有正用它连着的在线客户端（不落库，接口层实时聚合）
    online: bool = Field(default=False, description="此刻有没有在线客户端")
    #: 派生态：正用它连着的在线客户端快照
    clients: list[object] = Field(default_factory=list, description="在线客户端（快照）")


class IssuedBotData(BaseModel):
    """添加机器人的结果。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    record: BotData
    #: 明文令牌：**只在这一次响应里出现**，之后查不回来
    token: str = Field(description="明文令牌，请立刻保存，之后无法再查")
