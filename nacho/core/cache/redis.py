"""Redis 后端：异步 IO 执行命令，键统一加命名空间前缀。

驱动是 redis-py 的异步客户端（可选依赖，``pip install "nacho[redis]"``）：一条命令
一个协程，不阻塞事件循环，连接池由驱动自己管。上面两件事在适配器里做完：

* **前缀**：所有键都拼上 ``<namespace>:``，多个应用共用一个 Redis 实例时互不干扰；
  上层看到 / 传进来的键始终是**不含前缀**的那一份（:meth:`RedisCache.keys` 会把前缀
  去掉再还回去）；
* **异常**：驱动层的 ``RedisError`` 一律翻成 :class:`~nacho.core.cache.models.CacheError`，
  上层不必 import redis 才能接住缓存层的错。

与内存后端的语义对齐写在 :mod:`nacho.core.cache.interfaces`；这里额外注意两点：
``decode_responses=True`` 让读回来的是 ``str`` 而不是 ``bytes``（上层不用解码）；
``keys()`` 用 ``SCAN`` 游标遍历而不是 ``KEYS``（后者在大库上会阻塞整个 Redis）。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from nacho.core.cache.models import CacheError, RedisOptions

if TYPE_CHECKING:  # 驱动是可选依赖，只在类型检查时导入
    from redis.asyncio import Redis


class RedisCache:
    """redis-py 异步客户端封装（符合 :class:`~nacho.core.cache.interfaces.CacheBackend`）。"""

    def __init__(self, options: RedisOptions, *, namespace: str = "nacho") -> None:
        self._options: RedisOptions = options
        self._prefix: str = f"{namespace}:" if namespace else ""
        self._client: Redis | None = None
        self._errors: type[Exception] = Exception  # 驱动异常基类，start() 里换成 RedisError

    # ---- 生命周期 ----
    async def start(self) -> None:
        """连上 Redis（顺手 ping 一次探活）；重复调用是空操作。

        :raises CacheError: 没装 redis 包，或连不上（服务没起、密码不对、地址写错）。
        """
        if self._client is not None:
            return
        try:
            from redis.asyncio import Redis
            from redis.exceptions import RedisError
        except ImportError as exc:
            hint = "RedisCache 需要 redis 包：pip install redis（或 pip install 'nacho[redis]'）"
            raise CacheError(hint) from exc

        options = self._options
        client = Redis(
            host=options.host,
            port=options.port,
            db=options.db,
            username=options.username or None,  # Redis 6+ ACL；空 = 默认用户
            password=options.password or None,  # 空 = 不认证
            socket_timeout=options.socket_timeout,
            max_connections=options.max_connections,
            decode_responses=True,  # 读回来就是 str，和内存后端一致
        )
        self._errors = RedisError
        try:
            await client.ping()
        except RedisError as exc:
            await client.aclose()  # 连不上就别留着连接池
            raise CacheError(
                f"Redis 连不上 {options.host}:{options.port}/{options.db}：{exc}"
            ) from exc
        self._client = client

    async def stop(self, timeout: float = 5.0) -> None:
        """断开连接池；重复调用是空操作。``timeout`` 只为与其它后端对齐签名。"""
        client, self._client = self._client, None
        if client is None:
            return
        await client.aclose()

    async def ping(self) -> bool:
        try:
            await self._call("ping")
        except CacheError:
            return False
        return True

    # ---- 数据操作 ----
    async def get(self, key: str) -> str | None:
        value = await self._call("get", self._full(key))
        return None if value is None else str(value)

    async def set(self, key: str, value: str, ttl: float | None = None) -> None:
        await self._call("set", self._full(key), value, ex=self._ex(ttl))

    async def delete(self, key: str) -> bool:
        return int(await self._call("delete", self._full(key))) > 0

    async def exists(self, key: str) -> bool:
        return int(await self._call("exists", self._full(key))) > 0

    async def expire(self, key: str, ttl: float) -> bool:
        return bool(await self._call("expire", self._full(key), timedelta(seconds=ttl)))

    async def ttl(self, key: str) -> float | None:
        seconds = float(await self._call("ttl", self._full(key)))
        if seconds < 0:  # 协议里 -2 = 键不存在、-1 = 没设过期
            return None if seconds == -2 else float("inf")
        return seconds

    async def incr(self, key: str, amount: int = 1) -> int:
        return int(await self._call("incrby", self._full(key), amount))

    async def get_many(self, keys: Sequence[str]) -> dict[str, str]:
        if not keys:
            return {}
        values: list[object] = await self._call("mget", [self._full(key) for key in keys])
        return {
            key: str(value)
            for key, value in zip(keys, values, strict=True)
            if value is not None
        }

    async def set_many(self, items: Mapping[str, str], ttl: float | None = None) -> None:
        if not items:
            return
        client = self._require_client()
        ex = self._ex(ttl)
        # 走 pipeline：一趟网络往返发完整批，不在每条命令上各等一次 RTT
        pipe = client.pipeline(transaction=False)
        for key, value in items.items():
            pipe.set(self._full(key), value, ex=ex)
        try:
            await pipe.execute()
        except self._errors as exc:
            raise CacheError(f"Redis 批量写入失败：{exc}") from exc

    async def delete_many(self, keys: Sequence[str]) -> int:
        if not keys:
            return 0
        return int(await self._call("delete", *[self._full(key) for key in keys]))

    async def keys(self, pattern: str = "*") -> list[str]:
        client = self._require_client()
        try:
            # SCAN 游标遍历：KEYS 在大库上会阻塞整个 Redis（返回的键带前缀，去掉再还）
            return [self._strip(key) async for key in client.scan_iter(match=self._full(pattern))]
        except self._errors as exc:
            raise CacheError(f"Redis 扫描键失败：{exc}") from exc

    async def clear(self) -> int:
        """只清本命名空间下的键（共享实例时不动别人的数据）。"""
        found = await self.keys()
        return await self.delete_many(found) if found else 0

    # ---- 内部 ----
    @staticmethod
    def _ex(ttl: float | None) -> timedelta | None:
        """TTL 秒数 -> redis-py 要的 ``ex`` 参数（``None`` = 不设过期）。

        redis-py 收 ``timedelta`` 时会按毫秒精度下发，所以小数秒不会被截断。
        """
        return None if ttl is None else timedelta(seconds=ttl)

    def _full(self, key: str) -> str:
        """逻辑键 -> Redis 上的真键（拼命名空间前缀）。"""
        return f"{self._prefix}{key}"

    def _strip(self, key: str) -> str:
        """Redis 上的真键 -> 逻辑键（去掉前缀再交给上层）。"""
        return key[len(self._prefix) :] if self._prefix else key

    def _require_client(self) -> Redis:
        if self._client is None:
            raise CacheError("Redis 缓存还没启动：先 await cache.start()")
        return self._client

    async def _call(self, method: str, *args: object, **kwargs: object) -> Any:
        """把一条命令丢给驱动；驱动层的异常统一翻成 :class:`CacheError`。"""
        client = self._require_client()
        try:
            return await getattr(client, method)(*args, **kwargs)
        except self._errors as exc:
            raise CacheError(f"Redis 命令 {method} 失败：{exc}") from exc
