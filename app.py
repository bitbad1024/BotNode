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
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import cast

from config import CONFIG_PATH, TEMPLATE_PATH, ConfigError, Settings
from nacho import __version__
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

# --------------------------------------------------------------------------- 初始化
#: setup 建立的数据库适配器，停机后由 _main 统一关连接
_db_adapters: list[SqliteAdapter | MariadbAdapter] = []


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
    log.warning("业务占位：把实现接进 run() 即可")
    await scheduler.start()
    scheduler.add("*/1 * * * * *", lambda:print("每秒一次"),  name="巡检")
    scheduler.add("*/5 * * * * *", lambda:print("每五秒一次"), name="巡检")
    step = 0
    while True:
        log.info(f"{step}写入")
        step += 1
        await asyncio.sleep(1)


# --------------------------------------------------------------------------- 收尾
async def _shutdown() -> None:
    """收尾：等调度器跑完在飞的任务 -> 冲刷日志余量 -> 关库连接。

    顺序不能反：任务里还会写日志，得等它们收尾了再冲刷、关库，收尾日志才不会丢。
    Ctrl+C 取消的只是业务主协程，事件循环要等本函数跑完才停，所以这里能正常 await；
    收尾期间再按一次 Ctrl+C 会被强杀，那一下由 :func:`main` 兜住。
    """
    await scheduler.stop()  # 等在飞的任务自然收尾（默认 5 秒，超时只记 warning，不强杀）
    await cache.stop()  # 再停缓存：任务收完了，后面不会再有业务来读写
    await manager.stop()  # 停机自动冲刷余量
    for adapter in _db_adapters:  # 余量落库之后再关连接
        adapter.close()


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
    try:
        settings = Settings.load(_parse_args(argv))
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    try:
        core = await setup(settings)
    except (ConfigError, RuntimeError) as exc:  # RuntimeError = 数据库连不上等
        print(f"[初始化错误] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    try:
        core.info(
            "应用启动完成",
            version=__version__,
            config=str(settings.config_path) if settings.config_path else "默认值",
        )
        if settings.config_path is None:
            core.warning(f"没找到配置文件，按默认值启动；模板见 {TEMPLATE_PATH.name}")
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
