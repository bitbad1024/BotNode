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

Ctrl+C 走优雅停机：业务协程被取消 -> 等调度器在飞的任务收尾 -> 冲刷日志 -> 关库连接，
安静退出不吐 traceback（收尾期间再按一次 Ctrl+C 才是强杀）。

接口层（``nacho.api``）随主程序由 uvicorn 起成 HTTP 服务，和业务循环同进程、同事件循环跑；
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
from nacho.db import MariadbAdapter, SqliteAdapter
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
#: setup 建立的数据库适配器，停机后由 _main 统一关连接
_db_adapters: list[SqliteAdapter | MariadbAdapter] = []

#: 接口层 HTTP 服务（随主程序由 uvicorn 起）；引用放模块级，供 _shutdown 停机时取用
_api_server: uvicorn.Server | None = None
_api_task: asyncio.Task[None] | None = None
#: 接口层用的异步引擎（SQLModel 查 users 表）；停机时要 dispose
_api_engine: AsyncEngine | None = None
#: OneBot 反向 WS 服务（同进程随主程序起）；停机时由 _shutdown 一并停
_onebot_server: OneBotServer | None = None


class _NoSignalServer(uvicorn.Server):
    """随主程序跑时信号由主程序统一管：覆盖掉 uvicorn 自带的 SIGINT 安装，免得抢了业务循环的停机。"""

    def install_signal_handlers(self) -> None:
        pass


