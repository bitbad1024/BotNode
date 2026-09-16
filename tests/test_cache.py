"""缓存层单元测试：统一接口、本地内存后端、Redis 后端（降级 / 真连）、配置映射。"""
from __future__ import annotations

import asyncio
import math
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from textwrap import dedent

import pytest

from config import Settings
from nacho.core.cache import (
    Cache,
    CacheBackend,
    CacheError,
    CacheOptions,
    MemoryCache,
    RedisCache,
    RedisOptions,
)


def write(tmp_path: Path, text: str) -> Path:
    """把一段 TOML 落盘，返回路径。"""
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def free_port() -> int:
    """要一个当前没人监听的端口：连它会被立刻拒绝，用来模拟「Redis 没起」。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def unreachable_redis() -> RedisOptions:
    """一份指向「没人监听」的 Redis 选项。"""
    return RedisOptions(host="127.0.0.1", port=free_port(), socket_timeout=1.0)


@pytest.fixture
async def memory() -> AsyncIterator[Cache]:
    """默认门面（memory 后端），用例结束就停。"""
    facade = Cache()
    await facade.start()
    yield facade
    await facade.stop()


class TestMemoryBackend:
    """本地内存后端：不启用 Redis 时，上层实际用的就是它。"""

    async def test_set_get_roundtrip(self, memory: Cache) -> None:
        await memory.set("k", "v")
        assert await memory.get("k") == "v"
        assert await memory.exists("k") is True

    async def test_missing_key_reads_as_none(self, memory: Cache) -> None:
        assert await memory.get("nope") is None
        assert await memory.exists("nope") is False
        assert await memory.ttl("nope") is None

    async def test_set_overwrites_value_and_ttl(self, memory: Cache) -> None:
        await memory.set("k", "one", ttl=60)
        await memory.set("k", "two")
        assert await memory.get("k") == "two"
        assert await memory.ttl("k") == math.inf  # 覆盖时 TTL 也跟着换

    async def test_ttl_expires(self, memory: Cache) -> None:
        await memory.set("k", "v", ttl=0.05)
        assert await memory.get("k") == "v"
        await asyncio.sleep(0.1)
        assert await memory.get("k") is None  # 过期即「不存在」，与 Redis 一致

    async def test_ttl_reporting(self, memory: Cache) -> None:
        await memory.set("forever", "v")
        assert await memory.ttl("forever") == math.inf
        await memory.set("soon", "v", ttl=5)
        remaining = await memory.ttl("soon")
        assert remaining is not None and 0 < remaining <= 5

    async def test_expire_sets_deadline(self, memory: Cache) -> None:
        await memory.set("k", "v")
        assert await memory.expire("k", 0.05) is True
        await asyncio.sleep(0.1)
        assert await memory.exists("k") is False
        assert await memory.expire("nope", 5) is False  # 键不存在，设不上

    async def test_delete(self, memory: Cache) -> None:
        await memory.set("k", "v")
        assert await memory.delete("k") is True
        assert await memory.delete("k") is False

    async def test_incr_counts_from_zero(self, memory: Cache) -> None:
        assert await memory.incr("n") == 1  # 键不存在时从 0 起算
        assert await memory.incr("n") == 2
        assert await memory.incr("n", 5) == 7
        assert await memory.get("n") == "7"

    async def test_incr_rejects_non_integer(self, memory: Cache) -> None:
        await memory.set("s", "abc")
        with pytest.raises(CacheError):
            await memory.incr("s")

    async def test_incr_keeps_ttl(self, memory: Cache) -> None:
        await memory.set("n", "1", ttl=5)
        await memory.incr("n")
        remaining = await memory.ttl("n")
        assert remaining is not None and 0 < remaining <= 5  # 自增不该把 TTL 冲掉

    async def test_batch_operations(self, memory: Cache) -> None:
        await memory.set_many({"a": "1", "b": "2"})
        assert await memory.get_many(["a", "b", "c"]) == {"a": "1", "b": "2"}
        assert await memory.delete_many(["a", "b", "c"]) == 2
        assert await memory.delete_many([]) == 0

    async def test_keys_pattern(self, memory: Cache) -> None:
        await memory.set_many({"user:1": "a", "user:2": "b", "post:1": "c"})
        assert sorted(await memory.keys()) == ["post:1", "user:1", "user:2"]
        assert sorted(await memory.keys("user:*")) == ["user:1", "user:2"]

    async def test_keys_skips_expired(self, memory: Cache) -> None:
        await memory.set("gone", "v", ttl=0.05)
        await asyncio.sleep(0.1)
        assert await memory.keys() == []

    async def test_clear(self, memory: Cache) -> None:
        await memory.set_many({"a": "1", "b": "2"})
        assert await memory.clear() == 2
        assert await memory.keys() == []
        assert await memory.clear() == 0

    async def test_default_ttl_applies_when_set_omits_it(self) -> None:
        """配置里的 default_ttl 管「没写 ttl」的那些写入，显式 ttl=0 仍然永不过期。"""
        facade = Cache(CacheOptions(default_ttl=5))
        await facade.start()
        try:
            await facade.set("k", "v")
            remaining = await facade.ttl("k")
            assert remaining is not None and 0 < remaining <= 5
            await facade.set("k", "v", ttl=0)
            assert await facade.ttl("k") == math.inf
        finally:
            await facade.stop()

    async def test_sweeper_drops_expired_keys(self) -> None:
        """没人再访问的过期键由后台任务清掉（只靠惰性过期会一直占着内存）。"""
        backend = MemoryCache(sweep_interval=0.05)
        await backend.start()
        try:
            await backend.set("k", "v", ttl=0.02)
            await asyncio.sleep(0.2)
            assert backend._data == {}  # 看一眼内部存储：这里面空着就是没泄漏
        finally:
            await backend.stop()


class TestFacade:
    """门面本身：生命周期、状态、没启动就调用。"""

    async def test_operations_before_start_raise(self) -> None:
        facade = Cache()
        with pytest.raises(CacheError):
            await facade.get("k")
        assert facade.running is False

    async def test_start_and_stop_are_idempotent(self) -> None:
        facade = Cache()
        await facade.start()
        await facade.start()  # 重复启动是空操作
        assert facade.running is True
        await facade.stop()
        await facade.stop()
        assert facade.running is False

    async def test_default_backend_is_memory(self, memory: Cache) -> None:
        """不启用 Redis 也能直接用 —— 这是默认姿势。"""
        assert memory.options.backend == "memory"
        assert memory.backend_name == "memory"
        assert memory.degraded is False
        assert await memory.ping() is True

    async def test_configure_rejected_after_start(self, memory: Cache) -> None:
        with pytest.raises(CacheError):
            memory.configure(CacheOptions(backend="redis"))


class TestRedisBackend:
    """Redis 后端：可选依赖、连不上时的两种处理，以及本机有服务时的真连。"""

    def test_backends_satisfy_protocol(self) -> None:
        assert isinstance(MemoryCache(), CacheBackend)
        assert isinstance(RedisCache(RedisOptions()), CacheBackend)

    async def test_unreachable_redis_degrades_to_memory(self) -> None:
        """配了 Redis 但连不上：退回本地缓存，上层照旧读写，只是能问出 degraded。"""
        pytest.importorskip("redis")
        facade = Cache(CacheOptions(backend="redis", redis=unreachable_redis()))
        await facade.start()
        try:
            assert facade.degraded is True
            assert facade.backend_name == "memory"
            await facade.set("k", "v")
            assert await facade.get("k") == "v"
        finally:
            await facade.stop()

    async def test_unreachable_redis_raises_without_fallback(self) -> None:
        pytest.importorskip("redis")
        options = CacheOptions(
            backend="redis", fallback_to_memory=False, redis=unreachable_redis()
        )
        facade = Cache(options)
        with pytest.raises(CacheError, match="连不上"):
            await facade.start()
        assert facade.running is False  # 起不来就别留个半死的门面

    async def test_real_redis_roundtrip_if_available(self) -> None:
        """本机有 Redis 就顺带把真后端跑一遍；没有就跳过（不强制装服务）。"""
        pytest.importorskip("redis")
        facade = Cache(
            CacheOptions(
                backend="redis", namespace="nacho-test", redis=RedisOptions(socket_timeout=1.0)
            )
        )
        await facade.start()
        if facade.degraded:
            await facade.stop()
            pytest.skip("本机没有可用的 Redis，跳过真连用例")
        try:
            await facade.clear()
            await facade.set("k", "v", ttl=30)
            assert await facade.get("k") == "v"  # 读回来是 str，不用上层解码
            assert await facade.keys() == ["k"]  # 列出来的是逻辑键，不带 namespace 前缀
            remaining = await facade.ttl("k")
            assert remaining is not None and 0 < remaining <= 30
            await facade.set_many({"a": "1", "b": "2"})
            assert await facade.get_many(["a", "b"]) == {"a": "1", "b": "2"}
            assert await facade.delete_many(["a", "b"]) == 2
            assert await facade.incr("n") == 1
            assert await facade.delete("k") is True
        finally:
            await facade.clear()
            await facade.stop()


class TestOptionsFromMapping:
    """配置区域 -> 缓存选项的转换（app.py 就是这么接的）。"""

    def test_defaults_without_config_section(self) -> None:
        """配置里没有 [cache] 这一节时，转出来的选项就是本地内存版。"""
        options = CacheOptions.from_mapping(Settings().cache.model_dump())
        assert options.backend == "memory"
        assert options.namespace == "nacho"
        assert options.default_ttl == 0.0
        assert options.redis.port == 6379

    def test_reads_region_and_nested_redis(self, tmp_path: Path) -> None:
        settings = Settings.load(
            write(
                tmp_path,
                dedent(
                    """\
                    [cache]
                    backend = "redis"
                    namespace = "app"
                    default_ttl = 30.0
                    fallback_to_memory = false

                    [cache.redis]
                    host = "10.0.0.9"
                    port = 6390
                    db = 3
                    max_connections = 20
                    """
                ),
            )
        )
        options = CacheOptions.from_mapping(settings.cache.model_dump())
        assert (options.backend, options.namespace) == ("redis", "app")
        assert (options.default_ttl, options.fallback_to_memory) == (30.0, False)
        assert (options.redis.host, options.redis.port, options.redis.db) == ("10.0.0.9", 6390, 3)
        assert options.redis.max_connections == 20

    def test_unknown_keys_are_ignored(self) -> None:
        """多写的项不该让缓存层炸掉：校验那一项归配置系统管。"""
        options = CacheOptions.from_mapping({"backend": "memory", "nonsense": 1, "redis": {"x": 1}})
        assert options.backend == "memory"
        assert options.redis.port == 6379
