"""缓存层：一套 API，两种后端（Redis / 本地内存），上层不用管用的是哪个。

用法一瞥::

    from nacho.core.cache import CacheOptions, cache

    await cache.start()                     # 默认就是本地内存版：不启用 Redis 也能用
    await cache.set("k", "v", ttl=60)
    await cache.get("k")

    await cache.list_push("queue", "a", "b")            # 列表
    await cache.hash_set("user:1", {"name": "阿一"})    # 哈希
    await cache.set_json("profile", {"tags": ["a"]})    # 嵌套结构走 JSON

    cache.configure(CacheOptions(backend="redis", namespace="nacho"))   # 要用 Redis
    await cache.start()

业务代码通常只碰 :data:`~nacho.core.cache.manager.cache`（进程级单例）；要一份独立的
缓存就自己 ``Cache()``（比如测试里）。

行为约定：

* **后端可选**：``backend = "redis"`` 走 Redis（redis-py 异步客户端，命令异步执行），
  ``"memory"``（默认）走进程内存；两者实现同一个协议
  （:class:`~nacho.core.cache.interfaces.CacheBackend`），键是字符串、值分字符串 / 列表 /
  哈希三种结构、TTL 按秒，语义一致 —— 换后端不用改上层代码；
* **结构化数据**：字符串之外还有列表（``list_push`` / ``list_range`` / ``list_pop`` 等）
  与哈希（``hash_set`` / ``hash_get_all`` / ``hash_delete`` 等）；嵌套结构（哈希的哈希、
  对象、数组）用 :meth:`~nacho.core.cache.core.Cache.set_json` 存成一段 JSON —— 一个键
  只能按写入时的那种结构访问，换一种访问会抛
  :class:`~nacho.core.cache.models.CacheError`（对应 Redis 的 ``WRONGTYPE``）；
* **连不上不静默**：配了 Redis 但连不上，默认**当场抛错**并在报错信息里提示检查
  ``config.toml`` 的 ``[cache.redis]`` 配置；只有显式打开 ``fallback_to_memory`` 才退回本地
  缓存并记一条 warning（要上报就问 :attr:`~nacho.core.cache.core.Cache.degraded`）；
* **命名空间**：Redis 上所有键都带 ``<namespace>:`` 前缀（多个应用共用一个实例时隔离），
  上层看到 / 传进来的键名始终是不带前缀的那一份；
* **不隐式启动**：没 ``start()`` 就调数据接口会抛
  :class:`~nacho.core.cache.models.CacheError`；
* **配置**：``[cache]`` 区域（见 ``config.py`` / ``config.toml.example``）里的映射，
  由 :meth:`~nacho.core.cache.models.CacheOptions.from_mapping` 转成选项传进来，
  本层不 import ``config`` —— 和日志核心、调度器一样，配置由 ``app.py`` 传下来。
"""
from __future__ import annotations

from nacho.core.cache.core import Cache
from nacho.core.cache.interfaces import CacheBackend
from nacho.core.cache.logging import CACHE_LOGGER_NAME, cache_logger
from nacho.core.cache.manager import cache
from nacho.core.cache.memory import MemoryCache
from nacho.core.cache.models import CacheError, CacheOptions, RedisOptions
from nacho.core.cache.redis import RedisCache

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
