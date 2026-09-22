"""异步日志系统。

设计要点：

* 日志系统以 :class:`~nacho.core.logger.core.LogCore`（或基类
  :class:`~nacho.core.logger.base.BaseLogger`）形式提供，默认把日志推到消息队列；
* 实例化即带一路控制台输出，文件 / 数据库等输出通道在运行时按需挂载，
  挂载后自动启动，无需手动启动处理机；
* 分发器从队列批量取日志扇出给处理机，处理机异常被完全隔离，
  单点崩溃不会影响业务与其它处理机；
* 子日志实例 = **一个名字 + 一份落回配置 + 自己的出口**：
  :meth:`~nacho.core.logger.base.BaseLogger.child` 派生时把父实例实际会投的处理机与
  过滤器复制一份进去作为「落回配置」；子实例自己挂了出口就只投自层那些（自层覆盖，
  不再带上父级 / 核心的文件出口），没挂才整份走落回配置。一条日志在同一个处理机上
  永远只投一次；
* 落回配置**创建即冻结**：父实例之后再挂 / 再摘都不回头影响已经建好的子实例；
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

子日志实例：一个名字 + 一份落回配置 + 自己的出口
================================================

:meth:`~nacho.core.logger.base.BaseLogger.child` 的名字**相对本实例**：
``core.child("robot")`` 得到的名字是 ``nacho.robot``（写全名 ``"nacho.robot"`` 也认，
见 :meth:`~nacho.core.logger.base.BaseLogger.qualify`）。名字按 ``.`` 逐段派生，
``child("robot.arm")`` 等价于 ``child("robot").child("arm")``，于是 ``arm`` 的落回配置
是 ``robot`` 那一份::

    core = LogCore("nacho")                               # 核心 = [console]
    core.attach(LocalFileLogProcessor("logs/nacho.log"))  # 核心的全量文件出口

    core.attach(LocalFileLogProcessor("logs/robot.log"), name="nacho.robot")
    core.attach(LocalFileLogProcessor("logs/arm.log"), name="nacho.robot.arm")

    core.child("robot").info("就绪")           # -> console + robot.log（自层覆盖，不进 nacho.log）
    core.child("robot.arm").info("过载")        # -> console + arm.log（不进 robot.log / nacho.log）
    core.child("vision").info("没挂自己的出口")  # 自层为空 -> 回落核心：console + nacho.log

派生关系长成这样（左列是**落回配置**，右列是该名字**实际会投的**）::

    nacho              自层=[console, nacho.log]   落回=[]                    实际=[console, nacho.log]
     ├─ nacho.robot     自层=[robot.log]            落回=[console, nacho.log]  实际=[console, robot.log]
     │   └─ …arm        自层=[arm.log]              落回=[console, robot.log]  实际=[console, arm.log]
     └─ nacho.vision    自层=[]                     落回=[console, nacho.log]  实际=[console, nacho.log]

配置规则：

* **自层覆盖**：``attach`` 挂到本实例（或用 ``name`` 指定某名字）的出口属于**自层**；
  自层一旦非空，该名字写日志就只投自层那些，不再带上父级 / 核心的文件出口。标了
  :attr:`~nacho.core.logger.processors.base.BaseLogProcessor.inherit_on_override`
  的出口（控制台）例外，仍会保留；
* **无自层出口就回落**：本层一个出口都没挂时，整份走「落回配置」——派生那一刻从
  父实例复制来的、父实例实际会投的处理机快照；
* **冻结**：落回配置一到手就固定——父实例之后再挂 / 再摘，都不回头影响本层；
* 级别同理：``child(name, level=...)`` / :meth:`~nacho.core.logger.base.BaseLogger.set_level`
  只改本实例，之后的子实例才复制得到。

**一条日志只投一处。** 记录按**它所属实例**解析后的配置投递，不沿名字向上回溯，
同一个处理机也不会重复投。

**顺序很重要**：落回配置在实例创建（第一次 ``child`` / ``get_logger``）时定格，
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
    await logger.search(owner_id="u-admin")        # 只看某个人名下的日志（不填 = 谁都不限）

完整可运行的示例见 ``examples/logging_demo.py``（分阶段启动 / 模块出口隔离 / 崩溃隔离）
与 ``examples/child_config_demo.py``（子实例的自层覆盖、回落与冻结）。
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
from .interfaces import LogStore
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
    "LogStore",
]
