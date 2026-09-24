"""应用入口：读配置 -> 初始化日志系统 -> 业务 -> 优雅停机。

运行::

    python app.py                     # 读 ./config.toml，不存在则按默认值启动
    python app.py -c path/to.toml     # 指定配置文件

配置不在这里：TOML 读取、取值校验、:class:`Settings` 都在同目录的 ``config.py``，
本文件只管流程——取一份设置，照它把日志系统拉起来，跑业务，停机收尾。

初始化沿用 ``nacho/core/logger/__init__.py`` 里「进程门面」的用法——业务代码通常
只用这三个::

    configure(...)                     # 建立（或复用）进程默认核心
    get_logger("app")                  # 业务模块取自己的实例（与核心共享队列）
    await manager.stop()               # 停机自动冲刷余量

Ctrl+C 走优雅停机：主协程被取消 -> 等调度器在飞的任务收尾 -> 冲刷日志 -> 关库连接，
安静退出不吐 traceback（收尾期间再按一次 Ctrl+C 才是强杀）。

接口层（``nacho.api``）随主程序由 uvicorn 起成 HTTP 服务，和 OneBot 同进程、同事件循环跑；
停机时由 :func:`_shutdown` 一并停（先让 uvicorn 优雅退出，再关业务）。监听地址在 ``[api]``
配置的 ``host`` / ``port``。

OneBot 反向 WS（``nacho.onebot``）同样随主程序起：监听 ``[onebot]`` 的 ``host`` / ``port``，
等 OneBot 实现连进来，事件交给 :func:`on_event`；日志单独落 ``logs/onebot.log``（同进程共用
日志核心，只是换个文件），停机时一并收。

依赖：``app.py`` 需要 ``nacho[api]`` + ``nacho[onebot]``（``fastapi`` / ``uvicorn`` /
``sqlmodel`` / ``aiosqlite`` / ``websockets``）。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from contextlib import suppress
from typing import cast
from urllib.parse import quote_plus

import uvicorn
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from config import (
    BASE_DIR,
    CONFIG_PATH,
    TEMPLATE_PATH,
    ConfigError,
    DatabaseSettings,
    Settings,
)
from nacho import __version__
from nacho.api import (
    ApiOptions,
    SqlSessionStore,
    SqlUserStore,
    attach_api_logging,
    create_app,
)
from nacho.core.cache import CacheOptions, cache
from nacho.core.logger import (
    BaseLogger,
    BaseLogProcessor,
    DatabaseLogProcessor,
    LocalFileLogProcessor,
    LogCore,
    LogLevel,
    configure,
    get_logger,
    manager,
)
from nacho.core.scheduler import (
    scheduler
)
from nacho.db import SqlLogStore
from nacho.workflow import SqlWorkflowStore
from nacho.workflow.runtime import load_published_workflows
from nacho.onebot import (
    ONEBOT_LOGGER_NAME,
    OneBotConnection,
    OneBotEvent,
    OneBotOptions,
    OneBotServer,
    SqlTokenRegistry,
    attach_onebot_logging,
    onebot_logger,
)

# --------------------------------------------------------------------------- 初始化
#: 日志出口单独连另一个库时自建的引擎（默认没有，用的就是业务那个）；停机时一并收
_log_engines: list[AsyncEngine] = []

#: 接口层 HTTP 服务（随主程序由 uvicorn 起）；引用放模块级，供 _shutdown 停机时取用
_api_server: uvicorn.Server | None = None
_api_task: asyncio.Task[None] | None = None
#: 应用共用的数据库引擎（令牌 / 用户 / 会话 + 默认的日志库出口都挂它上面）；停机要 dispose
_db_engine: AsyncEngine | None = None
#: OneBot 反向 WS 服务（同进程随主程序起）；停机时由 _shutdown 一并停
_onebot_server: OneBotServer | None = None


class _NoSignalServer(uvicorn.Server):
    """随主程序跑时信号由主程序统一管：覆盖掉 uvicorn 自带的 SIGINT 安装，免得抢了业务循环的停机。"""

    def install_signal_handlers(self) -> None:
        pass


async def setup(settings: Settings) -> tuple[LogCore, AsyncEngine]:
    """按设置初始化：建应用共用的数据库引擎 -> configure 建日志核心（顺带挂文件 / 数据库
    出口）-> start -> 拉缓存；返回 ``(核心, 业务引擎)``。

    引擎（只是连接池，第一次真正用到才连库）在这里一起建好：库出口得有引擎才能挂，建好
    一并交回调用方给业务存储复用（两者**共用同一个**）——``[logging.database]`` 单独配了
    连接项（要把日志放另一个库）时才另建一个。日志先起来，再拉缓存：缓存层的启动信息
    （用了哪个后端 / 有没有降级）要走日志核心。
    """
    app, log = settings.app, settings.logging  # 区域：[app] / [logging]
    file_log, db_log = log.file, log.database  # 子区域：[logging.file] / [logging.database]
    #(TODO)用models包装logger的配置,几个模块对齐一下
    global _db_engine
    url, db_target = _engine_url(settings.database)  # 顺带把 sqlite 的目录建出来
    engine: AsyncEngine = create_async_engine(url)
    _db_engine = engine  # 模块级也留一份：停机时 _shutdown 靠它关（它拿不到这个返回值）

    processors: list[BaseLogProcessor] = []
    if file_log.enabled:
        processors.append(
            LocalFileLogProcessor(
                file_log.path,
                name="file",
                buffer_size=file_log.buffer_size,
                flush_interval=file_log.flush_interval,
                max_bytes=file_log.max_bytes if file_log.max_bytes > 0 else None,
                backup_count=file_log.backup_count,
            )
        )
    log_target: str | None = None
    if db_log.enabled:
        # 日志出口写在 [logging.database] 的连接上；没单独配就是 [database] 那块库，直接
        # 复用业务引擎（同一个连接池），配了另一个库才再建一个。
        log_url, log_target = _engine_url(db_log.connection)
        log_engine: AsyncEngine = engine
        if db_log.connection != settings.database:
            log_engine = create_async_engine(log_url)
            _log_engines.append(log_engine)
        processors.append(
            DatabaseLogProcessor(
                SqlLogStore(log_engine),
                buffer_size=db_log.buffer_size,
                flush_interval=db_log.flush_interval,
            )
        )
    core = configure(
        app.name,
        level=LogLevel.DEBUG if app.debug else log.level,
        console=log.console,
        console_color=log.console_color,
        console_level=log.console_level,
        queue_maxsize=log.queue.maxsize,
        dispatch_batch_size=log.queue.dispatch_batch_size,
        dispatch_timeout=log.queue.dispatch_timeout,
        processors=processors,
    )
    _ = await core.start()
    # 挂了哪些出口说一声：日志本身进了哪里、有没有落库，翻日志时不用回来看配置
    core.info(
        "日志出口已就绪",
        console=log.console,
        file=str(file_log.path) if file_log.enabled else None,
        database=log_target,
    )
    core.info("数据库引擎就绪", driver=settings.database.driver, target=db_target)  # 口令不进日志
    # 缓存：默认（memory）就是本地内存，配了 redis 而连不上时按 fallback_to_memory 处理
    cache.configure(CacheOptions.from_mapping(settings.cache.model_dump()))
    await cache.start()
    return core, engine


def _engine_url(db_settings: DatabaseSettings) -> tuple[str, str]:
    """把连接项拼成**异步驱动**的 URL；返回 ``(url, 给人看的 target)``。

    sqlite 走 ``aiosqlite``、mariadb 走 ``aiomysql``：库出口与业务存储用的是同一套异步驱动
    （同一个引擎），不再各养一份驱动。``target`` 只是落日志用的摘录，**口令不进它**。
    """
    if db_settings.driver == "mariadb":
        target = f"{db_settings.host}:{db_settings.port}/{db_settings.database}"
        auth = f"{quote_plus(db_settings.user)}:{quote_plus(db_settings.password)}"
        return f"mysql+aiomysql://{auth}@{target}", target
    db_settings.path.parent.mkdir(parents=True, exist_ok=True)  # sqlite：目录要先在
    target = db_settings.path.as_posix()
    return f"sqlite+aiosqlite:///{target}", target


async def _prepare_stores(
    db: AsyncEngine, log: BaseLogger
) -> tuple[SqlTokenRegistry, SqlUserStore, SqlSessionStore, SqlWorkflowStore]:
    """建表 + 种演示账号（幂等）：各份落库存储都挂同一个 ``db``。

    放在**启动阶段**而不是等 lifespan：接口服务是 ``create_task`` 起的，启动阶段抛的异常没人
    await、会被静默吞掉，于是「没建成」只在第一个请求时才炸成 1146（表不存在），离真正的原因
    很远。放这里：失败就是启动失败，当场看得见。（lifespan 里那次留着兜底，幂等。）
    """
    tokens = SqlTokenRegistry(db)
    await tokens.ensure_schema()
    users = SqlUserStore(db)  # hasher 默认 PBKDF2，只有 seed_demo 用
    await users.ensure_schema()
    seeded = await users.seed_demo()  # 空表才种演示账号，已有数据不动
    sessions = SqlSessionStore(db)
    await sessions.ensure_schema()
    workflows = SqlWorkflowStore(db)
    await workflows.ensure_schema()
    log.info(
        "数据表就绪",
        tables=[
            "onebot_tokens",
            "users",
            "auth_sessions",
            "workflow_definitions",
            "workflow_versions",
        ],
        demo_accounts=seeded,  # 0 = 表里本来就有账号，一条没动
    )
    return tokens, users, sessions, workflows


# --------------------------------------------------------------------------- 业务
async def on_event(conn: OneBotConnection, event: OneBotEvent) -> None:
    """OneBot 事件钩子：业务接这里。

    现在只记一条日志，演示「收到了事件」；要发动作就这么写::

        await conn.call("send_msg", message_type="private", user_id=..., message="hi")
    """
    log = onebot_logger(ONEBOT_LOGGER_NAME)
    log.info("收到事件", post_type=event.post_type, self_id=event.self_id, remote=conn.remote)


# --------------------------------------------------------------------------- 收尾
async def _shutdown() -> None:
    """收尾：先停对外的两个服务（接口层 HTTP + OneBot 反向 WS）-> 等调度器跑完在飞的任务
    -> 冲刷日志余量 -> 关库连接。

    顺序不能反：任务里还会写日志，得等它们收尾了再冲刷、关库，收尾日志才不会丢。
    对外服务先优雅退出（信号由主程序统一管，不会抢 SIGINT），再关业务侧；
    Ctrl+C 取消的只是业务主协程，事件循环要等本函数跑完才停，所以这里能正常 await；
    收尾期间再按一次 Ctrl+C 会被强杀，那一下由 :func:`main` 兜住。
    """
    # 先停接口层 HTTP 服务：让 uvicorn 优雅退出（在飞的请求处理完再关）
    if _api_server is not None:
        _api_server.should_exit = True
    if _api_task is not None:
        with suppress(asyncio.CancelledError):
            await _api_task
    # 再停 OneBot 反向 WS：关监听并断开所有客户端
    if _onebot_server is not None:
        await _onebot_server.stop()
    await scheduler.stop()  # 等在飞的任务自然收尾（默认 5 秒，超时只记 warning，不强杀）
    await cache.stop()  # 再停缓存：任务收完了，后面不会再有业务来读写
    await manager.stop()  # 停机自动冲刷余量
    for engine in _log_engines:  # 余量落库之后再关（默认用的就是业务那个引擎，下面一并收）
        await engine.dispose()
    if _db_engine is not None:  # 数据库引擎（连接池）单独收
        await _db_engine.dispose()


# --------------------------------------------------------------------------- 入口
def _parse_args(argv: Sequence[str] | None) -> str:
    """解析命令行参数，返回配置文件路径。"""
    parser = argparse.ArgumentParser(description="nacho 应用入口")
    _ = parser.add_argument(
        "-c",
        "--config",
        default=str(CONFIG_PATH),
        help="配置文件路径（默认 ./config.toml，不存在则按默认值启动）",
    )
    return cast("str", parser.parse_args(argv).config)


async def _main(argv: Sequence[str] | None = None) -> None:
    """入口：装配 -> 起服务 -> 等停机。

    真正的服务（接口层 HTTP、OneBot 反向 WS）都在后台任务里跑，本函数只把**顺序**摆正：
    初始化哪一步失败都当场退出（``SystemExit(2)``），不留半截状态。
    """
    # 1) 配置：读不到就报错退出（-c 指定的文件，或默认的 ./config.toml）
    try:
        settings = Settings.load(_parse_args(argv))
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    # 2) 初始化：建库引擎 + 日志 / 缓存（库出口挂在同一个引擎上）-> 三份落库存储
    try:
        # setup 交回的引擎：库出口已经挂在它上面，业务存储接着用同一个（不再另建）；
        # 连不上库会在第一次用到时暴露，由下面这个 except 兜住
        core, db = await setup(settings)
        db_log = get_logger("db")  # 出口先挂、实例后取（实例创建即冻结落回配置）
        token_registry, user_store, session_store, workflow_store = await _prepare_stores(db, db_log)
    except (ConfigError, RuntimeError, SQLAlchemyError) as exc:  # 连不上库 / 建表被拒等
        print(f"[初始化错误] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    # 3) 起服务：两个都在后台任务里跑，主协程最后停在 OneBot 上等停机
    global _api_server, _api_task, _onebot_server
    try:
        core.info(
            "应用启动完成",
            version=__version__,
            config=str(settings.config_path) if settings.config_path else "默认值",
        )
        if settings.config_path is None:
            core.warning(f"没找到配置文件，按默认值启动；模板见 {TEMPLATE_PATH.name}")

        # OneBot 反向 WS：先建好对象，下面的接口层要用它（<prefix>/onebot/* 那组管理接口）
        attach_onebot_logging(BASE_DIR / "logs" / "onebot.log")
        _onebot_server = OneBotServer(
            OneBotOptions.from_mapping(settings.onebot.model_dump()),
            handler=on_event,
            tokens=token_registry,  # 令牌 -> 账号；一个端口接多个客户端，靠它认归属
        )

        # 接口层：挂日志 -> 建应用（注入同一个 db 上的两份存储 + OneBot）-> 起 uvicorn
        attach_api_logging(BASE_DIR / "logs" / "api.log")
        _db_engine = db
        _api_server = _NoSignalServer(
            uvicorn.Config(
                create_app(
                    ApiOptions.from_mapping(settings.api.model_dump()),
                    user_store=user_store,
                    session_store=session_store,
                    onebot=_onebot_server,
                    workflow_store=workflow_store,
                ),
                host=settings.api.host,
                port=settings.api.port,
                log_config=None,  # 不接管日志系统（接口层走 nacho.core.logger）
                access_log=False,  # 访问日志交给接口层自己的中间件
            )
        )
        _api_task = asyncio.create_task(_api_server.serve(), name="api")

        # 启动定时任务调度器：工作流的 time-trigger 节点靠它到点触发
        await scheduler.start()

        # 把所有已发布工作流载入调度器（时间触发的开始节点在此登记 cron）。
        # 发布接口只挪指针、不执行图，触发配置随启动统一生效。
        await load_published_workflows(workflow_store, scheduler)

        # 等停机：OneBot 起监听并一直跑，主协程就停在这一行（端口被占等当场报错退出）。
        # 服务都在后台任务里，不需要再手搓一个"业务循环"；Ctrl+C 取消本协程同样走到下面收尾。
        try:
            await _onebot_server.serve_forever()
        except OSError as exc:
            core.error(
                "OneBot 反向 WS 监听失败（端口被占？）",
                host=settings.onebot.host,
                port=settings.onebot.port,
                error=str(exc),
            )
            raise SystemExit(2) from exc
    except (KeyboardInterrupt, asyncio.CancelledError):  # Ctrl+C / 被外部取消：不算故障
        core.info("收到中断信号，开始停机")
    finally:
        await _shutdown()


def main(argv: Sequence[str] | None = None) -> None:
    """同步入口：``python app.py`` 走这里。"""
    try:
        asyncio.run(_main(argv))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass  # Ctrl+C（含收尾被打断）：能收的已在 _main 里收完，安静退即可


if __name__ == "__main__":
    main()
