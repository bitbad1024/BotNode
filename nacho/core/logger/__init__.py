"""异步日志系统：一根 root、按目标扇出、具名路由，业务侧拿 ``bind`` 出来的只读视图。

快速开始::

    from nacho.core.logger import configure, get_logger

    configure(level="INFO")
    get_logger("api.robot").info("收到请求")

设计要点、route / bind / 过滤器 / 内省与检索的完整说明见 ``docs/logger.md``。
"""
from __future__ import annotations

# ---- 进程门面（业务代码常用）----
from .core import mount_module
from .manager import configure, get_logger, manager

# ---- 核心与视图 ----
from .base import BaseLogger, BoundLogger
from .core import LogCore

# ---- 处理器（可挂到核心上）----
from .processors import (
    BaseLogProcessor,
    ConsoleLogProcessor,
    DatabaseLogProcessor,
    LocalFileLogProcessor,
)

# ---- 过滤器（挂在 Target 上）----
from .filters import LevelFilter, LogFilter

# ---- 数据模型 ----
from .models import LogLevel, LogRecord, LogSearchResult, Target, normalize_timestamp

# ---- 默认核心管理（高级 / 装配）----
from .core import current_default_core, default_core, set_default_core
from .manager import LogManager

# ---- 依赖协议（第三方存储实现用）----
from .interfaces import LogStore

__all__ = [
    # 进程门面（业务代码常用）
    "configure",
    "get_logger",
    "manager",
    "mount_module",
    # 核心与视图
    "LogCore",
    "BaseLogger",
    "BoundLogger",
    # 处理器
    "BaseLogProcessor",
    "ConsoleLogProcessor",
    "LocalFileLogProcessor",
    "DatabaseLogProcessor",
    # 过滤器
    "LogFilter",
    "LevelFilter",
    # 数据模型
    "LogLevel",
    "LogRecord",
    "LogSearchResult",
    "Target",
    "normalize_timestamp",
    # 默认核心管理（高级 / 装配）
    "default_core",
    "current_default_core",
    "set_default_core",
    "LogManager",
    # 依赖协议
    "LogStore",
]
