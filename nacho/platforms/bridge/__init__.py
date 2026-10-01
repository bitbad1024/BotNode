"""桥接层（bridge）：把各平台适配器统一到一套「规范化事件 + 能力协议」上。

这一层解决什么：接入层每多一个平台（OneBot / Kook / …），不该让图、节点、令牌列表跟着改。
做法是老一套「结构化协议、互不 import」（同 :mod:`nacho.api.api.onebot.protocols`）：

* ``models.py``     规范化数据形状：PlatformEvent（事件）/ BotClient（在线一行）/ ActionResult（动作回执）
* ``protocols.py``  BotAdapter 协议：一个平台适配器长什么样
* ``gateway.py``    Gateway 总线：注册适配器、订阅事件、按平台路由发送
* ``onebot.py``     第一个适配器：把 nacho.platforms.onebot.OneBotServer 包进来（**可选**：只有它
                    import ``nacho.platforms.onebot``，且只有 ``pip install "nacho[onebot]"`` 才装得上；
                    这里刻意不 re-export —— import 本包不应连带要求 websockets）
* ``kook.py``       第二个适配器：把 nacho.platforms.kook.KookClient 包进来（**可选**：只有它
                    import ``nacho.platforms.kook``）。与 OneBot 方向相反 —— OneBot 是反向 WS
                    （框架当服务端），Kook 是正向 WS（框架当客户端、Bot Token 鉴权）——
                    用来验证 ``BotAdapter`` 协议对「方向相反」的平台同样通用。
* ``manager.py``   机器人管理服务 BotManager：把「凭证落库 + 适配器生命周期」统一成接口层
                    认的 BotsService（跨平台增 / 启停 / 删，platform 差异封在实现里）

依赖方向：本包只依赖 ``nacho.core``（logger），**不** import ``nacho.platforms.onebot`` /
``nacho.api`` / ``nacho.workflow`` —— 平台的类型只在适配器实现里出现，协议这边一律
``object`` / 结构化形状（PEP 563 惰性注解兜底）。

------------------------------------------------------------------------ 怎么写第二个适配器
照 :mod:`nacho.platforms.bridge.onebot` 的样子（Kook 已经照此写了一个，四步）：

1. **平台接入**（如果还没有独立包）：像 ``nacho/platforms/onebot/`` /
   ``nacho/platforms/kook/`` 那样只管「连接 + 协议」—— 鉴权、连接管理、事件分发
   （handler 钩子）、动作发送；**业务不进去**。
   **方向先想清**：OneBot 是反向 WS（框架当服务端），Kook 是正向 WS（框架当客户端）——
   底层「监听」还是「连出去」完全不同，但上层适配器长得一样，这正是本层要的抽象；
2. **适配器** ``nacho/platforms/bridge/<平台>.py``：包住平台服务端 / 客户端，实现
   :class:`~nacho.platforms.bridge.protocols.BotAdapter` 的能力（``platform`` / ``start`` /
   ``stop`` / ``clients`` / ``send``）；平台事件在 handler 里翻译成
   :class:`~nacho.platforms.bridge.models.PlatformEvent`（身份转字符串、会话指向归一、
   翻译不了的挂 ``raw``）后调 ``publish`` 投给 Gateway。这个模块是 bridge 里**唯一**
   允许 import 平台包的地方；
3. **装配** ``bootstrap.py``：凭证统一是「凭证行」（``nacho.bots`` 的 ``bot_credentials``
   表，``platform`` + ``owner_id`` + ``bot_id`` 复合键，一个用户可多个机器人）—— OneBot
   行是随机令牌（反向 WS 握手按 token 认归属）、Kook 行是用户自填的 Bot Token（可逆加密
   落库、连接时解密）；装配读行建适配器（``publish=gateway.publish``）->
   ``gateway.register``，管理面经 ``manager.py`` 的 ``BotManager`` 统一成接口层认的
   「增 / 启停 / 删」；
4. **下游不动**：接口层与工作流认的是 Gateway / 适配器的形状，只要兼容面还在，
   新平台上线**不碰** ``router.py`` / 节点 / 前端 —— 这是本层存在的全部意义。

装配形态（bootstrap 现在的样子，两个平台并存；凭证统一是 ``bot_credentials`` 凭证行）::

    gateway = Gateway()
    gateway.register(OneBotAdapter(options, publish=gateway.publish, tokens=store))  # store: SqlBotStore（凭证行）
    kook = KookAdapter(kook_options, publish=gateway.publish)
    for cred in await store.list_platform("kook", enabled_only=True):   # 读 Kook 凭证行
        kook.add_bot(cred.bot_id, await store.decrypt_token(cred.bot_id, secret_key), owner_id=cred.owner_id)
    gateway.register(kook)
    manager = BotManager(store, onebot=onebot, kook=kook, secret_key=secret_key)  # /api/bots/* 的 BotsService
    gateway.subscribe(on_platform_event)   # 业务只认规范化事件，不认平台
    await gateway.start()                  # -> 各 adapter.start()

发送走总线（按平台路由，环境问题当场抛）::

    await gateway.send("onebot", owner_id, "send_private_msg", user_id=..., message=...)
    await gateway.send("kook", owner_id, "send_channel_msg", target_id=..., content=...)
"""
from __future__ import annotations

from .gateway import EventSubscriber, Gateway
from .logging import BRIDGE_LOGGER_NAME, bridge_logger
from .models import ActionResult, BotClient, EventTarget, PlatformEvent
from .protocols import BotAdapter

__all__ = [
    # 日志接入点
    "BRIDGE_LOGGER_NAME",
    # 规范化模型
    "ActionResult",
    # 协议
    "BotAdapter",
    "BotClient",
    # 总线
    "EventSubscriber",
    "EventTarget",
    "Gateway",
    "PlatformEvent",
    "bridge_logger",
]
