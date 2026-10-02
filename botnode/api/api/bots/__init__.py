"""机器人管理入口：把「机器人列表 / 添加 / 启用停用 / 删除」做成 HTTP 接口（泛化版）。

P5 过渡：这是 `/api/onebot/*` 的泛化壳，底层仍复用 OneBot 的令牌注册表，先立起
「机器人」这个名字与 ``platform`` 字段；多实例 / Kook 在 P5-4 / P6 才真正落库。
"""
from __future__ import annotations

from .requests import AddBotRequest, SetBotEnabledRequest
from .responses import BotData, IssuedBotData
from .router import router

__all__ = [
    "router",
    "AddBotRequest",
    "SetBotEnabledRequest",
    "BotData",
    "IssuedBotData",
]
