"""会话服务：开一次登录、认一个令牌、滑动续期、列出设备、吊销。

**只有一种令牌**，它同时是三样东西::

    明文        给客户端的随机串，放 HttpOnly Cookie（JS 读不到）
    摘要        表主键（表列 token_hash）= 对外的 id = 缓存键（不可逆，露出去不怕）
    滑动有效期  勾「记住设备」用 remember_ttl（默认 30 天），不勾用 access_ttl（默认 2 小时）

**登录时可以复用旧令牌**（:meth:`SessionService.reuse`）：客户端手里那个令牌还认得出、且属于
同一个人，就延长它的有效期、重写缓存，**不新建会话**——同一台设备反复登录不会在设备列表里堆出
一串记录，手里的 Cookie 也不用换。认不出来（不在 / 不是本人）就照常发一个新的：复用是尽力而为
的优化，不是登录的前置条件。

两边的分工（**这点最要紧**）::

    缓存（auth:token:<摘要> -> user_id）  会话**还活着**的凭据：
        命中就是有效，顺手把 TTL 往后延（有通讯就一直延期）；
        过期就是闲置太久 —— 直接失效，不回库翻找
    库里（auth_sessions 一行/次登录）      **设备记录**：
        谁、什么时候、从哪台机器登录的；用于「登录设备」列表与吊销

所以「闲置过期」只活在缓存里。这也是为什么**吊销必须先删缓存**：缓存删不掉就等于没吊销
（库里删了也没用，请求根本不看库）。双删的顺序与后果：

* 缓存删失败 → 直接抛错，**不动库**（那条设备记录还在，重试即可）；
* 缓存删成功、库删失败 → 抛错。令牌已经不可用（缓存里没了），库里的行会残留在
  「登录设备」里，用户重试一次就能删掉。

**取舍照实说**：因为活着的凭据只在缓存里，缓存一没（进程重启、Redis 重启）全员都要重登。
单机默认就是进程内内存缓存，"进程重启要重登"本来就是预期；真要不掉线，得让库里也记
``last_seen_at`` 并按它判闲置——那是另一套取舍，现在没做。
"""
from __future__ import annotations

from tickneko.core.cache import CacheError
from tickneko.core.logger import BaseLogger

from ...common.errors import InternalError, UnauthorizedError
from ...logging import API_LOGGER_NAME, api_logger
from ...options import DEFAULT_REMEMBER_TTL, DEFAULT_TOKEN_TTL
from .models import Authenticated, ClientInfo, IssuedSession, SessionRecord
from .protocols import SessionStore
from .tokens import TokenIndex, generate_token, hash_token


