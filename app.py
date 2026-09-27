"""应用入口：读配置 -> 初始化日志核心 -> 交给 nacho 装配业务 -> 优雅停机。

**本文件只做核心初始化**：解析命令行、读 ``config.toml``、按 ``[logging]`` 把日志核心建起来、
建好共用的数据库引擎；之后把这两样交给 :mod:`nacho.bootstrap`，由它装配其余模块（接口层
HTTP、OneBot 反向 WS、调度器、工作流），并在停机时先收业务（含冲刷日志余量、关库连接）。

之所以切这一刀：日志核心必须在**任何业务模块被 import 之前**按配置建好。nacho 里有模块级
``get_logger``（导入即执行）——谁先被 import，谁就顺手把进程默认核心按默认参数建出来，配置里
的颜色 / 级别就此定死、再也传不进去（``LogManager.configure`` 在「已存在核心」时只合并
processors）。所以本文件顶层**不 import 任何业务模块**，:mod:`nacho.bootstrap` 也是建好核心
之后才（在函数里）导入。

配置不在这里：TOML 读取、取值校验、:class:`Settings` 都在同目录的 ``config.py``，本文件只管
流程——取一份设置，照它把日志核心拉起来，把业务交给包内装配，停机收尾。

初始化沿用 ``nacho/core/logger/__init__.py`` 里「进程门面」的用法——这里只用这两个::

    configure(...)        # 建（或复用）进程默认核心
    await core.start()    # 起来之后业务模块取的实例才带得上这些出口

Ctrl+C 走优雅停机：主协程被取消 -> 业务侧收尾（停服务 / 等调度器 / 冲刷日志 / 关库）-> 关日志
库连接，安静退出不吐 traceback（收尾期间再按一次 Ctrl+C 才是强杀）。

接口层（``nacho.api``）随主程序由 uvicorn 起成 HTTP 服务，和 OneBot 同进程、同事件循环跑；
监听地址在 ``[api]`` 的 ``host`` / ``port``。OneBot 反向 WS（``nacho.onebot``）同样随主程序起，
监听 ``[onebot]`` 的 ``host`` / ``port``，日志单独落 ``logs/onebot.log``（同进程共用日志核心，
只是换个文件）。装配与停机的细节都在 :mod:`nacho.bootstrap`。

运行::

    python app.py                     # 读 ./config.toml，不存在则按默认值启动
    python app.py -c path/to.toml     # 指定配置文件

依赖：``app.py`` 需要 ``nacho[api]`` + ``nacho[onebot]``（``fastapi`` / ``uvicorn`` /
``sqlmodel`` / ``aiosqlite`` / ``websockets``）。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import cast
from urllib.parse import quote_plus

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
from nacho.core.logger import (
    BaseLogProcessor,
    DatabaseLogProcessor,
    LocalFileLogProcessor,
    LogCore,
    LogLevel,
    configure,
)
from nacho.db import SqlLogStore

# --------------------------------------------------------------------------- 初始化
#: 日志出口单独连另一个库时自建的引擎（默认没有，用的就是业务那个）；停机时一并收
_log_engines: list[AsyncEngine] = []


async def setup_logging(settings: Settings) -> tuple[LogCore, AsyncEngine]:
    """按 ``[logging]`` 建日志核心，并把共用的库引擎一并建好交回调用方。

    引擎（只是连接池，第一次真正用到才连库）在这里一起建：库出口得有引擎才能挂，建好一并
    交给 :mod:`nacho.bootstrap` 给业务存储复用（两者**共用同一个**）——``[logging.database]``
    单独配了连接项（要把日志放另一个库）时才另建一个。

    业务侧（建表、起服务、拉缓存）**不在这里**：那些模块一被 import 就可能写日志，必须等核心
    按配置建好之后再由装配模块引入。
    """
    app, log = settings.app, settings.logging  # 区域：[app] / [logging]
    file_log, db_log = log.file, log.database  # 子区域：[logging.file] / [logging.database]
    #(TODO)用models包装logger的配置,几个模块对齐一下
    url, db_target = _engine_url(settings.database)  # 顺带把 sqlite 的目录建出来
    engine: AsyncEngine = create_async_engine(url)

    # 片名前缀：配置留空就跟进程名，于是片名与进程名对得上（nacho-2026-09-28.log）
    file_prefix: str = file_log.prefix or app.name
    processors: list[BaseLogProcessor] = []
    if file_log.enabled:
        # 整进程**一份**文件出口，按天分片（不再「一个模块一个文件」，见 local.py 的模块文档）
        processors.append(
            LocalFileLogProcessor(
                file_log.dir,
                prefix=file_prefix,
                rotate_minutes=file_log.rotate_minutes,
                max_bytes=file_log.max_bytes if file_log.max_bytes > 0 else None,
                keep_days=file_log.keep_days,
                # 检索没给时间范围时往回找几天（页面上默认不带 start/end）：翻页与自动刷新
                # 都要各查一遍，没有这个下限就是每次都把保留期内的片全读一遍
                search_days=file_log.search_days,
                name="file",
                buffer_size=file_log.buffer_size,
                flush_interval=file_log.flush_interval,
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
        # 文件那份的落点是「目录 + 片名前缀」：具体写到哪一片看当天（<前缀>-<日期>.log）
        file_dir=str(file_log.dir) if file_log.enabled else None,
        file_prefix=file_prefix if file_log.enabled else None,
        database=log_target,
    )
    core.info("数据库引擎就绪", driver=settings.database.driver, target=db_target)  # 口令不进日志
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
    """入口：配置 -> 日志核心 -> 交给 nacho 装配 -> 等停机。

    真正的服务（接口层 HTTP、OneBot 反向 WS）都在后台任务里跑，本函数只把**顺序**摆正：
    初始化哪一步失败都当场退出（``SystemExit(2)``），不留半截状态。
    """
    # 1) 配置：读不到就报错退出（-c 指定的文件，或默认的 ./config.toml）
    try:
        settings = Settings.load(_parse_args(argv))
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    # 2) 核心初始化：日志核心 + 库引擎（此刻还没 import 任何业务模块）
    try:
        core, db = await setup_logging(settings)
    except (ConfigError, RuntimeError, SQLAlchemyError) as exc:
        print(f"[初始化错误] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    # 3) 装配：核心建好之后才导入业务装配模块（顺序的意义见模块文档）
    from nacho.bootstrap import run, serve_forever, shutdown

    try:
        core.info(
            "应用启动完成",
            version=__version__,
            config=str(settings.config_path) if settings.config_path else "默认值",
        )
        if settings.config_path is None:
            core.warning(f"没找到配置文件，按默认值启动；模板见 {TEMPLATE_PATH.name}")
        await run(
            engine=db,
            api=settings.api.model_dump(),
            api_host=settings.api.host,
            api_port=settings.api.port,
            onebot=settings.onebot.model_dump(),
            cache_config=settings.cache.model_dump(),
        )
    except (ConfigError, RuntimeError, SQLAlchemyError) as exc:  # 连不上库 / 建表被拒等
        print(f"[初始化错误] {exc}", file=sys.stderr)
        await shutdown()  # 可能起了半截（服务已起、后面失败）：收干净再退
        raise SystemExit(2) from exc

    # 4) 等停机：主协程停在 OneBot 上（端口被占等当场报错退出）。
    #    服务都在后台任务里，不需要再手搓一个"业务循环"；Ctrl+C 取消本协程同样走到下面收尾。
    try:
        await serve_forever()
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
        await shutdown()
        for engine in _log_engines:  # 业务侧收完、日志余量冲刷完，再关日志库连接
            await engine.dispose()


def main(argv: Sequence[str] | None = None) -> None:
    """同步入口：``python app.py`` 走这里。"""
    try:
        asyncio.run(_main(argv))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass  # Ctrl+C（含收尾被打断）：能收的已在 _main 里收完，安静退即可


if __name__ == "__main__":
    main()
