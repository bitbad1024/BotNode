"""应用入口：读配置 -> 建日志核心与库引擎 -> 交给 :mod:`botnode.bootstrap` 装配 -> 优雅停机。

**只做核心初始化**：解析命令行、读 ``data/config.toml``、按 ``[logging]`` 建日志核心、建共用的
数据库引擎（建好立即探一次，连不上当场报错）。业务模块一律不在这里 import —— 日志核心必须
先于任何业务模块按配置建好（它们有模块级的 ``default_core().child(...)``，导入即执行会把
进程默认核心按默认参数定死），所以 :mod:`botnode.bootstrap` 也是核心就绪之后才在函数里导入。
配置本身的读取与校验在 ``config.py``，本文件只管流程。

运行::

    python app.py                     # 读 data/config.toml，不存在则按默认值启动
    python app.py -c path/to.toml     # 指定配置文件

启动顺序、数据库探测、Ctrl+C 收尾、监听地址与依赖的完整说明见 ``docs/app/app.md``。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import cast
from urllib.parse import quote_plus

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from config import (
    BASE_DIR,
    CONFIG_PATH,
    KEY_FILE,
    TEMPLATE_PATH,
    ConfigError,
    DatabaseSettings,
    Settings,
    load_or_create_secret_key,
)
from botnode import __version__
from botnode.core.logger import (
    BaseLogProcessor,
    DatabaseLogProcessor,
    LocalFileLogProcessor,
    LogCore,
    LogLevel,
    configure,
)
from botnode.db import SqlLogStore

# --------------------------------------------------------------------------- 初始化
#: 日志出口单独连另一个库时自建的引擎（默认没有，用的就是业务那个）；停机时一并收
_log_engines: list[AsyncEngine] = []


async def setup_logging(settings: Settings) -> tuple[LogCore, AsyncEngine]:
    """按 ``[logging]`` 建日志核心，并把共用的库引擎一并建好交回调用方。

    引擎（只是连接池，第一次真正用到才连库）在这里一起建：库出口得有引擎才能挂，建好一并
    交给 :mod:`botnode.bootstrap` 给业务存储复用（两者**共用同一个**）——``[logging.database]``
    单独配了连接项（要把日志放另一个库）时才另建一个。

    业务侧（建表、起服务、拉缓存）**不在这里**：那些模块一被 import 就可能写日志，必须等核心
    按配置建好之后再由装配模块引入。
    """
    app, log = settings.app, settings.logging  # 区域：[app] / [logging]
    file_log, db_log = log.file, log.database  # 子区域：[logging.file] / [logging.database]
    #(TODO)用models包装logger的配置,几个模块对齐一下
    url, db_target = _engine_url(settings.database)  # 顺带把 sqlite 的目录建出来
    engine: AsyncEngine = create_async_engine(
        url,
        # mariadb 连不上时别干等：默认驱动会一直重试到系统级超时（实测十几秒才报错），
        # 给个明确的 connect_timeout 让它尽快失败（sqlite 不受此参数影响）
        connect_args={"connect_timeout": 5} if settings.database.driver == "mariadb" else {},
    )
    await _probe_database(engine, db_target, settings.database.driver)

    # 片名前缀：配置留空就跟进程名，于是片名与进程名对得上（botnode-2026-09-28.log）
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
            log_engine = create_async_engine(
                log_url,
                connect_args=(
                    {"connect_timeout": 5}
                    if db_log.connection.driver == "mariadb"
                    else {}
                ),
            )
            # 日志库单独配了另一个库：它连不上也该当场报错，别等到 core.start() 里被吞成
            # 一行乱码兜底日志（见 base.py 的「处理机启动失败，已跳过」分支）
            await _probe_database(log_engine, log_target, db_log.connection.driver)
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


async def _probe_database(engine: AsyncEngine, target: str, driver: str) -> None:
    """启动阶段主动连一次库：**连不上当场报错**，别等第一个请求 / 建表时才炸。

    SQLAlchemy 引擎是惰性的（``create_async_engine`` 只建连接池，第一次真正用到才连库），
    不主动探一下，连库失败会推迟到 :meth:`core.start` 里日志出口建表时——那里异常被日志
    核心吞掉只剩一行乱码 traceback，用户只看到"没提示"。这里当场 ``SELECT 1``，连不上抛
    带 target（host:port/db）的异常并提示检查配置，由入口统一打成 ``[初始化错误]`` 干净退出。
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise RuntimeError(
            f"连不上数据库（{driver} @ {target}）：{exc}"
            "（请检查 config.toml 的 [database] 配置，以及数据库服务是否在监听）"
        ) from exc


# --------------------------------------------------------------------------- 入口
def _parse_args(argv: Sequence[str] | None) -> str:
    """解析命令行参数，返回配置文件路径。"""
    parser = argparse.ArgumentParser(description="BotNode 应用入口")
    _ = parser.add_argument(
        "-c",
        "--config",
        default=str(CONFIG_PATH),
        help="配置文件路径（默认 data/config.toml，不存在则按默认值启动）",
    )
    return cast("str", parser.parse_args(argv).config)


async def _main(argv: Sequence[str] | None = None) -> None:
    """入口：配置 -> 日志核心 -> 交给 botnode 装配 -> 等停机。

    真正的服务（接口层 HTTP、OneBot 反向 WS）都在后台任务里跑，本函数只把**顺序**摆正：
    初始化哪一步失败都当场退出（``SystemExit(2)``），不留半截状态。
    """
    # 1) 配置：读不到就报错退出（-c 指定的文件，或默认的 data/config.toml）
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
    from botnode.bootstrap import run, serve_forever, shutdown

    try:
        core.info(
            "应用启动完成",
            version=__version__,
            config=str(settings.config_path) if settings.config_path else "默认值",
        )
        if settings.config_path is None:
            core.warning(
                f"没找到配置文件，按默认值启动；模板见 {TEMPLATE_PATH.name}，"
                f"复制成 {CONFIG_PATH.relative_to(BASE_DIR).as_posix()} 即生效"
            )
        # Kook 凭证的加密密钥：配置留空就自动生成/读取 data/secret_key —— 落盘而不是每次随机，
        # 否则重启后已落库的 Bot Token 密文会全部解不开（见 config.load_or_create_secret_key）
        kook_config: dict[str, object] = settings.kook.model_dump()
        if not settings.kook.secret_key.strip():
            kook_config["secret_key"] = load_or_create_secret_key()
            core.info("Kook 密钥未配置，已自动生成/读取", path=str(KEY_FILE))
        await run(
            engine=db,
            api=settings.api.model_dump(),
            api_host=settings.api.host,
            api_port=settings.api.port,
            onebot=settings.onebot.model_dump(),
            kook=kook_config,
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
    # Windows 下 stderr 默认按本地代码页（GBK）写，而终端/捕获端按 UTF-8 读——中文全变乱码。
    # 启动前把 stderr 统一成 UTF-8（botnode 自己的 console 出口就是 UTF-8），
    # 这样启动失败时的「打印到 stderr」和日志核心的兜底（lastResort）都不再是乱码。
    try:
        if sys.stderr is not None:
            reconfigure = getattr(sys.stderr, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(encoding="utf-8")
    except (OSError, ValueError):  # 非文本流（重定向成二进制等）别硬改
        pass
    try:
        asyncio.run(_main(argv))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass  # Ctrl+C（含收尾被打断）：能收的已在 _main 里收完，安静退即可


if __name__ == "__main__":
    main()