class SessionService:
    """会话的编排：令牌（缓存）与设备记录（库）两边都对上。"""

    def __init__(
        self,
        store: SessionStore,
        *,
        index: TokenIndex | None = None,
        access_ttl: float = DEFAULT_TOKEN_TTL,
        remember_ttl: float = DEFAULT_REMEMBER_TTL,
        logger: BaseLogger | None = None,
    ) -> None:
        """
        :param store: 会话存储（落库 / 内存）；
        :param index: 令牌在缓存里的映射，默认用进程级缓存门面；
        :param access_ttl: **不勾**「记住设备」时的滑动有效期（秒）；``<= 0`` 表示永不过期；
        :param remember_ttl: **勾了**「记住设备」时的滑动有效期（秒）；``<= 0`` 表示永不过期；
        :param logger: 业务日志实例，默认 ``api`` 那个。
        """
        self._store: SessionStore = store
        self._index: TokenIndex = index if index is not None else TokenIndex()
        self._access_ttl: float = access_ttl
        self._remember_ttl: float = remember_ttl
        self._logger: BaseLogger | None = logger

    @property
    def access_ttl(self) -> float:
        """不勾「记住设备」时的滑动有效期（秒）；``<= 0`` 表示永久。"""
        return self._access_ttl

    @property
    def remember_ttl(self) -> float:
        """勾了「记住设备」时的滑动有效期（秒）；``<= 0`` 表示永久。"""
        return self._remember_ttl

    @property
    def store(self) -> SessionStore:
        """会话存储（测试与扩展时会用到）。"""
        return self._store

    # ------------------------------------------------------------------ 开 / 认 / 续
    async def open(
        self, user_id: str, *, client: ClientInfo, remember: bool = False
    ) -> IssuedSession:
        """开一次会话：库里记一行设备，缓存里绑上令牌。

        :raises InternalError: 缓存绑不上。**这里必须失败**——令牌只活在缓存里，
            绑不上却照样把令牌发出去，等于发了一个假令牌（客户端一用就 401）。
        :raises TokenHashCollisionError: 摘要撞了（撞不上，见该异常）。写库这一步就失败，
            所以**缓存还没绑、别人那条会话分毫未动**，这一次登录直接失败。
        """
        ttl: float = self.ttl_for(remember)
        token: str = generate_token()
        digest: str = hash_token(token)
        session: SessionRecord = await self._store.create(
            digest, user_id, client=client, remembered=remember
        )
        try:
            await self._index.bind(token_hash=digest, user_id=user_id, ttl=ttl)
        except CacheError as exc:
            self._log().error("会话缓存不可用，登录中止", token_hash=digest, error=str(exc) or repr(exc))
            raise InternalError("会话缓存不可用，请稍后重试") from exc
        self._log().info(
            "会话已开启",
            owner_id=user_id,  # 审计事件归属本人
            token_hash=digest,
            user_id=user_id,
            device=client.device_name,
            remembered=remember,
        )
        return IssuedSession(session=session, token=token, expires_in=self._seconds(ttl))

    async def reuse(
        self, token: str, *, user_id: str, remember: bool = False
    ) -> IssuedSession | None:
        """拿一个旧令牌**接着用**：延长它的有效期、重写缓存，不新建会话。

        "旧令牌还在"按**缓存**判（会话还活着这件事只记在缓存里），并且要求它认出来的就是
        ``user_id`` 本人——**不能因为"知道现在是谁在登录"就把别人的令牌给续了**。

        成功时返回的 :class:`IssuedSession` 里，``token`` 就是传进来的那个旧令牌（原样发回，
        客户端手里的 Cookie 不用换）；认不出来 / 不是本人 / 设备记录不在了都返回 ``None``，
        由调用方改走 :meth:`open` 发一个新的。

        这里用 :meth:`TokenIndex.bind` **重写**而不是 :meth:`TokenIndex.slide`：缓存的值里存着
        "这条会话该延多久"，本次勾选和当初那次可能不一样（2 小时 ↔ 30 天），得一起改掉；
        重写顺带就把 TTL 拨满了。

        :param remember: 本次登录勾没勾「记住设备」；按它选档，并同步进设备记录。
        """
        if not token:
            return None
        digest: str = hash_token(token)
        try:
            found: tuple[str, float] | None = await self._index.resolve(digest)
        except CacheError as exc:
            # 缓存不可用：当"认不出来"处理，退回发新令牌（那条路照样会报缓存不可用）
            self._log().warning("复用失败：会话缓存不可用", token_hash=digest, error=str(exc) or repr(exc))
            return None
        if found is None or found[0] != user_id:
            return None
        session: SessionRecord | None = await self._store.get(digest)
        if session is None:
            # 库那行是设备记录，复用要把它读出来回给前端；读不到说明状态不对（缓存有、库没，
            # 正常到不了），宁可另发一个，也别复活一条没有记录的会话
            return None
        ttl: float = self.ttl_for(remember)
        try:
            await self._index.bind(token_hash=digest, user_id=user_id, ttl=ttl)
        except CacheError as exc:
            self._log().error("会话缓存不可用，复用中止", token_hash=digest, error=str(exc) or repr(exc))
            raise InternalError("会话缓存不可用，请稍后重试") from exc
        if session.remembered != remember:
            # 设备记录里那一栏也得跟着变，不然列表上显示的还是当初那次的选择
            updated: SessionRecord | None = await self._store.set_remembered(
                digest, remembered=remember
            )
            session = updated if updated is not None else session
        self._log().info(
            "会话已复用",
            owner_id=user_id,  # 审计事件归属本人
            token_hash=digest,
            user_id=user_id,
            device=session.device_name,
            remembered=remember,
        )
        return IssuedSession(session=session, token=token, expires_in=self._seconds(ttl))

    async def authenticate(self, token: str) -> Authenticated:
        """拿令牌认人，顺手滑动续期。

        只看缓存：命中就算有效（并把 TTL 往后延），没命中就是无效或闲置太久。

        :raises UnauthorizedError: 令牌无效 / 已闲置过期 / 被吊销。
        """
        if not token:
            raise UnauthorizedError("缺少令牌")
        digest: str = hash_token(token)
        found: tuple[str, float] | None = await self._index.resolve(digest)
        if found is None:
            raise UnauthorizedError("令牌无效或已过期")
        user_id, ttl = found
        try:
            await self._index.slide(digest, ttl=ttl)
        except CacheError as exc:
            # 续期失败不致命：这次照样放行，只是它按原 TTL 到期（缓存抽风不该把人踢下线）
            self._log().warning("会话续期失败", token_hash=digest, error=str(exc) or repr(exc))
        return Authenticated(
            user_id=user_id, token_hash=digest, expires_in=self._seconds(ttl)
        )

    def ttl_for(self, remember: bool) -> float:
        """这条会话该用哪个滑动有效期。"""
        return self._remember_ttl if remember else self._access_ttl

    # ------------------------------------------------------------------ 设备列表 / 吊销
    async def list_for_user(self, user_id: str) -> tuple[SessionRecord, ...]:
        """这个用户的全部登录（新的在前），给「登录设备」列表用。"""
        return await self._store.list_for_user(user_id)

    async def revoke(self, token_hash: str, *, user_id: str) -> bool:
        """吊销一条会话（**双删**：先缓存后库，任一步失败即失败）。

        :param user_id: 必须是这条会话的主人，否则当"没有这条会话"处理（不回 403，
            免得拿别人的摘要试探出"存在但没权限"）。
        """
        session: SessionRecord | None = await self._store.get(token_hash)
        if session is None or session.user_id != user_id:
            return False
        await self._drop_cache(token_hash)
        removed: bool = await self._store.remove(token_hash)
        self._log().info(
            "会话已吊销",
            owner_id=user_id,  # 审计事件归属本人
            token_hash=token_hash,
            user_id=user_id,
            device=session.device_name,
        )
        return removed

    async def revoke_all(self, user_id: str) -> int:
        """把这个用户的全部登录下线（**包含当前这条**），返回吊销了几条。

        同样是双删：**先把所有会话的缓存删完**（任一失败就中止、一行都不删），
        再一次性删库——避免出现"删了一半"的中间态。
        """
        sessions: tuple[SessionRecord, ...] = await self._store.list_for_user(user_id)
        for session in sessions:
            await self._drop_cache(session.token_hash)
        removed: int = await self._store.remove_all(user_id)
        self._log().info(
            "全部会话已吊销", owner_id=user_id, user_id=user_id, count=removed
        )
        return removed

    # ------------------------------------------------------------------ 内部
    async def _drop_cache(self, token_hash: str) -> None:
        """双删的第一步：删缓存。失败就抛错——调用方据此**不再动库**。"""
        try:
            await self._index.drop(token_hash)
        except CacheError as exc:
            self._log().error("吊销失败：缓存删不掉", token_hash=token_hash, error=str(exc) or repr(exc))
            raise InternalError("吊销失败：会话缓存不可用") from exc

    def _seconds(self, ttl: float) -> int:
        """响应里回多少秒；``0`` 表示不过期（前端据此不显示倒计时）。"""
        return max(0, int(ttl))

    def _log(self) -> BaseLogger:
        """业务日志实例（``api``）；用完即取，避免核心被替换后拿到旧的。"""
        return self._logger if self._logger is not None else api_logger(API_LOGGER_NAME)
