"""本地内存缓存：不启用 Redis 时的落点，也是 Redis 连不上时的降级兜底。

数据只活在当前进程里：换进程就没了，也不跨机器共享；换来的是零依赖、零网络。
语义与 Redis 后端完全对齐（见 :mod:`nacho.core.cache.interfaces`），所以上层换了后端
不用改代码 —— 唯一真正不同的是「没有别人能看到你写的东西」。
"""
from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Mapping, Sequence
from fnmatch import fnmatchcase

from nacho.core.cache.models import CacheError
from nacho.core.logger import get_logger


class MemoryCache:
    """进程内存里的键值缓存（带 TTL）。

    存储就是一个 dict：``key -> (值, 过期时刻)``，过期时刻为 ``None`` 表示永不过期。
    读写都在事件循环线程里一口气做完（方法体内没有 ``await``），所以天然是原子的，
    不需要锁。

    过期键两条路清理：读到 / 查到的时候顺手删（惰性），另有后台任务定期扫一遍
    （没人再访问的过期键只能靠它，否则会一直占着内存）。
    """

    def __init__(self, *, sweep_interval: float = 30.0) -> None:
        self._data: dict[str, tuple[str, float | None]] = {}
        self._sweep_interval: float = sweep_interval
        self._sweeper: asyncio.Task[None] | None = None

    # ---- 生命周期 ----
    async def start(self) -> None:
        """起后台清扫任务；重复调用是空操作。"""
        if self._sweeper is not None and not self._sweeper.done():
            return
        self._sweeper = asyncio.create_task(self._sweep_loop())

    async def stop(self, timeout: float = 5.0) -> None:
        """停后台清扫任务；数据留着（进程内存，本来也不跨进程）。"""
        sweeper, self._sweeper = self._sweeper, None
        if sweeper is None:
            return
        sweeper.cancel()
        # asyncio.wait 不会把子任务被取消当异常抛出来；外层若要取消本协程，
        # 这行 await 自己会抛 CancelledError，正好往上传播（停机信号不能被吞）
        _, pending = await asyncio.wait({sweeper}, timeout=timeout)
        if pending:  # 取消是瞬时的，走到这里说明另有情况，记一笔但不等了
            get_logger("cache").warning("内存缓存的清扫任务没在超时内停下")

    async def ping(self) -> bool:
        """本地缓存永远在线。"""
        return True
    #(TODO)应该以后实现redis的全部抽象操作，比如json，列表，哈希的哈希
    # ---- 数据操作（都无 await：一步做完，不用锁）----
    async def get(self, key: str) -> str | None:
        entry = self._live(key)
        return entry[0] if entry is not None else None

    async def set(self, key: str, value: str, ttl: float | None = None) -> None:
        deadline = None if ttl is None else time.monotonic() + ttl
        self._data[key] = (value, deadline)

    async def delete(self, key: str) -> bool:
        return self._data.pop(key, None) is not None

    async def exists(self, key: str) -> bool:
        return self._live(key) is not None

    async def expire(self, key: str, ttl: float) -> bool:
        entry = self._live(key)
        if entry is None:
            return False
        self._data[key] = (entry[0], time.monotonic() + ttl)
        return True

    async def ttl(self, key: str) -> float | None:
        entry = self._live(key)
        if entry is None:
            return None
        deadline = entry[1]
        if deadline is None:
            return math.inf
        return max(0.0, deadline - time.monotonic())

    async def incr(self, key: str, amount: int = 1) -> int:
        entry = self._live(key)
        current = "0" if entry is None else entry[0]
        try:
            value = int(current) + amount
        except ValueError as exc:  # 值和 Redis 一样要求是整数字符串
            raise CacheError(f"键 {key} 的值不是整数，不能自增：{current!r}") from exc
        deadline = None if entry is None else entry[1]  # 自增不动 TTL（与 Redis 一致）
        self._data[key] = (str(value), deadline)
        return value

    async def get_many(self, keys: Sequence[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        for key in keys:
            entry = self._live(key)
            if entry is not None:
                found[key] = entry[0]
        return found

    async def set_many(self, items: Mapping[str, str], ttl: float | None = None) -> None:
        deadline = None if ttl is None else time.monotonic() + ttl
        for key, value in items.items():
            self._data[key] = (value, deadline)

    async def delete_many(self, keys: Sequence[str]) -> int:
        return sum(1 for key in keys if self._data.pop(key, None) is not None)

    async def keys(self, pattern: str = "*") -> list[str]:
        self._purge()  # 顺手把过期的摘掉：列出来的键都在有效期内
        return [key for key in self._data if fnmatchcase(key, pattern)]

    async def clear(self) -> int:
        count = len(self._data)
        self._data.clear()
        return count

    # ---- 内部 ----
    async def _sweep_loop(self) -> None:
        """按时扫一遍过期键（惰性过期兜不住没人再访问的键）。"""
        while True:
            await asyncio.sleep(self._sweep_interval)
            self._purge()

    def _live(self, key: str) -> tuple[str, float | None] | None:
        """取未过期的条目；已过期的顺手删掉（惰性过期）。"""
        entry = self._data.get(key)
        if entry is None:
            return None
        deadline = entry[1]
        if deadline is not None and deadline <= time.monotonic():
            del self._data[key]
            return None
        return entry
    #(TODO)仿照redis设计，抽样删除，取值时删除
    def _purge(self) -> int:
        """把已过期的键真正摘掉，返回摘掉的条数。"""
        now = time.monotonic()
        stale = [
            key
            for key, (_, deadline) in self._data.items()
            if deadline is not None and deadline <= now
        ]
        for key in stale:
            del self._data[key]
        return len(stale)
