"""异步日志系统。

设计要点：

* 日志系统以 :class:`~nacho.core.logger.core.LogCore`（或基类
  :class:`~nacho.core.logger.base.BaseLogger`）形式提供，默认把日志推到消息队列；
* 实例化即带一路控制台输出，文件 / 数据库等输出通道在运行时按需挂载，
  挂载后自动启动，无需手动启动处理机；
* 分发器从队列批量取日志扇出给处理机，处理机异常被完全隔离，
  单点崩溃不会影响业务与其它处理机；
* 子日志实例 = **一个名字 + 一份配置副本**：:meth:`~nacho.core.logger.base.BaseLogger.child`
  派生时把父实例的处理机与过滤器复制一份进去，写日志只投给自己这份副本，
  不会沿名字逐层累加——一条日志在同一个处理机上永远只投一次；
* 副本**创建即冻结**：父实例之后再挂 / 再摘都不回头影响已经建好的子实例；
* 过滤由分发器负责：挂载出口时用 ``log_filter`` 传一个
  :class:`~nacho.core.logger.filters.LogFilter`，分发器在查找分发时筛掉不该进
  该出口的日志；处理机只负责落地，不含任何过滤器。

快速开始（最小化启动 + 增量挂载）::

    from nacho.core.logger import LogCore, LocalFileLogProcessor

    logger = LogCore()                      # 只有控制台，立即可用
    await logger.start()
    logger.info("机器人已启动", robot_id="r-001")

    logger.attach(LocalFileLogProcessor("logs/nacho.log"))   # 运行期挂载
    await logger.stop()                     # 停机自动冲刷余量

子日志实例：一个名字 + 一份配置副本
==================================

:meth:`~nacho.core.logger.base.BaseLogger.child` 的名字**相对本实例**：
``core.child("robot")`` 得到的名字是 ``nacho.robot``（写全名 ``"nacho.robot"`` 也认，
见 :meth:`~nacho.core.logger.base.BaseLogger.qualify`）。名字按 ``.`` 逐段派生，
``child("robot.arm")`` 等价于 ``child("robot").child("arm")``，于是 ``arm`` 拿到的是
``robot`` 那份配置的副本::

    core = LogCore("nacho")                          # 核心配置 = [console]

    core.attach(LocalFileLogProcessor("logs/robot.log"), name="nacho.robot")
    core.attach(LocalFileLogProcessor("logs/arm.log"), name="nacho.robot.arm")

    core.child("robot").info("就绪")     # -> robot.log + console
    core.child("robot.arm").info("过载")  # -> arm.log + robot.log + console

派生关系长成这样（左列是**派生时复制到的**，右列是该名字**实际会投的**）::

    nacho               本层=[console]        实际=[console]
     ├─ nacho.robot      复制+本层=[console, robot.log]     实际=[robot.log, console]
     │   └─ …arm         复制+本层=[console, robot.log, arm.log]  实际=[arm.log, robot.log, console]
     └─ nacho.vision     复制=[console]        实际=[console]

复制规则：

* **复制**：``child`` 创建那一刻把父实例的处理机与过滤器各复制一份，自己再叠加
  本层 ``attach`` 的，去重后**只投一次**；
* **冻结**：副本一到手就固定——父实例之后再挂 / 再摘，都不回头影响本层；
* 就地增删：本层 :meth:`~nacho.core.logger.base.BaseLogger.attach` 改的是本层那份；
* 级别同理：``child(name, level=...)`` / :meth:`~nacho.core.logger.base.BaseLogger.set_level`
  只改本实例，之后的子实例才复制得到。

**是复制，不是冒泡。** 一条日志只按**它所属实例**那份配置投递，不沿名字向上回溯，
也不额外走一层「全量出口」；父层设备之所以收到子层日志，是因为子层复制了它，
不会因此多写一遍。

**顺序很重要**：副本在实例创建（第一次 ``child`` / ``get_logger``）时定格，
要挂出口请先挂载、再取实例。

运行期挂载与过滤器::

    from nacho.core.logger import LevelFilter

    core.attach(
        LocalFileLogProcessor("logs/robot.log"),
        name="nacho.robot",
        log_filter=LevelFilter("WARNING"),
    )
    # 过滤器挂在出口一侧：被筛掉的日志连处理机的缓冲区都不进

进程门面（业务代码通常只用这三个）::

    from nacho.core.logger import attach_mount, configure, get_logger

    configure(level="INFO")                  # 建立（或复用）进程默认核心（名 nacho）
    attach_mount("api.robot", LocalFileLogProcessor("logs/api.log"))   # 名字相对核心
    get_logger("api.robot").info("收到请求")   # 与核心共享同一个队列

内省与排查（随时可查，只读，不会替谁把实例建出来）::

    logger.processors                         # 本实例配置里的处理机
    logger.routes                             # 各实例的配置总览
    logger.effective_outputs("nacho.robot")   # 该名字会收到的设备
    logger.effective_level("nacho.robot")     # 该名字的生效级别
    logger.stats                              # 队列 / 处理机 / 丢弃条数汇总

检索与刷新::

    await logger.flush()                           # 刷所有出口的缓冲区
    await logger.search(level="ERROR", limit=20)   # 聚合各出口，按时间倒序并按 record_id 去重

完整可运行的示例见 ``examples/logging_demo.py``（分阶段启动 / 模块配置副本 / 崩溃隔离）
与 ``examples/child_config_demo.py``（子实例配置的复制与冻结）。
"""

from .base import BaseLogger
from .core import (
    LogCore,
    attach_mount,
    current_default_core,
    default_core,
    set_default_core,
)
from .filters import LevelFilter, LogFilter
from .interfaces import DatabaseAdapter
from .manager import LogManager, configure, get_logger, manager
from .models import LogLevel, LogRecord, normalize_timestamp
from .processors import (
    BaseLogProcessor,
    ConsoleLogProcessor,
    DatabaseLogProcessor,
    LocalFileLogProcessor,
)
from .queue import AsyncLogQueue, OverflowPolicy

__all__ = [
    # 日志系统
    "BaseLogger",
    "LogCore",
    "LogManager",
    "configure",
    "get_logger",
    "manager",
    "attach_mount",
    "default_core",
    "current_default_core",
    "set_default_core",
    # 过滤器（由分发器在查找分发时使用）
    "LogFilter",
    "LevelFilter",
    # 消息队列
    "AsyncLogQueue",
    "OverflowPolicy",
    # 数据模型
    "LogLevel",
    "LogRecord",
    "normalize_timestamp",
    # 日志处理机
    "BaseLogProcessor",
    "ConsoleLogProcessor",
    "LocalFileLogProcessor",
    "DatabaseLogProcessor",
    # 依赖协议
    "DatabaseAdapter",
]
