"""桥接层（bridge）：把各平台适配器统一到一套「规范化事件 + 能力协议」上。

这一层解决什么：接入层每多一个平台（OneBot / Kook / …），不该让图、节点、令牌列表跟着改。
做法是老一套「结构化协议、互不 import」（同 :mod:`nacho.api.api.onebot.protocols`）：

* ``models.py``     规范化数据形状：PlatformEvent（事件）/ BotClient（在线一行）/ ActionResult（动作回执）
* ``protocols.py``  BotAdapter 协议：一个平台适配器长什么样
* ``gateway.py``    Gateway 总线：注册适配器、订阅事件、按平台路由发送
* ``onebot.py``     第一个适配器：把 nacho.onebot.OneBotServer 包进来（**可选**：只有它
                    import ``nacho.onebot``，且只有 ``pip install "nacho[onebot]"`` 才装得上；
                    这里刻意不 re-export —— import 本包不应连带要求 websockets）
* ``kook.py``       第二个适配器：把 nacho.kook.KookClient 包进来（**可选**：只有它
                    import ``nacho.kook``）。与 OneBot 方向相反 —— OneBot 是反向 WS
                    （框架当服务端），Kook 是正向 WS（框架当客户端、Bot Token 鉴权）——
                    用来验证 ``BotAdapter`` 协议对「方向相反」的平台同样通用。

依赖方向：本包只依赖 ``nacho.core``（logger），**不** import ``nacho.onebot`` /
``nacho.api`` / ``nacho.workflow`` —— 平台的类型只在适配器实现里出现，协议这边一律
``object`` / 结构化形状（PEP 563 惰性注解兜底）。

------------------------------------------------------------------------ 怎么写第二个适配器
照 :mod:`nacho.bridge.onebot` 的样子（Kook 已经照此写了一个，四步）：

1. **平台接入**（如果还没有独立包）：像 ``nacho/onebot/`` / ``nacho/kook/`` 那样只管
   「连接 + 协议」—— 鉴权、连接管理、事件分发（handler 钩子）、动作发送；**业务不进去**。
   **方向先想清**：OneBot 是反向 WS（框架当服务端），Kook 是正向 WS（框架当客户端）——
   底层「监听」还是「连出去」完全不同，但上层适配器长得一样，这正是本层要的抽象；
2. **适配器** ``nacho/bridge/<平台>.py``：包住平台服务端 / 客户端，实现
   :class:`~nacho.bridge.protocols.BotAdapter` 的能力（``platform`` / ``start`` /
   ``stop`` / ``clients`` / ``send``）；平台事件在 handler 里翻译成
   :class:`~nacho.bridge.models.PlatformEvent`（身份转字符串、会话指向归一、翻译不了的
   挂 ``raw``）后调 ``publish`` 投给 Gateway。这个模块是 bridge 里**唯一**允许
   import 平台包的地方；
3. **装配** ``bootstrap.py``：读该平台的配置（``[kook]`` 这类）-> 建适配器
   （``publish=gateway.publish``）-> ``gateway.register``；凭证形态（OneBot 的令牌表、
   Kook 的 Bot Token）各平台自定，不套用别家的；
4. **下游不动**：接口层与工作流认的是 Gateway / 适配器的形状，只要兼容面还在，
   新平台上线**不碰** ``router.py`` / 节点 / 前端 —— 这是本层存在的全部意义。

装配形态（bootstrap 现在的样子，两个平台并存）::

    gateway = Gateway()
    gateway.register(OneBotAdapter(options, publish=gateway.publish, tokens=registry))
    gateway.register(KookAdapter(KookOptions(token="..."), publish=gateway.publish))  # 配了 token 才建
    gateway.subscribe(on_platform_event)   # 业务只认规范化事件，不认平台
    await gateway.start()                  # -> 各 adapter.start()

发送走总线（按平台路由，环境问题当场抛）::

    await gateway.send("onebot", owner_id, "send_private_msg", user_id=..., message=...)
    await gateway.send("kook", owner_id, "send_channel_msg", target_id=..., content=...)
"""
from __future__ import annotations

from .gateway import EventSubscriber, Gateway
from .models import ActionResult, BotClient, PlatformEvent
from .protocols import BotAdapter

__all__ = [
    # 规范化模型
    "PlatformEvent",
    "BotClient",
    "ActionResult",
    # 协议
    "BotAdapter",
    # 总线
    "Gateway",
    "EventSubscriber",
]
