"""把接口层装成一个 FastAPI 应用。

:func:`create_app` 是唯一的装配入口，按顺序做四件事：

1. 建 ``FastAPI``（带 :func:`lifespan`：启动 / 停各记一条日志）；
2. 装 :class:`~nacho.api.common.middlewares.RequestLogMiddleware`（编号 + 访问日志）；
3. 装异常处理器（:func:`~nacho.api.common.errors.register_exception_handlers`）—— 出去的错误
   都是 :class:`~nacho.api.common.models.ErrorResponse` 那个形状；
4. 挂各业务模块的路由（现在只有鉴权那一份 ``<prefix>/auth``），并把各模块的服务挂到
   ``app.state`` 上给路由注入。

依赖全是可选的：不传 ``user_store`` 就用内存演示账号，不传 ``hasher`` / ``tokens`` 就走
默认实现，所以 **不接数据库也能直接跑起来**。

用法::

    from nacho.api import ApiOptions, attach_api_logging, create_app

    attach_api_logging(Path("logs/api.log"))          # 先挂载日志，再建应用
    app = create_app(ApiOptions.from_mapping(settings.api.model_dump()))

    # uvicorn nacho_api:app --port 8000
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from nacho import __version__
from nacho.core.logger import BaseLogger

from .common.errors import register_exception_handlers
from .common.middlewares import RequestLogMiddleware
from .logging import API_LOGGER_NAME, api_logger
from .api import auth_router
from .services.auth import AuthService
from .services.auth.protocols import TokenService
from .services.user.protocols import PasswordHasher, UserStore
from .services.user.security import Pbkdf2PasswordHasher
from .services.user.store import InMemoryUserStore
from .options import ApiOptions


def create_app(
    options: ApiOptions | None = None,
    *,
    user_store: UserStore | None = None,
    hasher: PasswordHasher | None = None,
    tokens: TokenService | None = None,
    title: str = "nacho",
    version: str = __version__,
    logger: BaseLogger | None = None,
) -> FastAPI:
    """装配一个 FastAPI 应用（接口层的对外门面）。

    :param options: 接口层选项（路由前缀、令牌有效期、访问日志开关、签名密钥）；
    :param user_store: 用户存储，默认内存演示账号；
    :param hasher: 密码哈希器，默认 PBKDF2；
    :param tokens: 令牌签发器，默认 HMAC 令牌（密钥取 ``options.secret``，空则随机）；
    :param title / version: OpenAPI 文档上的标题与版本；
    :param logger: 业务日志实例，默认 ``api`` 那个。
    """
    chosen: ApiOptions = options if options is not None else ApiOptions()
    log: BaseLogger = logger if logger is not None else api_logger(API_LOGGER_NAME)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """启动 / 停机各记一条：什么时候起的服务、令牌多久过期，日志里一眼能看到。"""
        log.info(
            "接口层启动",
            title=app.title,
            prefix=chosen.prefix,
            token_ttl=chosen.token_ttl,
            access_log=chosen.access_log,
        )
        try:
            yield
        finally:
            log.info("接口层停止")

    app = FastAPI(
        title=title,
        version=version,
        description="nacho 接口层：登录等对外接口（数据协议见各接口的 Schema）。",
        lifespan=lifespan,
    )
    # 中间件后加的先执行：访问日志在外层，能兜住里面抛出来的异常
    app.add_middleware(RequestLogMiddleware, options=chosen, logger=None)
    register_exception_handlers(app, logger=log)
    app.include_router(auth_router, prefix=chosen.prefix)

    # 依赖注入：服务在这一层建好挂上去（换存储 / 换算法只改这一处）
    chosen_hasher: PasswordHasher = hasher if hasher is not None else Pbkdf2PasswordHasher()
    app.state.auth_service = AuthService(
        user_store if user_store is not None else InMemoryUserStore.demo(chosen_hasher),
        hasher=chosen_hasher,
        tokens=tokens,
        ttl=chosen.token_ttl,
        secret=chosen.secret,
        logger=log,
    )
    return app
