"""接口层的选项（对应配置文件里的 ``[api]``）。

和缓存层一个路子：**接口层不读配置文件**，配置系统把 ``[api]`` 那一节校验完，由入口
（``app.py`` / 示例）用 :meth:`ApiOptions.from_mapping` 转成这里的一份不可变选项传进来
—— 两边靠普通映射解耦，接口层不知道 TOML 长什么样。

``Settings.api.model_dump()`` 可以直接喂给 :meth:`from_mapping`。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from typing import cast

#: 默认路由前缀：登录接口最终挂在 ``/api/auth/login``
DEFAULT_PREFIX: str = "/api"
#: 默认令牌有效期（秒）
DEFAULT_TOKEN_TTL: float = 3600.0


def _pick(data: Mapping[str, object], allowed: Iterable[str]) -> dict[str, object]:
    """挑出 ``data`` 里 ``allowed`` 认的字段；多出来的键忽略。

    校验归配置系统管：配置里多写了接口层不认的项，不该让接口层启动失败。
    """
    keys: set[str] = set(allowed)
    return {key: value for key, value in data.items() if key in keys}


@dataclass(frozen=True)
class ApiOptions:
    """接口层的选项（对应 ``[api]`` 一节）。"""

    #: 路由前缀，登录接口挂在 ``<prefix>/auth/login``
    prefix: str = DEFAULT_PREFIX
    #: 登录令牌有效期（秒）
    token_ttl: float = DEFAULT_TOKEN_TTL
    #: 是否逐条记访问日志（方法 / 路径 / 状态码 / 耗时）
    access_log: bool = True
    #: 令牌签名密钥；空串表示启动时现生成一个随机的（重启后已签发的令牌失效）
    secret: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> ApiOptions:
        """从一份映射建选项：缺的项用默认值，多出来的键忽略。"""
        allowed: set[str] = {field.name for field in fields(class_or_instance=cls)}
        picked: dict[str, object] = _pick(data, allowed)
        # 字段是运行时按名挑出来的，这里逐个显式收窄到构造器对应的具体类型；
        # 不写 ``cast(dict[str, Any])``，否则实参会带 ``Any`` 触发类型检查告警。
        # cast 运行时是恒等，行为与原来一致。
        return cls(
            prefix=cast(str, picked.get("prefix", DEFAULT_PREFIX)),
            token_ttl=cast(float, picked.get("token_ttl", DEFAULT_TOKEN_TTL)),
            access_log=cast(bool, picked.get("access_log", True)),
            secret=cast(str, picked.get("secret", "")),
        )
