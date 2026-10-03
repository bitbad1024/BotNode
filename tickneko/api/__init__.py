"""接口层（``tickneko.api``）：对外协议、请求校验、登录与日志接入。

分层约定在 :mod:`tickneko` 里写死了：这里只管**对外怎么说**，业务怎么存、密码怎么算由传进来
的实现决定，接口层不依赖数据库，也不依赖具体算法。

目录按「分层 + 通用件」划分::

    app.py          create_app()：把下面这些装成一个 FastAPI 应用
    options.py      接口层选项（对应配置里的 [api]），靠普通映射传进来
    logging.py      日志接入点：api / api.access 两个名字
    common/         跨业务的通用件（不属于任何业务）
        models.py       响应壳：ApiResponse / ErrorResponse / ErrorPayload / ErrorDetail
        errors/         错误出口：错误码 / 异常类型 / 翻响应的处理器
        middlewares/    请求中间件：编号 + 访问日志
        dependencies.py 路由通用依赖：取这次请求的编号
        encoding.py     base64 编解码（哈希串与令牌共用）
    api/            入口层：认识 FastAPI（路由 / 依赖 / 请求响应）
        auth/           鉴权接口：登录、当前用户
        onebot/         OneBot 管理接口：在线客户端列表、踢人、令牌签发与吊销（兼容面）
        bots/           机器人管理接口：跨平台增 / 启停 / 删（凭证行走 bot_credentials）
    services/       业务层：不认识 FastAPI
        user/           用户：形状 / 校验规则 / 协议 / 默认实现
        auth/           鉴权：令牌 / 凭据换令牌的服务

依赖方向**单向**：入口层 ``api.auth`` 用业务层 ``services.*``，业务层用不到入口层。
加一个业务：业务逻辑放 ``services/``，HTTP 入口放 ``api/``，通用件不用动。

最小用法（登录功能开箱可用，默认走内存演示账号）::

    from tickneko.api import ApiOptions, create_app

    app = create_app(ApiOptions.from_mapping({"prefix": "/api", "token_ttl": 3600}))

日志不用在这儿接：整进程**一份**文件出口（按天分片）由入口建核心时挂好，
接口层的日志照进那一份（名字 ``tickneko.api`` / ``tickneko.api.access``），见 :mod:`tickneko.api.logging`。

    # uvicorn 起服务：uvicorn tickneko_api:app --port 18080
    #   POST /api/auth/login   {"account": "admin", "password": "tickneko-admin"}
    #   GET  /api/auth/me      Authorization: Bearer <上一步返回的 token>

接真实环境时把各模块协议的实现传进 :func:`create_app` 即可（``user_store`` / ``hasher`` /
``session_store`` / ``onebot`` / ``bots`` / ``workflow_store`` / ``workflow_triggers`` / ``avatar_store``），
路由与这里一行都不用改。

依赖 ``fastapi``（``pip install "tickneko[api]"``）；请求日志走 ``tickneko.core.logger``，
不额外引日志库。
"""
from __future__ import annotations

