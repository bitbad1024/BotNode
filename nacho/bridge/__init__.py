"""桥接层（bridge）：把各平台适配器统一到一套「规范化事件 + 能力协议」上。

这一层解决什么：接入层每多一个平台（OneBot / Kook / …），不该让图、节点、令牌列表跟着改。
做法是老一套「结构化协议、互不 import」（同 :mod:`nacho.api.api.onebot.protocols`）：

* ``models.py``     规范化数据形状：PlatformEvent（事件）/ BotClient（在线一行）/ ActionResult（动作回执）
* ``protocols.py``  BotAdapter 协议：一个平台适配器长什么样
* ``gateway.py``    Gateway 总线：注册适配器、订阅事件、按平台路由发送
* ``onebot.py``     第一个适配器：把 nacho.onebot.OneBotServer 包进来

依赖方向：本包只依赖 ``nacho.core``（logger），**不** import ``nacho.onebot`` /
``nacho.api`` / ``nacho.workflow`` —— 平台的类型只在适配器实现里出现，协议这边一律
``object`` / 结构化形状（PEP 563 惰性注解兜底）。
"""
from __future__ import annotations

from .models import ActionResult, BotClient, PlatformEvent
from .protocols import BotAdapter

__all__ = [
    # 规范化模型
    "PlatformEvent",
    "BotClient",
    "ActionResult",
    # 协议
    "BotAdapter",
]
