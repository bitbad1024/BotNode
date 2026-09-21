"""OneBot 反向 WS 接入层：框架当服务端，等 OneBot 实现（go-cqhttp / NapCat / LLOneBot …）连进来。

这一层只做「连接 + 协议」，不做业务；业务用 ``handler`` 钩子接 :meth:`OneBotServer`。

目录按职责划分::

    options.py   OneBotOptions：监听地址 / 路径 / 动作超时（对应配置 [onebot]）
    models.py    协议形状：上报事件 OneBotEvent（message / notice / request / meta_event）
                 与动作回应 ActionResponse
    server.py    OneBotServer / OneBotConnection：握手鉴权、连接管理、事件分发、动作发送
    tokens.py    令牌注册表：令牌 -> 账号（一个端口接多客户端，靠它认归属）
    logging.py   日志接入点：``onebot`` 这个名字（落 logs/onebot.log）

**一个端口接很多客户端**：谁连进来由令牌决定归属（:class:`~nacho.api.api.onebot.protocols.TokenRegistry`）—— 握手时把令牌
翻成账号、绑到连接上，在线的客户端能像路由器那样列出来（``server.roster()``）、也能踢掉
（``server.kick(...)``）。不传注册表就是不校验，不接数据库照样能跑。

用起来::

    from pathlib import Path

    from nacho.onebot import OneBotOptions, OneBotServer, attach_onebot_logging

    attach_onebot_logging(Path("logs/onebot.log"))         # 先挂日志，再建服务

    async def on_event(conn, event):
        await conn.call("send_msg", message_type="private", user_id=event.user_id, message="hi")

    server = OneBotServer(OneBotOptions(host="0.0.0.0", port=6700), handler=on_event)
    await server.start()
    await server.serve_forever()

OneBot 实现那边把「反向 WS（Reverse WebSocket）」地址配成 ``ws://<host>:<port><path>``；
配了令牌注册表的话，令牌按 ``Authorization: Bearer <token>`` 或 ``?access_token=<token>`` 带上。
"""
from __future__ import annotations

from .logging import ONEBOT_LOGGER_NAME, attach_onebot_logging, onebot_logger
from .models import (
    ActionResponse,
    MessageEvent,
    MetaEvent,
    NoticeEvent,
    OneBotEvent,
    RequestEvent,
    is_event,
    parse_action_response,
    parse_event,
)
from .options import (
    DEFAULT_ACTION_TIMEOUT,
    DEFAULT_HOST,
    DEFAULT_PATH,
    DEFAULT_PORT,
    OneBotOptions,
)
from .server import ClientEntry, EventHandler, OneBotConnection, OneBotServer
from .tokens import (
    InMemoryTokenRegistry,
    IssuedToken,
    SqlTokenRegistry,
    TokenRecord,
    generate_token,
    hash_token,
)

__all__ = [
    # 装配
    "OneBotServer",
    "OneBotConnection",
    "OneBotOptions",
    "EventHandler",
    # 在线列表
    "ClientEntry",
    # 令牌注册表
    "TokenRecord",
    "IssuedToken",
    "SqlTokenRegistry",
    "InMemoryTokenRegistry",
    "generate_token",
    "hash_token",
    # 选项默认值
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_PATH",
    "DEFAULT_ACTION_TIMEOUT",
    # 协议模型
    "OneBotEvent",
    "MessageEvent",
    "NoticeEvent",
    "RequestEvent",
    "MetaEvent",
    "ActionResponse",
    "is_event",
    "parse_event",
    "parse_action_response",
    # 日志接入点
    "ONEBOT_LOGGER_NAME",
    "attach_onebot_logging",
    "onebot_logger",
]