from .app import create_app
from .common.errors import (
    AccountAlreadyExistsError,
    AccountDisabledError,
    ApiError,
    AvatarTooLargeError,
    AvatarTypeUnsupportedError,
    ErrorCode,
    HttpStatus,
    InternalError,
    InvalidCredentialsError,
    PasswordMismatchError,
    TokenExpiredError,
    TokenInvalidError,
    UnauthorizedError,
    ValidationError,
    register_exception_handlers,
)
from .common.middlewares import RequestLogMiddleware
from .common.models import ApiResponse, ErrorDetail, ErrorPayload, ErrorResponse
from .common.headers import SESSION_EXPIRES_HEADER
from .logging import (
    ACCESS_LOGGER_NAME,
    API_LOGGER_NAME,
    TRACE_ID_HEADER,
    api_logger,
)
from .api import auth_router, log_router, onebot_router, profile_router
from .api.auth.dependencies import SESSION_COOKIE
from .api.auth.requests import LoginRequest, RegisterRequest
from .api.auth.responses import (
    LoginData,
    RevokeAllData,
    RevokeSessionData,
    SessionData,
)
from .api.log import LogData, LogPage
from .api.onebot import (
    ClientData,
    IssuedTokenData,
    IssueTokenRequest,
    KickData,
    RevokeData,
    SetTokenEnabledRequest,
    TokenData,
)
from .services.auth import AuthService, Credentials, CurrentUser, LoginResult
from .services.profile import (
    DEFAULT_AVATAR_MAX_BYTES,
    AvatarInfo,
    AvatarStore,
    FileAvatarStore,
    ProfileService,
    ProfileView,
)
from .services.session import (
    ClientInfo,
    SessionRecord,
    SessionService,
    SqlSessionStore,
    TokenHashCollisionError,
    describe_client,
)
from .services.user import (
    ACCOUNT_MAX_LENGTH,
    ACCOUNT_MIN_LENGTH,
    NICKNAME_MAX_LENGTH,
    NICKNAME_MIN_LENGTH,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    Account,
    Nickname,
    Password,
    PasswordHasher,
    Pbkdf2PasswordHasher,
    SqlUserStore,
    UserProfile,
    UserRecord,
    UserStore,
    profile_of,
)
from .api.profile import ProfileData, UpdateProfileRequest
from .options import DEFAULT_AVATAR_DIR, DEFAULT_PREFIX, DEFAULT_TOKEN_TTL, ApiOptions

__all__ = [
    # 装配
    "create_app",
    "ApiOptions",
    "DEFAULT_PREFIX",
    "DEFAULT_TOKEN_TTL",
    "auth_router",
    "onebot_router",
    "log_router",
    # OneBot 管理接口
    "IssueTokenRequest",
    "SetTokenEnabledRequest",
    "ClientData",
    "TokenData",
    "IssuedTokenData",
    "KickData",
    "RevokeData",
    # 运行日志接口
    "LogData",
    "LogPage",
    # 响应壳
    "ApiResponse",
    "ErrorResponse",
    "ErrorPayload",
    "ErrorDetail",
    # 错误出口
    "ApiError",
    "ErrorCode",
    "HttpStatus",
    "ValidationError",
    "InvalidCredentialsError",
    "PasswordMismatchError",
    "AccountDisabledError",
    "AccountAlreadyExistsError",
    "AvatarTooLargeError",
    "AvatarTypeUnsupportedError",
    "UnauthorizedError",
    "TokenInvalidError",
    "TokenExpiredError",
    "InternalError",
    "register_exception_handlers",
    # 用户模块
    "UserRecord",
    "UserProfile",
    "profile_of",
    "UserStore",
    "PasswordHasher",
    "SqlUserStore",
    "Pbkdf2PasswordHasher",
    "Account",
    "Password",
    "Nickname",
    "ACCOUNT_MIN_LENGTH",
    "ACCOUNT_MAX_LENGTH",
    "PASSWORD_MIN_LENGTH",
    "PASSWORD_MAX_LENGTH",
    "NICKNAME_MIN_LENGTH",
    "NICKNAME_MAX_LENGTH",
    # 鉴权模块
    "LoginRequest",
    "RegisterRequest",
    "LoginData",
    "SESSION_COOKIE",
    "SessionData",
    "RevokeSessionData",
    "RevokeAllData",
    "AuthService",
    "Credentials",
    "CurrentUser",
    "LoginResult",
    # 会话模块
    "SessionService",
    "SessionRecord",
    "ClientInfo",
    "SqlSessionStore",
    "TokenHashCollisionError",
    "describe_client",
    # 个人设置模块
    "ProfileService",
    "ProfileView",
    "AvatarInfo",
    "AvatarStore",
    "FileAvatarStore",
    "DEFAULT_AVATAR_MAX_BYTES",
    "DEFAULT_AVATAR_DIR",
    "UpdateProfileRequest",
    "ProfileData",
    "profile_router",
    # 日志接入点
    "api_logger",
    "RequestLogMiddleware",
    "API_LOGGER_NAME",
    "ACCESS_LOGGER_NAME",
    "TRACE_ID_HEADER",
    "SESSION_EXPIRES_HEADER",
]
