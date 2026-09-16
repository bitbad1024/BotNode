"""缓存门面：上层只认这一套 API，Redis 与本地内存的差异在这里被抹平。

用哪个后端由 :class:`~nacho.core.cache.models.CacheOptions` 的 ``backend`` 决定：
``"redis"`` 建 :class:`~nacho.core.cache.redis.RedisCache`，``"memory"``（默认）建
:class:`~nacho.core.cache.memory.MemoryCache` —— 两者都符合
:class:`~nacho.core.cache.interfaces.CacheBackend`，所以本类的方法就是无脑转发，
唯一多做的一件事是把「``ttl=None`` 该用哪个 TTL」按配置定下来。

于是上层拿到的服务与后端无关：同一个 ``get`` / ``set`` / ``ttl`` 语义，同一套
:class:`~nacho.core.cache.models.CacheError` 异常；配了 Redis 而它连不上时还能退回内存
（``fallback_to_memory``），业务代码不必写第二个分支，也不必知道这件事。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from nacho.core.cache.interfaces import CacheBackend
from nacho.core.cache.memory import MemoryCache
from nacho.core.cache.models import CacheError, CacheOptions
from nacho.core.cache.redis import RedisCache
from nacho.core.logger import get_logger


class Cache:
    """缓存门面；进程级实例见 :data:`nacho.core.cache.manager.cache`。

    生命周期与调度器一致：:meth:`start` / :meth:`stop` 都是显式的、重复调用是空操作。
    **不隐式启动**：没 ``start()`` 就调用数据接口会抛
    :class:`~nacho.core.cache.models.CacheError` —— 连不连得上应该在启动阶段就见分晓，
    而不是等到哪一次 ``get`` 才炸。
    """

    def __init__(self, options: CacheOptions | None = None) -> None:
        self._options: CacheOptions = options or CacheOptions()
        self._backend: CacheBackend | None = None
        self._degraded: bool = False  # 配了 Redis 但退回了内存

    # ---- 生命周期 ----
    def configure(self, options: CacheOptions) -> None:
        """换一份选项（配置由 ``app.py`` 从 ``[cache]`` 区域转进来）。

        :raises CacheError: 已经启动过了 —— 配置冻结在 :meth:`start` 那一刻，
            要改就先 :meth:`stop`。
        """
        if self._backend is not None:
            raise CacheError("缓存已经启动，改配置要先 await cache.stop()")
        self._options = options

    async def start(self) -> None:
        """按选项建后端并连上；重复调用是空操作。

        ``backend="redis"`` 而 Redis 连不上时：``fallback_to_memory`` 为真就退回内存并记一条
        warning（上层无感），为假则把 :class:`CacheError` 抛出去让启动阶段直接失败。
        降级只在这一刻判断一次，之后不自动重连 —— 要重新试就 ``stop()`` 再 ``start()``。
        """
        if self._backend is not None:
            return
        options = self._options
        backend: CacheBackend
        if options.backend != "redis":
            backend = MemoryCache(sweep_interval=options.sweep_interval)
        else:
            redis_backend = RedisCache(options.redis, namespace=options.namespace)
            try:
                await redis_backend.start()
            except CacheError as exc:
                if not options.fallback_to_memory:
                    raise
                self._degraded = True
                get_logger("cache").warning("Redis 起不来，退回本地缓存", error=str(exc))
                backend = MemoryCache(sweep_interval=options.sweep_interval)
            else:
                backend = redis_backend
        await backend.start()  # redis 分支已经起过，这里是空操作
        self._backend = backend
        get_logger("cache").info(
            f"缓存就绪：{self.backend_name}", namespace=options.namespace
        )

    async def stop(self, timeout: float = 5.0) -> None:
        """停后端（内存后端停清扫任务、Redis 后端断连接池）；重复调用是空操作。

        数据不动：内存后端不跨进程、本来就没有持久化可言，Redis 那边由服务端自己管。
        """
        backend, self._backend = self._backend, None
        if backend is None:
            return
        await backend.stop(timeout)

    # ---- 状态 ----
    @property
    def running(self) -> bool:
        """是否已启动。"""
        return self._backend is not None

    @property
    def backend_name(self) -> str:
        """实际在用的后端：``"redis"`` / ``"memory"``；没启动时返回选项里配的那个。"""
        if isinstance(self._backend, RedisCache):
            return "redis"
        if isinstance(self._backend, MemoryCache):
            return "memory"
        return self._options.backend

    @property
    def degraded(self) -> bool:
        """是否发生过降级（配了 Redis 但退回了内存）—— 需要上报就报这个。"""
        return self._degraded

    @property
    def options(self) -> CacheOptions:
        """当前这份选项。"""
        return self._options

    async def ping(self) -> bool:
        """后端是否可用，不抛异常（内存后端恒为真）。"""
        return await self._require().ping()

    # ---- 数据操作（转发给当前后端）----
    async def get(self, key: str) -> str | None:
        """取值；键不存在或已过期返回 ``None``。"""
        return await self._require().get(key)

    async def set(self, key: str, value: str, ttl: float | None = None) -> None:
        """写值；``ttl`` 留空则用配置里的 ``default_ttl``（为 0 表示永不过期）。"""
        await self._require().set(key, value, self._resolve_ttl(ttl))

    async def delete(self, key: str) -> bool:
        """删一个键，返回是否删掉了。"""
        return await self._require().delete(key)

    async def exists(self, key: str) -> bool:
        """键是否存在（已过期的算不存在）。"""
        return await self._require().exists(key)

    async def expire(self, key: str, ttl: float) -> bool:
        """给已有的键设过期时间，返回是否设上了（键不存在则 ``False``）。"""
        return await self._require().expire(key, ttl)

    async def ttl(self, key: str) -> float | None:
        """剩余存活秒数；``None`` = 键不存在，``math.inf`` = 永不过期。"""
        return await self._require().ttl(key)

    async def incr(self, key: str, amount: int = 1) -> int:
        """原子自增（键不存在时从 0 起算），返回自增后的值。"""
        return await self._require().incr(key, amount)

    async def get_many(self, keys: Sequence[str]) -> dict[str, str]:
        """批量取值，只返回拿到的那些键。"""
        return await self._require().get_many(keys)

    async def set_many(self, items: Mapping[str, str], ttl: float | None = None) -> None:
        """批量写值，同一个 TTL 作用于这一批。"""
        await self._require().set_many(items, self._resolve_ttl(ttl))

    async def delete_many(self, keys: Sequence[str]) -> int:
        """批量删除，返回删掉的键数。"""
        return await self._require().delete_many(keys)

    async def keys(self, pattern: str = "*") -> list[str]:
        """按通配符列键（``*`` / ``?`` / ``[abc]``），默认全部。"""
        return await self._require().keys(pattern)

    async def clear(self) -> int:
        """清掉本缓存的所有键（Redis 后端只清自己命名空间下的），返回清掉的键数。"""
        return await self._require().clear()

    # ---- 内部 ----
    def _require(self) -> CacheBackend:
        """取当前后端；没启动就报错（不隐式启动）。"""
        if self._backend is None:
            raise CacheError("缓存还没启动：先 await cache.start()")
        return self._backend

    def _resolve_ttl(self, ttl: float | None) -> float | None:
        """定下这次写入的 TTL：``None`` 用配置默认值，小于等于 0 都当「永不过期」。"""
        resolved = self._options.default_ttl if ttl is None else ttl
        return resolved if resolved > 0 else None
