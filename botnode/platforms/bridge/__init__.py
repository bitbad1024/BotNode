"""桥接层（bridge）：把各平台适配器统一到一套「规范化事件 + 能力协议」上。

多接一个平台不该让图、节点、令牌列表跟着改 —— 做法是「结构化协议、互不 import」：
``models`` 定规范化形状、``protocols`` 定适配器长什么样、``gateway`` 是总线，
``onebot`` / ``kook`` 是两个**（可选）**适配器（只有它们各自 import 对应平台包，本包
不 re-export），``manager`` 把凭证落库 + 生命周期统一成接口层认的 ``BotsService``。

依赖方向：本包只依赖 ``botnode.core``（logger），**不** import ``botnode.platforms.onebot`` /
``botnode.api`` / ``botnode.workflow``。装配形态、发送走总线、怎么写第二个适配器见
``docs/bridge/bridge.md``。
"""
from __future__ import annotations

from .gateway import EventSubscriber, Gateway
from .logging import BRIDGE_LOGGER_NAME, bridge_logger
from .models import ActionResult, BotClient, ChatTarget, PlatformEvent
from .protocols import BotAdapter

__all__ = [
    # 日志接入点
    "BRIDGE_LOGGER_NAME",
    # 规范化模型
    "ActionResult",
    # 协议
    "BotAdapter",
    "BotClient",
    "ChatTarget",
    # 总线
    "EventSubscriber",
    "Gateway",
    "PlatformEvent",
    "bridge_logger",
]
