"""令牌的生成，以及「令牌摘要 -> 用户 id」在缓存里的映射。

**不是 JWT**：令牌就是一段随机串，本身不带信息、也不可自证；服务端拿它的**摘要**去换用户，
换不出来就是无效。所以令牌是有状态的、能随时吊销，代价是每次请求查一次缓存（没命中再回库）。

只有**一种**令牌，没有 access / refresh 之分：

* 勾「记住设备」→ 这条会话的滑动有效期设长（30 天）+ Cookie 带 ``Max-Age``（持久 Cookie）；
* 不勾 → 滑动有效期短（2 小时）+ Cookie 不带 ``Max-Age``（会话 Cookie，关浏览器即丢）。

缓存里放的是**映射**（``令牌摘要 -> 用户 id``），不是整份用户对象：一条键很小，命中后拿到
user_id 再去按主键取用户也便宜；映射重建毫无成本，缓存整体失效也不会造成"回源把库打穿"。

摘要同时也是 ``auth_sessions`` 表的**主键**（表列就叫 ``token_hash``）：不可逆，所以拿它
当对外 id 是安全的 —— 设备列表、吊销接口传的都是它，不需要再另起一套编号。
"""
from __future__ import annotations

import hashlib
import secrets

from nacho.core.cache import Cache, cache as process_cache

#: 令牌前缀：日志里一眼认出来
TOKEN_PREFIX: str = "nacho_"
#: 缓存键前缀（前面还有 cache 门面自己的 namespace）
_TOKEN_KEY: str = "auth:token:"


def generate_token() -> str:
    """造一个令牌（明文）；只出现在签发那一刻，服务端只留摘要。

    32 字节随机 = 256 位熵，不必查重（``auth_sessions`` 那边还有主键兜底），推算见
    :func:`nacho.onebot.tokens.generate_token`。
    """
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """令牌摘要（sha256 十六进制）：缓存键与 ``auth_sessions.token_hash`` 都用它，不存明文。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenIndex:
    """「令牌摘要 -> 用户 id」在缓存里的映射。"""

    def __init__(self, cache: Cache | None = None) -> None:
        """:param cache: 缓存门面；不传就用进程级那个（主程序已经 ``start()`` 过）。"""
        self._cache: Cache = cache if cache is not None else process_cache
        #: 进程级缓存没启动时的兜底（自己的内存缓存），见 :meth:`_active`
        self._local: Cache | None = None

    async def _active(self) -> Cache:
        """这次真正往哪写。

        正常路径是配置好的那份（主程序先 ``cache.start()``，可能是 Redis）。但单独用接口层时
        ——示例、测试直接 :func:`nacho.api.create_app`——没人替我们启动它，那份门面还是"没跑"
        状态，写就读就抛 :class:`CacheError`。此时退到一份自己的内存缓存，让接口层**不依赖
        外部启动步骤也能跑**（和"不接数据库也能跑起来"是同一个态度）。

        代价说清楚：这份兜底是**进程内**的，重启即失效、多进程之间也不共享。正式跑请走主程序。
        """
        if self._cache.running:
            return self._cache
        if self._local is None:
            self._local = Cache()
            await self._local.start()
        return self._local

    async def bind(self, *, token_hash: str, user_id: str, ttl: float) -> None:
        """把令牌摘要绑到用户上。

        值写成 ``"<user_id>|<ttl>"``：滑动续期时要**知道这条会话该延多久**（勾没勾「记住
        设备」差着两个数量级），而这个数字只有登录时算过；每个请求为了拿它多查一次库不值当，
        所以顺手存进缓存。仍然是「令牌 -> 用户 id」的映射，只是多带上"该延多久"。

        :param ttl: 秒；``<= 0`` 表示永不过期（门面会把非正数归一成"永久"）。
        """
        cache: Cache = await self._active()
        await cache.set(_TOKEN_KEY + token_hash, f"{user_id}|{int(ttl)}", ttl)

    async def resolve(self, token_hash: str) -> tuple[str, float] | None:
        """拿摘要换 ``(用户 id, 该延多久)``；缓存里没有返回 ``None``。

        值坏了（不是 ``id|ttl`` 这个形状）也当没有——缓存里的东西不作数就别信它。
        """
        if not token_hash:
            return None
        cache: Cache = await self._active()
        raw: str | None = await cache.get(_TOKEN_KEY + token_hash)
        if not raw:
            return None
        user_id, _, ttl_text = raw.partition("|")
        if not user_id:
            return None
        try:
            return user_id, float(ttl_text or 0)
        except ValueError:
            return None

    async def slide(self, token_hash: str, *, ttl: float) -> None:
        """滑动续期：把这个键往后延（``ttl <= 0`` 是永久，不用延）。"""
        if ttl <= 0 or not token_hash:
            return
        cache: Cache = await self._active()
        await cache.expire(_TOKEN_KEY + token_hash, ttl)

    async def drop(self, token_hash: str) -> bool:
        """删掉这个键（吊销的第一步）；删到过返回 ``True``。

        注意**"什么都没删到"不算失败**：会话本来就没绑过（比如缓存重启过、或者令牌从没
        被用过），属于正常情况，调用方照旧接着删库。
        """
        if not token_hash:
            return False
        cache: Cache = await self._active()
        return await cache.delete(_TOKEN_KEY + token_hash)
