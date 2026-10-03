"""鉴权的业务编排：开新账号（注册）与登录 —— 查人 -> 比密码 -> 查停用 -> 开会话（并发令牌）。

令牌不是 JWT、也不能自证：签发时把访问令牌绑进缓存（``令牌摘要 -> 用户 id``），
每次用令牌就是拿它去缓存里换会话，顺手滑动续期；会话/设备与长期令牌摘要落在
``auth_sessions`` 表。这一套编排在 :class:`~tickneko.api.services.session.SessionService`
里，本类只管登录这一段业务（密码对不对、账号停没停用）。

依赖的三样能力（用户怎么查、密码怎么算、会话怎么开）都走协议，所以换数据库、换哈希算法、
换令牌形式都不用动入口层。

它也不认识 FastAPI：失败时抛 :class:`~tickneko.api.common.errors.ApiError`，状态码已经挂在
异常上；要换协议（比如做成消息入口）这份逻辑能直接用。HTTP 入口在 :mod:`tickneko.api.api.auth`。
"""
from __future__ import annotations

from tickneko.core.logger import BaseLogger

from ...common.errors import (
    AccountAlreadyExistsError,
    AccountDisabledError,
    InvalidCredentialsError,
    PasswordMismatchError,
    UnauthorizedError,
    ValidationError,
)
from ...logging import API_LOGGER_NAME, api_logger
from ..session.models import ClientInfo, IssuedSession, SessionRecord
from ..session.service import SessionService
from ..user.models import UserProfile, profile_of
from ..user.protocols import PasswordHasher, UserStore
from ..user.security import Pbkdf2PasswordHasher
from .models import Credentials, CurrentUser, LoginResult


