"""缓存后端协议：内存与 Redis 两个实现都符合它，上层只认这套方法。

约定（两个后端必须一致 —— 差异在这里被抹平，上层换后端不用改代码）：

* **键**是普通字符串，**不带前缀**。前缀是 Redis 后端自己的事（共享实例时的命名空间
  隔离，见 :class:`~nacho.core.cache.redis.RedisCache`），上层看到的键名与后端无关；
* **值**是 ``str``。Redis 后端开 ``decode_responses`` 直接拿回字符串，上层不必解码字节；
* **TTL** 是秒（``float``）：``set(..., ttl=None)`` 表示永不过期，
  :meth:`CacheBackend.ttl` 用 ``None`` 表示键不存在、``math.inf`` 表示永不过期；
* **批量**方法要么全做要么不做（Redis 后端走 pipeline / 单条多键命令），不返回半截结果；
* **生命周期**：:meth:`CacheBackend.start` / :meth:`CacheBackend.stop`，重复调用是空操作；
  出错一律抛 :class:`~nacho.core.cache.models.CacheError`（驱动层异常不外泄）。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class CacheBackend(Protocol):
    """缓存后端协议（由内存层、Redis 层实现）。"""

    async def start(self) -> None:
        """建连接 / 起后台任务；重复调用是空操作。"""
        ...

    async def stop(self, timeout: float = 5.0) -> None:
        """释放资源；重复调用是空操作。"""
        ...

    async def ping(self) -> bool:
        """后端是否可用（给健康检查用），不抛异常。"""
        ...

    async def get(self, key: str) -> str | None:
        """取值；键不存在或已过期返回 ``None``。"""
        ...

    async def set(self, key: str, value: str, ttl: float | None = None) -> None:
        """写值；``ttl`` 为 ``None`` 表示永不过期（覆盖同名键的 TTL 与旧值）。"""
        ...

    async def delete(self, key: str) -> bool:
        """删一个键，返回是否删掉了。"""
        ...

    async def exists(self, key: str) -> bool:
        """键是否存在（已过期的算不存在）。"""
        ...

    async def expire(self, key: str, ttl: float) -> bool:
        """给已有的键设过期时间，返回是否设上了（键不存在则 ``False``）。"""
        ...

    async def ttl(self, key: str) -> float | None:
        """剩余存活秒数；``None`` = 键不存在，``math.inf`` = 永不过期。"""
        ...

    async def incr(self, key: str, amount: int = 1) -> int:
        """原子自增（键不存在时从 0 起算），返回自增后的值；值不是整数则抛 CacheError。"""
        ...

    async def get_many(self, keys: Sequence[str]) -> dict[str, str]:
        """批量取值，只返回拿到的那些键（顺序不保证，调用方按 key 取）。"""
        ...

    async def set_many(self, items: Mapping[str, str], ttl: float | None = None) -> None:
        """批量写值，同一个 ``ttl`` 作用于这一批。"""
        ...

    async def delete_many(self, keys: Sequence[str]) -> int:
        """批量删除，返回删掉的键数。"""
        ...

    async def keys(self, pattern: str = "*") -> list[str]:
        """按通配符列键（``*`` / ``?`` / ``[abc]``），默认全部；不保证顺序。"""
        ...

    async def clear(self) -> int:
        """清掉本缓存的所有键（Redis 后端只清自己命名空间下的），返回清掉的键数。"""
        ...
