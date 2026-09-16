"""缓存后端协议：内存与 Redis 两个实现都符合它，上层只认这套方法。

约定（两个后端必须一致 —— 差异在这里被抹平，上层换后端不用改代码）：

* **键**是普通字符串，**不带前缀**。前缀是 Redis 后端自己的事（共享实例时的命名空间
  隔离，见 :class:`~nacho.core.cache.redis.RedisCache`），上层看到的键名与后端无关；
* **值**有三种结构，对应 Redis 的三种类型：字符串（:meth:`CacheBackend.get` /
  :meth:`CacheBackend.set`）、列表（``list_*``）、哈希（``hash_*``）。元素与字段值都是
  ``str`` —— Redis 后端开 ``decode_responses`` 直接拿回字符串，上层不必解码字节；
  元素 / 字段值收到别的类型一律抛 :class:`~nacho.core.cache.models.CacheError`，
  不会被悄悄塞进去（Redis 那边由驱动报 ``DataError``，翻过来是同一个异常）。
  要存嵌套结构（哈希的哈希、对象、数组）就用
  :meth:`~nacho.core.cache.core.Cache.set_json` 装成一段 JSON 文本：Redis 的哈希字段
  同样只放字符串，嵌套得自己序列化，两边在这种事上帮不上忙；
* **结构不能混用**：一个键按哪种结构写入，就只能按那种结构访问，换一种访问会抛
  :class:`~nacho.core.cache.models.CacheError`（对应 Redis 的 ``WRONGTYPE``）——
  唯一的例外是 :meth:`CacheBackend.get_many`，它遇到非字符串键当作没取到（照 ``MGET`` 来）；
* **空结构不占键**：列表被弹空、哈希字段被删空之后，这个键就不存在了（与 Redis 一致）；
* **TTL** 是秒（``float``）：``set(..., ttl=None)`` 表示永不过期，
  :meth:`CacheBackend.ttl` 用 ``None`` 表示键不存在、``math.inf`` 表示永不过期；
  ``list_*`` / ``hash_*`` 上的 ``ttl`` **只在新建键时生效**，往已有的键上追加不动它的
  过期时间（照 Redis 来：``RPUSH`` / ``HSET`` 不刷新 TTL）；
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

    # ---- 列表（Redis 的 list）----
    async def list_push(self, key: str, *values: str, ttl: float | None = None) -> int:
        """从右侧推入元素（``list_push(key, "a", "b")``），返回推入后的长度。

        ``ttl`` 只在键不存在时用；空推入当「问长度」，不建键。
        """
        ...

    async def list_push_left(self, key: str, *values: str, ttl: float | None = None) -> int:
        """从左侧推入元素（队列的另一端），其余同 :meth:`list_push`。"""
        ...

    async def list_range(self, key: str, start: int = 0, stop: int = -1) -> list[str]:
        """取下标区间内的元素，**两端都含**；负数从右往左数，越界自动裁剪。

        键不存在返回空列表；``list_range(key)`` 就是整个列表。
        """
        ...

    async def list_length(self, key: str) -> int:
        """列表长度（键不存在算 0）。"""
        ...

    async def list_pop(self, key: str, count: int = 1) -> list[str]:
        """从右侧弹出至多 ``count`` 个元素，按**弹出顺序**返回（最右侧的先出来）。

        没得弹就返回空列表；弹空之后键就没了。
        """
        ...

    # ---- 哈希（Redis 的 hash）----
    async def hash_set(self, key: str, items: Mapping[str, str], ttl: float | None = None) -> int:
        """写字段（``HSET`` 语义，一次可写多个），返回**新增**的字段数。

        ``ttl`` 只在键不存在时用；已存在的键上追加字段不动它的 TTL。
        """
        ...

    async def hash_get(self, key: str, field: str) -> str | None:
        """取一个字段；键或字段不存在返回 ``None``。"""
        ...

    async def hash_get_all(self, key: str) -> dict[str, str]:
        """取回整个哈希（键不存在返回空字典）；不保证字段顺序。"""
        ...

    async def hash_delete(self, key: str, *fields: str) -> int:
        """删若干个字段（``hash_delete(key, "a", "b")``），返回真删掉的个数；被删空的键就没了。"""
        ...

    async def keys(self, pattern: str = "*") -> list[str]:
        """按通配符列键（``*`` / ``?`` / ``[abc]``），默认全部；不保证顺序。"""
        ...

    async def clear(self) -> int:
        """清掉本缓存的所有键（Redis 后端只清自己命名空间下的），返回清掉的键数。"""
        ...
