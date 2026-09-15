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
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import cast

from config import CONFIG_PATH, TEMPLATE_PATH, ConfigError, Settings
from nacho import __version__
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
    """按设置初始化进程默认日志系统：configure 建核心（顺带挂文件 / 数据库出口）-> start。"""
    processors: list[BaseLogProcessor] = []
    if settings.file_enabled:
        processors.append(
            LocalFileLogProcessor(
                settings.file_path,
                name="file",
                buffer_size=settings.buffer_size,
                flush_interval=settings.flush_interval,
                max_bytes=settings.max_bytes if settings.max_bytes > 0 else None,
                backup_count=settings.backup_count,
            )
        )
    if settings.db_enabled:
        if settings.db_driver == "mariadb":
            adapter = MariadbAdapter(
                host=settings.db_host,
                port=settings.db_port,
                user=settings.db_user,
                password=settings.db_password,
                database=settings.db_database,
                table=settings.db_table,
            )
            paramstyle = "format"  # PyMySQL 的 %s 占位
        else:
            adapter = SqliteAdapter(settings.db_path, table=settings.db_table)
            paramstyle = "qmark"  # sqlite 的 ? 占位
        _db_adapters.append(adapter)
        processors.append(
            DatabaseLogProcessor(
                adapter,
                table=settings.db_table,
                paramstyle=paramstyle,
                buffer_size=settings.db_buffer_size,
                flush_interval=settings.db_flush_interval,
            )
        )
    core = configure(
        settings.name,
        level=LogLevel.DEBUG if settings.debug else settings.level,
        console=settings.console,
        console_color=settings.console_color,
        console_level=settings.console_level,
        queue_maxsize=settings.queue_maxsize,
        dispatch_batch_size=settings.dispatch_batch_size,
        dispatch_timeout=settings.dispatch_timeout,
        processors=processors,
    )
    _ = await core.start()
    return core


# --------------------------------------------------------------------------- 业务
async def run() -> None:
    """业务入口：初始化完成之后真正干活的地方（实现接这里）。"""
    log = get_logger("app")
    log.debug("这条 DEBUG 默认被级别挡住")
    log.info("业务开始", version=__version__)
    log.warning("业务占位：把实现接进 run() 即可")
    await scheduler.start()
    #scheduler.add("*/1 * * * *", lambda:print("每秒一次"),  name="巡检")
    #scheduler.add("*/5 * * * *", lambda:print("每五秒一次"), name="巡检")
    step = 0
    while True:
        log.info(f"{step}写入")
        step += 1
        await asyncio.sleep(1)


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
    finally:
        await manager.stop()  # 停机自动冲刷余量，收尾日志不会丢
        await scheduler.stop()
        for adapter in _db_adapters:  # 余量落库之后再关连接
            adapter.close()


def main(argv: Sequence[str] | None = None) -> None:
    """同步入口：``python app.py`` 走这里。"""
    asyncio.run(_main(argv))


if __name__ == "__main__":
    main()
