"""归属清单入口：``GET <prefix>/owners``（管理员的筛选下拉用它）。

管理员能看到所有人的工作流 / 机器人，列表要按归属筛（``?owner_id=``）—— 筛之前得先知道
「有哪些归属」，就是这里给的；昵称一起带上，前端不必再去查人。
"""
from __future__ import annotations

from .responses import OwnerData
from .router import router

__all__ = [
    "router",
    "OwnerData",
]
