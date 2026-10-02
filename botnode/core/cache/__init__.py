"""缓存层：一套 API，两种后端（Redis / 本地内存），上层不用管用的是哪个。

用法一瞥::

    from botnode.core.cache import CacheOptions, cache

    await cache.start()                     # 默认就是本地内存版：不启用 Redis 也能用
    await cache.set("k", "v", ttl=60)
    await cache.get("k")
    await cache.set_json("profile", {"tags": ["a"]})    # 嵌套结构走 JSON

    cache.configure(CacheOptions(backend="redis", namespace="botnode"))   # 要用 Redis
    await cache.start()

业务代码通常只碰 :data:`~botnode.core.cache.manager.cache`（进程级单例）；要一份独立的
缓存就自己 ``Cache()``。行为约定（后端差异、结构化数据、命名空间、连不上不静默、
配置注入）见 ``docs/cache/cache.md``。
"""
from __future__ import annotations

from botnode.core.cache.core import Cache
from botnode.core.cache.interfaces import CacheBackend
from botnode.core.cache.logging import CACHE_LOGGER_NAME, cache_logger
from botnode.core.cache.manager import cache
from botnode.core.cache.memory import MemoryCache
from botnode.core.cache.models import CacheError, CacheOptions, RedisOptions
from botnode.core.cache.redis import RedisCache

__all__ = [
    "CACHE_LOGGER_NAME",
    "Cache",
    "CacheBackend",
    "CacheError",
    "CacheOptions",
    "MemoryCache",
    "RedisCache",
    "RedisOptions",
    "cache",
    "cache_logger",
]
