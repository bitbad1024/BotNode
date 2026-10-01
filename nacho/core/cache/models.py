"""缓存层的选项与错误。

选项是两份不可变 dataclass：:class:`CacheOptions`（用哪个后端、命名空间、默认 TTL）
与 :class:`RedisOptions`（连一份 Redis 要的地址与账号）。配置系统产出的是一份普通映射
（``config.py`` 的 ``[cache]`` 区域），由 :meth:`CacheOptions.from_mapping` 转进来 ——
缓存层不 import 配置模块，两边靠这个映射解耦（和日志核心、调度器一样，配置由
``app.py`` 传下来）。
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any, Literal, TypeAlias

#: 支持的后端名：``"redis"`` 走 Redis 服务，``"memory"`` 走进程内存
BackendName: TypeAlias = Literal["memory", "redis"]


class CacheError(RuntimeError):
    """缓存层的错误：后端连不上、命令执行失败、还没启动就调用。"""


def _pick(cls: type[Any], data: Mapping[str, object]) -> dict[str, Any]:
    """挑出 ``data`` 里属于这个 dataclass 的字段。

    多出来的键直接忽略：缓存层只认自己这两块选项，配置里另写了别的项不该让它炸掉
    （校验那一项归配置系统管，报错也要报在配置那一层）。
    """
    known = {field.name for field in fields(cls)}
    return {key: value for key, value in data.items() if key in known}


@dataclass(frozen=True)
class RedisOptions:
    """连一份 Redis 需要的参数（对应配置里的 ``[cache.redis]``）。"""

    host: str = "127.0.0.1"
    port: int = 6379
    db: int = 0
    username: str = ""  # Redis 6+ 的 ACL 用户名；空 = 默认用户
    password: str = ""  # 空 = 不认证
    socket_timeout: float = 5.0  # 单条命令的超时（秒），卡住时别拖着上层
    #: TCP 建连超时（秒）：不设的话走系统默认（Windows 上实测要干等二十几秒才报错），
    #: 显式给个短超时，Redis 起不来时能尽快报出来
    socket_connect_timeout: float = 5.0
    #: 建连失败后的重试次数：驱动默认 10 次指数退避重试（连不上时要等二十几秒），
    #: 这里默认只补一次，宁可失败也不要干等
    connect_retries: int = 1
    max_connections: int = 10  # 连接池上限

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> RedisOptions:
        """从一份映射建选项：缺的项用默认值，多出来的键忽略。"""
        return cls(**_pick(cls, data))


@dataclass(frozen=True)
class CacheOptions:
    """缓存层的选项（对应配置里的 ``[cache]``）。"""

    backend: BackendName = "memory"  # 默认本地内存：不启用 Redis 也能直接用
    namespace: str = "nacho"  # Redis 上的键前缀，共用一个实例时用来隔离
    default_ttl: float = 0.0  # set() 没给 ttl 时用；0 = 永不过期
    # Redis 连不上时默认**当场报错**（配了 Redis 却起不来，多半是配置/服务有问题，
    # 静默退回本地缓存会掩盖掉）；只有显式打开才退回内存
    fallback_to_memory: bool = False
    sweep_interval: float = 30.0  # 内存后端扫过期键的间隔（秒）
    #: 只有 ``backend="redis"`` 时才用得上（不可变 dataclass 不能直接当默认值，用工厂）
    redis: RedisOptions = field(default_factory=RedisOptions)

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> CacheOptions:
        """从一份映射建选项：``redis`` 是嵌套映射，其余按字段名取，多出来的键忽略。

        ``Settings.cache.model_dump()`` 就能直接喂进来。
        """
        values = _pick(cls, data)
        redis_data = data.get("redis")
        if isinstance(redis_data, Mapping):
            values["redis"] = RedisOptions.from_mapping(redis_data)
        return cls(**values)
