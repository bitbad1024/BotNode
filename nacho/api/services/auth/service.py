"""鉴权的业务编排：查人 -> 比密码 -> 查停用 -> 签令牌。

这是鉴权模块里唯一一块「业务逻辑」：进去的是
:class:`~nacho.api.services.auth.models.Credentials`、出来的是
:class:`~nacho.api.services.auth.models.LoginResult` —— 都是业务层的对象，不认识 HTTP。
依赖的两样能力（用户怎么查、密码怎么算、令牌怎么签）都走 :mod:`nacho.api.services.user`
与本模块 :mod:`nacho.api.services.auth.protocols` 的协议，所以换数据库、换哈希算法、换令牌
形式都不用动这里，也不用动入口层。

它也不认识 FastAPI：失败时抛 :class:`~nacho.api.common.errors.ApiError`，状态码已经挂在
异常上；要换协议（比如做成消息入口）这份逻辑能直接用。HTTP 入口在
:mod:`nacho.api.api.auth`。
"""
from __future__ import annotations

from nacho.core.logger import BaseLogger

from ...common.errors import AccountDisabledError, InvalidCredentialsError, UnauthorizedError
from ...logging import API_LOGGER_NAME, api_logger
from ...options import DEFAULT_TOKEN_TTL
from ..user.models import UserProfile, profile_of
from ..user.protocols import PasswordHasher, UserStore
from ..user.security import Pbkdf2PasswordHasher
from .models import Credentials, LoginResult
from .protocols import TokenService
from .security import HmacTokenService, resolve_secret


class AuthService:
    """登录服务：把三份能力（用户存储 / 密码哈希 / 令牌签发）串成一次登录。

    :param store: 用户存储（按账号 / 按 id 取人）；
    :param hasher: 密码哈希器，默认 PBKDF2；
    :param tokens: 令牌签发器，默认 HMAC 令牌（密钥用 :func:`resolve_secret` 定）；
    :param ttl: 令牌有效期（秒），签发时按次传给 ``tokens``；
    :param secret: 令牌密钥，空串表示现生成一个随机的（重启即失效）。
    """

    def __init__(
        self,
        store: UserStore,
        *,
        hasher: PasswordHasher | None = None,
        tokens: TokenService | None = None,
        ttl: float = DEFAULT_TOKEN_TTL,
        secret: str = "",
        logger: BaseLogger | None = None,
    ) -> None:
        self._store: UserStore = store
        self._hasher: PasswordHasher = hasher if hasher is not None else Pbkdf2PasswordHasher()
        self._tokens: TokenService = (
            tokens if tokens is not None else HmacTokenService(resolve_secret(secret), ttl=ttl)
        )
        self._ttl: float = ttl
        self._logger: BaseLogger | None = logger

    @property
    def store(self) -> UserStore:
        """用户存储（测试和扩展时会用到）。"""
        return self._store

    @property
    def token_ttl(self) -> float:
        """令牌有效期（秒）。"""
        return self._ttl

    async def login(self, credentials: Credentials, *, trace_id: str = "-") -> LoginResult:
        """登录：成功给令牌 + 资料，失败抛 401 / 403 的 :class:`ApiError`。

        密码只在 :meth:`~nacho.api.services.user.protocols.PasswordHasher.verify` 里出现一次，
        不进日志。账号不存在与密码错是同一个 401：不告诉对方账号到底存不存在。
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

        token: str = self._tokens.issue(user.id, ttl=self._ttl)
        self._log().info("登录成功", account=account, user_id=user.id, trace_id=trace_id)
        return LoginResult(token=token, expires_in=int(self._ttl), user=profile_of(user))

    async def current_user(self, token: str, *, trace_id: str = "-") -> UserProfile:
        """按令牌取当前用户：令牌有问题由 ``tokens.parse`` 抛 401。

        令牌有效但账号没了 / 被停用了，同样按 401 处理（不是 500：是这份凭据不再可用）。
        """
        claims = self._tokens.parse(token)
        user = await self._store.get_by_id(claims.subject)
        if user is None or user.disabled:
            self._log().warning(
                "令牌对应的账号不可用", user_id=claims.subject, trace_id=trace_id
            )
            raise UnauthorizedError("账号不可用")
        return profile_of(user)

    def _log(self) -> BaseLogger:
        """业务日志实例（``api``）；用完即取，避免核心被替换后拿到旧的。"""
        return self._logger if self._logger is not None else api_logger(API_LOGGER_NAME)
