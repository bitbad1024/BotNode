"""Kook 正向 WS 接入层：框架当客户端，主动连 Kook 网关（用 Bot Token 鉴权）。

这一层只做「连接 + 协议」，不做业务；业务用 ``handler`` 钩子接 :meth:`KookClient`。

目录按职责划分::

    options.py   KookOptions：网关地址 / Bot Token / 心跳间隔 / 动作超时（对应配置 [kook]）
    models.py    协议形状：KookEvent（事件）+ KookActionResponse（动作回应）
    client.py    KookClient：正向 WS 连接、心跳保活、事件分发、REST 动作发送、断线重连
    logging.py   日志接入点：``kook`` 这个名字

与 OneBot 的方向相反：OneBot 是**反向 WS**（框架当服务端，等实现连进来），Kook 是
**正向 WS**（框架当客户端，连出去）。发消息走 REST API 而不是 WS。

用起来::

    from nacho.platforms.kook import KookOptions, KookClient

    async def on_event(event):
        print(event.type, event.target_id, event.content)

    client = KookClient(KookOptions(token="xxx"), handler=on_event)
    await client.start()
    await client.serve_forever()
"""
from __future__ import annotations

from .client import EventHandler, KookClient
from .logging import KOOK_LOGGER_NAME, kook_logger
from .models import (
    EVENT_CHANNEL,
    EVENT_IMAGE,
    EVENT_SYSTEM,
    EVENT_TEXT,
    KookActionResponse,
    KookEvent,
    parse_action_response,
    parse_event,
)
from .options import (
    DEFAULT_ACTION_TIMEOUT,
    DEFAULT_GATEWAY,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_HEARTBEAT_JITTER,
    DEFAULT_RECONNECT_INTERVAL,
    DEFAULT_TOKEN,
    KookOptions,
)

__all__ = [
    # 装配
    "KookClient",
    "KookOptions",
    "EventHandler",
    # 选项默认值
    "DEFAULT_GATEWAY",
    "DEFAULT_TOKEN",
    "DEFAULT_HEARTBEAT_INTERVAL",
    "DEFAULT_HEARTBEAT_JITTER",
    "DEFAULT_ACTION_TIMEOUT",
    "DEFAULT_RECONNECT_INTERVAL",
    # 协议模型
    "KookEvent",
    "KookActionResponse",
    "parse_event",
    "parse_action_response",
    "EVENT_TEXT",
    "EVENT_IMAGE",
    "EVENT_CHANNEL",
    "EVENT_SYSTEM",
    # 日志接入点
    "KOOK_LOGGER_NAME",
    "kook_logger",
]
