"""进程级缓存门面：业务代码直接 ``from tickneko.core.cache import cache``。

和调度器的 :data:`~tickneko.core.scheduler.manager.scheduler` 一个路子：这里只放一个进程级
实例，配置由 ``app.py`` 从 ``[cache]`` 区域转成
:class:`~tickneko.core.cache.models.CacheOptions` 再
:meth:`~tickneko.core.cache.core.Cache.configure` 进来 —— 缓存层自己不读配置文件。
"""
from __future__ import annotations

from tickneko.core.cache.core import Cache

#: 进程级默认缓存；没配置就是本地内存版，``start()`` 之后即可用
cache: Cache = Cache()