class AuthService:
    """登录与注册服务：把「用户存储 / 密码哈希 / 会话」三份能力串成一次登录，外加开新账号。"""

    def __init__(
        self,
        store: UserStore,
        *,
        hasher: PasswordHasher | None = None,
        sessions: SessionService,
        logger: BaseLogger | None = None,
    ) -> None:
        """
        :param store: 用户存储（按账号 / 按 id 取人）；
        :param hasher: 密码哈希器，默认 PBKDF2；
        :param sessions: 会话服务（开会话、认令牌、吊销），必传——令牌现在是有状态的；
        :param logger: 业务日志实例，默认 ``api`` 那个。
        """
        self._store: UserStore = store
        self._hasher: PasswordHasher = hasher if hasher is not None else Pbkdf2PasswordHasher()
        self._sessions: SessionService = sessions
        self._logger: BaseLogger | None = logger

    @property
    def store(self) -> UserStore:
        """用户存储（测试和扩展时会用到）。"""
        return self._store

    @property
    def sessions(self) -> SessionService:
        """会话服务（路由拿它做"登录设备"与吊销）。"""
        return self._sessions

    @property
    def token_ttl(self) -> float:
        """访问令牌的滑动有效期（秒）。"""
        return self._sessions.access_ttl

    # ------------------------------------------------------------------ 登录
    async def login(
        self,
        credentials: Credentials,
        *,
        client: ClientInfo,
        remember: bool = False,
        previous_token: str = "",
        trace_id: str = "-",
    ) -> LoginResult:
        """登录：成功给令牌 + 资料，失败抛 401 / 403 的 :class:`ApiError`。

        密码只在 :meth:`~tickneko.api.services.user.protocols.PasswordHasher.verify` 里出现一次，
        不进日志。账号不存在与密码错是同一个 401：不告诉对方账号到底存不存在。

        :param client: 这次登录从哪来（ip、设备、浏览器），记进会话；
        :param remember: 是否「记住设备」；勾了才发长期令牌，能做下次自动登录。
        :param previous_token: 客户端手里那个旧令牌，认得出且属于同一个人就**接着用它**
            （延长有效期、不新建会话），否则照常发一个新的。复用只是尽力而为的优化——
            认不出来、不是本人、缓存抽风，都不影响这次登录成功。
        """
        account: str = credentials.account
        user = await self._store.get_by_account(account)
        password: str = credentials.password

        if user is None or not self._hasher.verify(password, user.password_hash):
            self._log().warning("登录失败", account=account, reason="凭据不对", trace_id=trace_id)
            raise InvalidCredentialsError()
        if user.disabled:
            self._log().warning("登录失败", account=account, reason="账号停用", trace_id=trace_id)
            raise AccountDisabledError()

        # 先试复用旧令牌：同一台设备反复登录不该在设备列表里堆出一串记录，也不该把手里的
        # Cookie 作废。复用不成（没传 / 认不出 / 不是本人 / 缓存抽风）就照常发新的。
        issued: IssuedSession | None = None
        if previous_token:
            issued = await self._sessions.reuse(
                previous_token, user_id=user.id, remember=remember
            )
        reused: bool = issued is not None
        if issued is None:
            issued = await self._sessions.open(user.id, client=client, remember=remember)
        self._log().info(
            "登录成功",
            owner_id=user.id,  # 审计事件归属本人：普通用户在 /logs 里也查得到自己的这条
            account=account,
            user_id=user.id,
            token_hash=issued.session.token_hash,
            device=issued.session.device_name,
            remembered=remember,
            reused=reused,
            trace_id=trace_id,
        )
        return LoginResult(
            token=issued.token,
            expires_in=issued.expires_in,
            user=profile_of(user),
            session=issued.session,
            reused=reused,
        )

    # ------------------------------------------------------------------ 改密码
    async def change_password(
        self,
        user_id: str,
        *,
        current_password: str,
        new_password: str,
        keep_token_hash: str = "",
        trace_id: str = "-",
    ) -> int:
        """改密码：验旧密码 -> 写新哈希 -> **把这个用户的其他登录踢下线**，返回踢掉几条。

        三条规则只写在这一处（换个入口照样管用）：

        * 旧密码不对 -> 403（:class:`PasswordMismatchError`）：已登录 ≠ 能改密码，
          否则令牌一旦泄露就等于账号被接管；
        * 新密码与当前密码相同 -> 422（:class:`ValidationError`）：拿「改密码」当空操作没意义；
        * 成功 -> 吊销**其他**会话（``keep_token_hash`` 那条留着，就是发起这次修改的设备）：
          密码都变了，别处手里那个令牌不该还能用。

        新密码的长度由请求层的 ``Password`` 保证（422），这里不重复判。
        """
        user = await self._store.get_by_id(user_id)
        if user is None:
            # 会话指向的用户没了（被删）：按「登录失效」处理，不在这里泄露「这个 id 存不存在」
            raise UnauthorizedError("登录状态已失效，请重新登录")
        if not self._hasher.verify(current_password, user.password_hash):
            self._log().warning(
                "改密码失败", owner_id=user_id, reason="当前密码不对", trace_id=trace_id
            )
            raise PasswordMismatchError()
        if self._hasher.verify(new_password, user.password_hash):
            raise ValidationError("新密码不能与当前密码相同")
        await self._store.set_password(user_id, self._hasher.hash(new_password))
        revoked: int = await self._revoke_others(user_id, keep_token_hash)
        self._log().info(
            "密码已修改",
            owner_id=user_id,  # 审计事件归属本人：普通用户在 /logs 里也查得到自己这条
            revoked_sessions=revoked,
            trace_id=trace_id,
        )
        return revoked

    async def _revoke_others(self, user_id: str, keep_token_hash: str) -> int:
        """吊销这个用户除 ``keep_token_hash`` 之外的全部会话，返回踢掉几条。

        认不出「当前那条」（理论上不会：认令牌时总会带上摘要）就**一条都不踢** —— 宁可少踢，
        也不能把发起这次修改的人自己踢下线。
        """
        if not keep_token_hash:
            return 0
        revoked: int = 0
        for session in await self._sessions.list_for_user(user_id):
            if session.token_hash == keep_token_hash:
                continue
            if await self._sessions.revoke(session.token_hash, user_id=user_id):
                revoked += 1
        return revoked

    # ------------------------------------------------------------------ 注册
    async def register(
        self,
        *,
        account: str,
        password: str,
        nickname: str = "",
        trace_id: str = "-",
    ) -> UserProfile:
        """注册一个新账号，返回它的对外资料；账号已被占用抛 409 的 :class:`ApiError`。

        与登录刻意相反：注册**要**说清「这个账号有人了」——前端得据此提示换一个。

        密码只在 :meth:`~tickneko.api.services.user.protocols.PasswordHasher.hash` 里出现一次，
        不进日志、不落明文。先查一遍再写，是为了常规路径给个明确答案、并且**不必为注定失败的
        请求白算一次哈希**（PBKDF2 不便宜）；真正的兜底在落库那步的唯一约束上：并发下两个请求
        同时注册同一个账号，由它拦下并翻成同一个异常。

        :param nickname: 显示名（可留空；前端展示时自己回落到账号）。
        """
        if await self._store.get_by_account(account) is not None:
            self._log().warning(
                "注册失败", account=account, reason="账号已被注册", trace_id=trace_id
            )
            raise AccountAlreadyExistsError()
        try:
            user = await self._store.add(
                account=account,
                password_hash=self._hasher.hash(password),
                nickname=nickname,
            )
        except AccountAlreadyExistsError:
            # 刚查过又被别人抢注（并发）：补一条日志，异常原样往外抛——调用方只认这一种
            self._log().warning(
                "注册失败", account=account, reason="账号已被注册", trace_id=trace_id
            )
            raise
        self._log().info(
            "注册成功",
            owner_id=user.id,  # 审计事件归属本人：新账号在 /logs 里也查得到自己这条
            account=account,
            user_id=user.id,
            nickname=user.nickname,
            trace_id=trace_id,
        )
        return profile_of(user)

    # ------------------------------------------------------------------ 认令牌
    async def current_user(self, token: str, *, trace_id: str = "-") -> CurrentUser:
        """按访问令牌取当前用户；令牌无效由会话服务抛 401。

        令牌有效但账号没了 / 被停用了，同样按 401 处理（不是 500：是这份凭据不再可用）。
        """
        authenticated = await self._sessions.authenticate(token)
        user = await self._store.get_by_id(authenticated.user_id)
        if user is None or user.disabled:
            self._log().warning(
                "令牌对应的账号不可用",
                owner_id=authenticated.user_id,
                user_id=authenticated.user_id,
                trace_id=trace_id,
            )
            raise UnauthorizedError("账号不可用")
        return CurrentUser(
            user=profile_of(user),
            token_hash=authenticated.token_hash,
            expires_in=authenticated.expires_in,
        )

    # ------------------------------------------------------------------ 登录设备
    async def sessions_of(self, user_id: str) -> tuple[SessionRecord, ...]:
        """这个用户开着的全部登录（新的在前）。"""
        return await self._sessions.list_for_user(user_id)

    async def revoke_session(self, token_hash: str, *, user_id: str) -> bool:
        """吊销一条登录（双删：先缓存后库，任一步失败即失败）。"""
        return await self._sessions.revoke(token_hash, user_id=user_id)

    async def revoke_all_sessions(self, user_id: str) -> int:
        """把这个用户的登录全部下线（**包含当前这条**）。"""
        return await self._sessions.revoke_all(user_id)

    def _log(self) -> BaseLogger:
        """业务日志实例（``api``）；用完即取，避免核心被替换后拿到旧的。"""
        return self._logger if self._logger is not None else api_logger(API_LOGGER_NAME)
