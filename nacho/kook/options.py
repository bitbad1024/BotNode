"""Kook 正向 WebSocket 的选项（对应配置文件里的 ``[kook]``）。

和 OneBot 一个路子：**kook 模块不读配置文件**，配置系统把 ``[kook]`` 校验完，由入口
（装配层）用 :meth:`KookOptions.from_mapping` 转成这里的一份不可变选项传进来 —— 两边靠
普通映射解耦，kook 模块不知道 TOML 长什么样。

与 OneBot 的关键差异：OneBot 是**反向 WS**（框架当服务端，配监听地址等实现连进来），Kook
是**正向 WS**（框架当客户端，主动连 Kook 网关）。所以这里的字段是「要连哪个网关 / 用什么
凭证」而不是「监听哪个端口」。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from typing import cast

#: 默认网关地址（Kook 开发者中心的 WebSocket 网关）
DEFAULT_GATEWAY: str = "wss://www.kookapp.cn/gateway"
#: 默认 Bot Token（用户从 Kook 开放平台签发；空串 = 没配，连接会失败）
DEFAULT_TOKEN: str = ""
#: 默认心跳间隔（秒）：Kook 网关要求客户端定期发心跳，超时会被断开
DEFAULT_HEARTBEAT_INTERVAL: float = 30.0
#: 默认动作超时（秒）：发出去的动作等这么久还没回应就算失败
DEFAULT_ACTION_TIMEOUT: float = 30.0
#: 默认断线重连间隔（秒）：连接断开后等这么久再重连
DEFAULT_RECONNECT_INTERVAL: float = 3.0


def _pick(data: Mapping[str, object], allowed: Iterable[str]) -> dict[str, object]:
    """挑出 ``data`` 里 ``allowed`` 认的字段；多出来的键忽略。

    校验归配置系统管：配置里多写了 kook 不认的项，不该让它启动失败。
    """
    keys: set[str] = set(allowed)
    return {key: value for key, value in data.items() if key in keys}


@dataclass(frozen=True)
class KookOptions:
    """Kook 正向 WS 选项（对应 ``[kook]`` 一节）。"""

    #: 网关地址（框架当客户端，主动连过去）
    gateway: str = DEFAULT_GATEWAY
    #: Bot Token（Kook 开放平台签发，连接时鉴权用；空串 = 没配）
    token: str = DEFAULT_TOKEN
    #: 心跳间隔（秒）
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL
    #: 单个动作等回应的超时（秒）
    action_timeout: float = DEFAULT_ACTION_TIMEOUT
    #: 断线后重连的间隔（秒）
    reconnect_interval: float = DEFAULT_RECONNECT_INTERVAL

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> KookOptions:
        """从一份映射建选项：缺的项用默认值，多出来的键忽略。"""
        allowed: set[str] = {field.name for field in fields(class_or_instance=cls)}
        picked: dict[str, object] = _pick(data, allowed)
        return cls(
            gateway=cast(str, picked.get("gateway", DEFAULT_GATEWAY)),
            token=cast(str, picked.get("token", DEFAULT_TOKEN)),
            heartbeat_interval=cast(
                float, picked.get("heartbeat_interval", DEFAULT_HEARTBEAT_INTERVAL)
            ),
            action_timeout=cast(float, picked.get("action_timeout", DEFAULT_ACTION_TIMEOUT)),
            reconnect_interval=cast(
                float, picked.get("reconnect_interval", DEFAULT_RECONNECT_INTERVAL)
            ),
        )
