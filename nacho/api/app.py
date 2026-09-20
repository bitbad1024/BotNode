"""把接口层装成一个 FastAPI 应用。

:func:`create_app` 是唯一的装配入口，按顺序做四件事：

1. 建 ``FastAPI``（带 :func:`lifespan`：启动 / 停各记一条日志）；
2. 装 :class:`~nacho.api.common.middlewares.RequestLogMiddleware`（编号 + 访问日志）；
3. 装异常处理器（:func:`~nacho.api.common.errors.register_exception_handlers`）—— 出去的错误
   都是 :class:`~nacho.api.common.models.ErrorResponse` 那个形状；
4. 挂各业务模块的路由（鉴权 ``<prefix>/auth``、OneBot 管理 ``<prefix>/onebot``），并把各模块
   的服务挂到 ``app.state`` 上给路由注入。

依赖全是可选的：不传 ``user_store`` 就用内存演示账号，不传 ``hasher`` / ``tokens`` 就走
默认实现；传了 ``db``（``AsyncEngine``）就改用落库版 :class:`~nacho.api.services.user.store_sql.SqlUserStore`
（SQLModel 查 ``users`` 表，启动时建表），所以 **不接数据库也能直接跑起来**。

``onebot`` 同样是可选的：主程序把 :class:`nacho.onebot.OneBotServer` 传进来，
``<prefix>/onebot/*`` 那组管理接口才有用；没传就回 503（「没接入」和「出错了」分开报）。
这里按 :class:`~nacho.api.api.onebot.protocols.OneBotLike` 协议接收，所以 **接口层不 import
``nacho.onebot``** —— 只装 ``nacho[api]`` 也能跑起来（具体说明见那个模块）。

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

from sqlalchemy.ext.asyncio import AsyncEngine

from nacho import __version__
from nacho.core.logger import BaseLogger

from .common.errors import register_exception_handlers
from .common.middlewares import RequestLogMiddleware
from .logging import API_LOGGER_NAME, api_logger
from .api import auth_router, onebot_router
from .api.onebot.protocols import OneBotLike
from .services.auth import AuthService
from .services.session import InMemorySessionStore, SessionService, SqlSessionStore
from .services.session.protocols import SessionStore
from .services.user.protocols import PasswordHasher, UserStore
from .services.user.security import Pbkdf2PasswordHasher
from .services.user.store import InMemoryUserStore
from .services.user.store_sql import SqlUserStore
from .options import ApiOptions


def create_app(
    options: ApiOptions | None = None,
    *,
    user_store: UserStore | None = None,
    hasher: PasswordHasher | None = None,
    db: AsyncEngine | None = None,
    session_store: SessionStore | None = None,
    onebot: OneBotLike | None = None,
    title: str = "nacho",
    version: str = __version__,
    logger: BaseLogger | None = None,
) -> FastAPI:
    """装配一个 FastAPI 应用（接口层的对外门面）。

    :param options: 接口层选项（路由前缀、访问令牌滑动有效期、长期令牌有效期、访问日志）；
    :param user_store: 用户存储，默认内存演示账号；传了就直接用；
    :param hasher: 密码哈希器，默认 PBKDF2；
    :param session_store: 会话存储，默认按 ``db`` 决定（有库就落 ``auth_sessions``，
        没库就用内存）——登录令牌是**有状态**的，会话信息得有地方放；
    :param db: 异步引擎（``AsyncEngine``）；传了就用落库版
        :class:`~nacho.api.services.user.store_sql.SqlUserStore`（SQLModel 查 ``users`` 表）
        与 :class:`~nacho.api.services.session.store_sql.SqlSessionStore`（``auth_sessions`` 表），
        没传（也没传 ``user_store``）就退回内存演示账号——**不接数据库也能直接跑起来**；
    :param onebot: OneBot 服务端（``nacho.onebot.OneBotServer``）；传了 ``<prefix>/onebot/*``
        那组管理接口（在线列表 / 踢人 / 令牌增删）才可用，没传时这些接口回 503；
    :param title / version: OpenAPI 文档上的标题与版本；
    :param logger: 业务日志实例，默认 ``api`` 那个。
    """
    chosen: ApiOptions = options if options is not None else ApiOptions()
    log: BaseLogger = logger if logger is not None else api_logger(API_LOGGER_NAME)
    chosen_hasher: PasswordHasher = hasher if hasher is not None else Pbkdf2PasswordHasher()

    # 选用户存储：显式传的优先 -> 给了 db 就用落库版 -> 否则内存演示（保证开箱即跑）
    store: UserStore
    if user_store is not None:
        store = user_store
    elif db is not None:
        store = SqlUserStore(db, hasher=chosen_hasher)
    else:
        store = InMemoryUserStore.demo(chosen_hasher)

    # 选会话存储：显式传的优先 -> 给了 db 就落 auth_sessions 表 -> 否则内存（开箱即跑）
    sessions_store: SessionStore
    if session_store is not None:
        sessions_store = session_store
    elif db is not None:
        sessions_store = SqlSessionStore(db)
    else:
        sessions_store = InMemorySessionStore()
    session_service = SessionService(
        sessions_store,
        access_ttl=chosen.token_ttl,
        remember_ttl=chosen.remember_ttl,
        logger=log,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """启动 / 停机各记一条；落库存储在这里建表 + 空表种演示账号。"""
        log.info(
            "接口层启动",
            title=app.title,
            prefix=chosen.prefix,
            session_ttl=chosen.token_ttl,
            remember_ttl=chosen.remember_ttl,
            access_log=chosen.access_log,
        )
        # 落到库的存储：启动时先确保表在、空表种演示账号（幂等）；store 是闭包里的局部变量。
        # 主程序（根目录 app.py）在**起服务之前**会先建一次（失败即启动失败，看得见）；这里
        # 这次是幂等的兜底 —— 直接 ``create_app`` 起来的场合（测试 / 示例）靠它。
        if isinstance(store, SqlUserStore):
            await store.ensure_schema()
            await store.seed_demo()
        if isinstance(sessions_store, SqlSessionStore):
            await sessions_store.ensure_schema()
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
    app.include_router(onebot_router, prefix=chosen.prefix)

    # 依赖注入：服务在这一层建好挂上去（换存储 / 换算法只改这一处）
    app.state.auth_service = AuthService(
        store,
        hasher=chosen_hasher,
        sessions=session_service,
        logger=log,
    )
    # 选项也挂上去：信任代理、长期 Cookie 的有效期这些路由要用
    app.state.api_options = chosen
    # OneBot 服务端（可空）：没传时 <prefix>/onebot/* 回 503，见 onebot/dependencies.py
    app.state.onebot_server = onebot
    return app
