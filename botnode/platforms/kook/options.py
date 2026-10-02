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

#: 网关地址默认留空：Kook 的网关是**动态分发**的，连接前要走 gateway/index 拿真实地址
#: （返回的 url 已带 token / compress 参数）。留空 = 自动获取；显式填一个则直连（测试 / 自建网关）。
DEFAULT_GATEWAY: str = ""
#: 默认 Bot Token（用户从 Kook 开放平台签发；空串 = 没配，连接会失败）
DEFAULT_TOKEN: str = ""
#: 默认心跳间隔（秒）：Kook 网关要求客户端定期发心跳，超时会被断开
DEFAULT_HEARTBEAT_INTERVAL: float = 30.0
#: 默认心跳抖动（秒）：官方口径是 30 秒 + rand(-5, +5)，别让所有客户端同一时刻打心跳
DEFAULT_HEARTBEAT_JITTER: float = 5.0
#: 默认动作超时（秒）：发出去的动作等这么久还没回应就算失败
DEFAULT_ACTION_TIMEOUT: float = 30.0
#: 默认断线重连基准（秒）：官方连接流程的退避就是它 ×2^k —— 2、4（连接）-> 8、16（resume）
DEFAULT_RECONNECT_INTERVAL: float = 2.0
#: 默认重连退避上限（秒）：官方——回到「获取 Gateway」那一步后指数退避，最大间隔 60
DEFAULT_RECONNECT_MAX_INTERVAL: float = 60.0
#: 默认两次 REST 请求的最小间隔（秒）：限流，别一上来就撞 429
DEFAULT_REST_MIN_INTERVAL: float = 0.2
#: 默认 REST 瞬时失败重试次数（429 / 5xx / 网络抖动）
DEFAULT_REST_MAX_RETRIES: int = 3
#: 默认 REST 连接空闲上限（秒）：空闲超过它就主动重建（服务端会按空闲时间掐连接）
DEFAULT_REST_IDLE_TIMEOUT: float = 30.0


def _pick(data: Mapping[str, object], allowed: Iterable[str]) -> dict[str, object]:
    """挑出 ``data`` 里 ``allowed`` 认的字段；多出来的键忽略。

    校验归配置系统管：配置里多写了 kook 不认的项，不该让它启动失败。
    """
    keys: set[str] = set(allowed)
    return {key: value for key, value in data.items() if key in keys}


@dataclass(frozen=True)
class KookOptions:
    """Kook 正向 WS 选项（对应 ``[kook]`` 一节）。"""

    #: 网关地址：留空（默认）连接前走 gateway/index 动态获取真实地址（推荐）；
    #: 显式填一个则直连这个地址（测试 / 自建网关用）。
    gateway: str = DEFAULT_GATEWAY
    #: Bot Token（Kook 开放平台签发，连接时鉴权用；空串 = 没配）
    token: str = DEFAULT_TOKEN
    #: Bot Token 落库加密的密钥（kook 凭证行加解密用；[kook].token 直连时用不到）
    secret_key: str = ""
    #: 心跳间隔（秒）
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL
    #: 心跳抖动（秒）：实际间隔在 ``heartbeat_interval ± heartbeat_jitter`` 里随机
    heartbeat_jitter: float = DEFAULT_HEARTBEAT_JITTER
    #: 单个动作等回应的超时（秒）
    action_timeout: float = DEFAULT_ACTION_TIMEOUT
    #: 断线后重连的间隔（秒）
    reconnect_interval: float = DEFAULT_RECONNECT_INTERVAL
    #: 重连退避上限（秒）：指数退避封顶，避免无限拉长
    reconnect_max_interval: float = DEFAULT_RECONNECT_MAX_INTERVAL
    #: 两次 REST 请求的最小间隔（秒）：限流，别一上来就撞 429
    rest_min_interval: float = DEFAULT_REST_MIN_INTERVAL
    #: REST 瞬时失败重试次数（429 / 5xx / 网络抖动）
    rest_max_retries: int = DEFAULT_REST_MAX_RETRIES
    #: REST 持久连接的空闲上限（秒）：超了主动重建，别等请求时才撞上已关的连接
    rest_idle_timeout: float = DEFAULT_REST_IDLE_TIMEOUT

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> KookOptions:
        """从一份映射建选项：缺的项用默认值，多出来的键忽略。"""
        allowed: set[str] = {field.name for field in fields(class_or_instance=cls)}
        picked: dict[str, object] = _pick(data, allowed)
        return cls(
            gateway=cast(str, picked.get("gateway", DEFAULT_GATEWAY)),
            token=cast(str, picked.get("token", DEFAULT_TOKEN)),
            secret_key=cast(str, picked.get("secret_key", "")),
            heartbeat_interval=cast(
                float, picked.get("heartbeat_interval", DEFAULT_HEARTBEAT_INTERVAL)
            ),
            heartbeat_jitter=cast(
                float, picked.get("heartbeat_jitter", DEFAULT_HEARTBEAT_JITTER)
            ),
            action_timeout=cast(float, picked.get("action_timeout", DEFAULT_ACTION_TIMEOUT)),
            reconnect_interval=cast(
                float, picked.get("reconnect_interval", DEFAULT_RECONNECT_INTERVAL)
            ),
            reconnect_max_interval=cast(
                float, picked.get("reconnect_max_interval", DEFAULT_RECONNECT_MAX_INTERVAL)
            ),
            rest_min_interval=cast(
                float, picked.get("rest_min_interval", DEFAULT_REST_MIN_INTERVAL)
            ),
            rest_max_retries=cast(
                int, picked.get("rest_max_retries", DEFAULT_REST_MAX_RETRIES)
            ),
            rest_idle_timeout=cast(
                float, picked.get("rest_idle_timeout", DEFAULT_REST_IDLE_TIMEOUT)
            ),
        )
