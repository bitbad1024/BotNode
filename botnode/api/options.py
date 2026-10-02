"""接口层的选项（对应配置文件里的 ``[api]``）。

和缓存层一个路子：**接口层不读配置文件**，配置系统把 ``[api]`` 那一节校验完，由入口
（``app.py`` / 示例）用 :meth:`ApiOptions.from_mapping` 转成这里的一份不可变选项传进来
—— 两边靠普通映射解耦，接口层不知道 TOML 长什么样。

``Settings.api.model_dump()`` 可以直接喂给 :meth:`from_mapping`。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import cast

#: 默认路由前缀：登录接口最终挂在 ``/api/auth/login``
DEFAULT_PREFIX: str = "/api"
#: 访问令牌默认有效期（秒）：2 小时。**每次带令牌的请求都会把有效期往后延**，
#: 所以它实际是"闲置多久算掉线"，不是"登录后最多能用多久"。
DEFAULT_TOKEN_TTL: float = 7200.0
#: 长期令牌（「记住设备」才有）默认有效期（秒）：30 天。
DEFAULT_REMEMBER_TTL: float = 2_592_000.0
#: 头像默认存放目录（相对当前工作目录；配置文件里可改成别的，见 ``[api] avatar_dir``）。
#: 只影响默认的本地目录存储（:class:`~botnode.api.services.profile.FileAvatarStore`）。
DEFAULT_AVATAR_DIR: str = "data/avatars"
#: 头像默认大小上限（字节）：2 MiB。常见的 512×512 头像（带照片的 PNG）也就几百 KB，
#: 上限存在的意义是别让人把整个相册塞进来；``[api] avatar_max_bytes`` 可改。
DEFAULT_AVATAR_MAX_BYTES: int = 2 * 1024 * 1024


def _pick(data: Mapping[str, object], allowed: Iterable[str]) -> dict[str, object]:
    """挑出 ``data`` 里 ``allowed`` 认的字段；多出来的键忽略。

    校验归配置系统管：配置里多写了接口层不认的项，不该让它启动失败。
    """
    keys: set[str] = set(allowed)
    return {key: value for key, value in data.items() if key in keys}


@dataclass(frozen=True)
class ApiOptions:
    """接口层的选项（对应 ``[api]`` 一节）。"""

    #: 路由前缀，登录接口挂在 ``<prefix>/auth/login``
    prefix: str = DEFAULT_PREFIX
    #: 访问令牌有效期（秒），**带令牌的请求会滑动续期**；``<= 0`` 表示永不过期
    token_ttl: float = DEFAULT_TOKEN_TTL
    #: 长期令牌有效期（秒），勾「记住设备」才发；``<= 0`` 表示永不过期
    remember_ttl: float = DEFAULT_REMEMBER_TTL
    #: 是否逐条记访问日志（方法 / 路径 / 状态码 / 耗时）
    access_log: bool = True
    #: 是否信任代理头 ``X-Forwarded-For`` 里的客户端 ip。
    #: 默认 ``False``：直接把 XFF 当真实 ip 是能被伪造的，只有确实挂在可信代理后面才打开。
    trust_proxy: bool = False
    #: 头像存放目录（默认实现往这儿落文件；换存储实现时它就没用了 —— 由那个实现自己交代）
    avatar_dir: Path = Path(DEFAULT_AVATAR_DIR)
    #: 头像字节上限（超了回 413）；``<= 0`` 表示不限（不建议，见 ``[api] avatar_max_bytes``）
    avatar_max_bytes: int = DEFAULT_AVATAR_MAX_BYTES

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
            remember_ttl=cast(float, picked.get("remember_ttl", DEFAULT_REMEMBER_TTL)),
            access_log=cast(bool, picked.get("access_log", True)),
            trust_proxy=cast(bool, picked.get("trust_proxy", False)),
            # 配置系统给的是路径（``Path``）或字符串，两种都收
            avatar_dir=Path(cast("str | Path", picked.get("avatar_dir", DEFAULT_AVATAR_DIR))),
            avatar_max_bytes=cast(
                int, picked.get("avatar_max_bytes", DEFAULT_AVATAR_MAX_BYTES)
            ),
        )