async def setup(settings: Settings) -> LogCore:
    """按设置初始化进程默认日志系统：configure 建核心（顺带挂文件 / 数据库出口）-> start。

    日志先起来，再拉缓存 —— 缓存层的启动信息（用了哪个后端 / 有没有降级）要走日志核心。
    """
    app, log = settings.app, settings.logging  # 区域：[app] / [logging]
    file_log, db_log = log.file, log.database  # 子区域：[logging.file] / [logging.database]
    #(TODO)用models包装logger的配置,几个模块对齐一下
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
    if db_log.enabled:
        conn = db_log.connection  # 日志出口最终用的那份连接
        if conn.driver == "mariadb":
            adapter = MariadbAdapter(
                host=conn.host,
                port=conn.port,
                user=conn.user,
                password=conn.password,
                database=conn.database,
                table=db_log.table,
            )
            paramstyle = "format"  # PyMySQL 的 %s 占位
        else:
            adapter = SqliteAdapter(conn.path, table=db_log.table)
            paramstyle = "qmark"  # sqlite 的 ? 占位
        _db_adapters.append(adapter)
        processors.append(
            DatabaseLogProcessor(
                adapter,
                table=db_log.table,
                paramstyle=paramstyle,
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
    # 缓存：默认（memory）就是本地内存，配了 redis 而连不上时按 fallback_to_memory 处理
    cache.configure(CacheOptions.from_mapping(settings.cache.model_dump()))
    await cache.start()
    return core


# --------------------------------------------------------------------------- 业务
async def run() -> None:
    """业务入口：初始化完成之后真正干活的地方（实现接这里）。"""
    log = get_logger("app")
    log.debug("这条 DEBUG 默认被级别挡住")
    log.info("业务开始", version=__version__)
    #log.warning("业务占位：把实现接进 run() 即可")
    #await scheduler.start()
    #scheduler.add("*/1 * * * * *", lambda:print("每秒一次"),  name="巡检")
    #scheduler.add("*/5 * * * * *", lambda:print("每五秒一次"), name="巡检")
    #step = 0
    while True:
    #    log.info(f"{step}写入")
    #    step += 1
        await asyncio.sleep(1)


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
    for adapter in _db_adapters:  # 余量落库之后再关连接
        adapter.close()
    if _api_engine is not None:  # 接口层的引擎（连接池）单独收
        await _api_engine.dispose()


# --------------------------------------------------------------------------- 入口
def _build_user_engine(db: DatabaseSettings) -> AsyncEngine:
    """按配置建接口层的异步引擎（接口层用 SQLModel 查 ``users`` 表，不手写 SQL）。

    复用 ``[database]`` 公共节的连接信息；异步驱动与数据库层那份同步驱动不同：
    sqlite 走 ``aiosqlite``，mariadb 走 ``aiomysql``（数据库层日志落库用的是
    sqlite3 / PyMySQL，两套驱动各管一段，互不干扰）。
    """
    if db.driver == "mariadb":
        url = (
            f"mysql+aiomysql://{quote_plus(db.user)}:{quote_plus(db.password)}"
            f"@{db.host}:{db.port}/{db.database}"
        )
    else:
        db.path.parent.mkdir(parents=True, exist_ok=True)  # sqlite：目录要先在
        url = f"sqlite+aiosqlite:///{db.path.as_posix()}"
    return create_async_engine(url)


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
    try:
        settings = Settings.load(_parse_args(argv))
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    try:
        core = await setup(settings)
        user_engine = _build_user_engine(settings.database)  # RuntimeError = 数据库连不上
        # 令牌注册表：和 users 表同一个库；「连进来的客户端属于哪个账号」由它定
        token_registry = SqlTokenRegistry(user_engine)
        await token_registry.ensure_schema()
        # 接口层那两张表（users / auth_sessions）**也在这里先建**，不等 lifespan：
        # 接口服务是 ``create_task`` 起的，启动阶段抛的异常没人 await、会被静默吞掉，
        # 于是「没建成」只在第一个请求时才炸成 1146（表不存在），离真正的原因很远。
        # 建在启动阶段：失败就是启动失败，当场看得见。（lifespan 里那次留着做兜底，幂等。）
        user_store = SqlUserStore(user_engine)  # hasher 默认 PBKDF2，只有 seed_demo 用
        await user_store.ensure_schema()
        await user_store.seed_demo()  # 空表才种演示账号，已有数据不动
        session_store = SqlSessionStore(user_engine)
        await session_store.ensure_schema()
    except (ConfigError, RuntimeError, SQLAlchemyError) as exc:  # 连不上库 / 建表被拒等
        print(f"[初始化错误] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    global _api_server, _api_task, _api_engine, _onebot_server
    try:
        core.info(
            "应用启动完成",
            version=__version__,
            config=str(settings.config_path) if settings.config_path else "默认值",
        )
        if settings.config_path is None:
            core.warning(f"没找到配置文件，按默认值启动；模板见 {TEMPLATE_PATH.name}")

        # OneBot 反向 WS：同进程同事件循环，共用日志核心（只是换个文件落盘），随主程序一起起。
        # 服务要**先建好**再交给接口层：<prefix>/onebot/* 那组管理接口（在线列表 / 踢人 / 令牌）用它
        attach_onebot_logging(BASE_DIR / "logs" / "onebot.log")
        _onebot_server = OneBotServer(
            OneBotOptions.from_mapping(settings.onebot.model_dump()),
            handler=on_event,
            tokens=token_registry,  # 令牌 -> 账号；一个端口接多个客户端，靠它认归属
        )

        # 接口层：先挂日志（落 logs/api.log），再建应用（注入真实数据库引擎与 OneBot 服务）
        attach_api_logging(BASE_DIR / "logs" / "api.log")
        _api_engine = user_engine
        api_app = create_app(
            ApiOptions.from_mapping(settings.api.model_dump()),
            # 直接用上面建好表、种好账号的那两份存储（不再传 db 让它另起一份）
            user_store=user_store,
            session_store=session_store,
            onebot=_onebot_server,
        )
        _api_server = _NoSignalServer(
            uvicorn.Config(
                api_app,
                host=settings.api.host,
                port=settings.api.port,
                log_config=None,  # 不接管日志系统（接口层走 nacho.core.logger）
                access_log=False,  # 访问日志交给接口层自己的中间件
            )
        )
        _api_task = asyncio.create_task(_api_server.serve(), name="api")

        if not await token_registry.items():  # 一枚令牌都没有：客户端连上来会被 401 拒
            hint = f"{settings.api.prefix}/onebot/tokens"
            core.warning(f"还没有任何 OneBot 令牌，客户端连不上；签一个：POST {hint}")

        try:
            await _onebot_server.start()
        except OSError as exc:  # 端口被占等：报清楚，别带着半截状态往下跑
            core.error(
                "OneBot 反向 WS 监听失败（端口被占？）",
                host=settings.onebot.host,
                port=settings.onebot.port,
                error=str(exc),
            )
            raise SystemExit(2) from exc

        await run()
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
