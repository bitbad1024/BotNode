"""OneBot 反向 WS 的选项（对应配置文件里的 ``[onebot]``）。

和接口层一个路子：**onebot 模块不读配置文件**，配置系统把 ``[onebot]`` 校验完，由入口
（``onebot.py``）用 :meth:`OneBotOptions.from_mapping` 转成这里的一份不可变选项传进来
—— 两边靠普通映射解耦，onebot 模块不知道 TOML 长什么样。

``Settings.onebot.model_dump()`` 可以直接喂给 :meth:`from_mapping`。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from typing import cast

#: 默认监听地址（反向 WS：等 OneBot 实现连进来）
DEFAULT_HOST: str = "127.0.0.1"
#: 默认监听端口（go-cqhttp / NapCat 反向 WS 常用的 6700）
DEFAULT_PORT: int = 6700
#: 默认 WS 路径：客户端连 ``ws://host:port<path>``
DEFAULT_PATH: str = "/"
#: 默认动作超时（秒）：发出去的 action 等这么久还没回应就算失败
DEFAULT_ACTION_TIMEOUT: float = 30.0


def _pick(data: Mapping[str, object], allowed: Iterable[str]) -> dict[str, object]:
    """挑出 ``data`` 里 ``allowed`` 认的字段；多出来的键忽略。

    校验归配置系统管：配置里多写了 onebot 不认的项，不该让它启动失败。
    """
    keys: set[str] = set(allowed)
    return {key: value for key, value in data.items() if key in keys}


@dataclass(frozen=True)
class OneBotOptions:
    """OneBot 反向 WS 选项（对应 ``[onebot]`` 一节）。"""

    #: 监听地址 / 端口（框架当服务端，OneBot 实现连进来）
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    #: 只接受该路径的连接（OneBot 实现里的反向 WS 地址要与之对应）
    path: str = DEFAULT_PATH
    #: 单个动作等回应的超时（秒）
    action_timeout: float = DEFAULT_ACTION_TIMEOUT

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> OneBotOptions:
        """从一份映射建选项：缺的项用默认值，多出来的键忽略。"""
        allowed: set[str] = {field.name for field in fields(class_or_instance=cls)}
        picked: dict[str, object] = _pick(data, allowed)
        # 字段是运行时按名挑出来的，这里逐个显式收窄到构造器对应的具体类型；
        # 不写 ``cast(dict[str, Any])``，否则实参会带 ``Any`` 触发类型检查告警。
        return cls(
            host=cast(str, picked.get("host", DEFAULT_HOST)),
            port=cast(int, picked.get("port", DEFAULT_PORT)),
            path=cast(str, picked.get("path", DEFAULT_PATH)),
            action_timeout=cast(float, picked.get("action_timeout", DEFAULT_ACTION_TIMEOUT)),
        )
