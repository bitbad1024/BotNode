"""异步日志系统。

设计要点：

* 日志系统以 :class:`~nacho.core.logger.core.LogCore`（或基类
  :class:`~nacho.core.logger.base.BaseLogger`）形式提供，默认把日志推到消息队列；
* 实例化即带一路控制台输出，文件 / 数据库等输出通道在运行时按需挂载，
  挂载后自动启动，无需手动启动处理机；
* 分发器从队列批量取日志扇出给处理机，处理机异常被完全隔离，
  单点崩溃不会影响业务与其它处理机；
* 模块路由是一棵**名字树**（见下）：子模块只需给出一个字符串名字再挂输出设备，
  各模块的出口互不混杂；**父层挂一次，子层复制一份**——子名字第一次用到时把父名字的
  输出设备复制下来，此后各改各的（父层再改不回头影响已复制的子层），也可以就地只改
  其中一部分；级别则逐层继承，父层改了子名字立刻跟着变；
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

名字树：父层挂一次，子层复制一份
================================

名字按 ``.`` 分层。:meth:`~nacho.core.logger.base.BaseLogger.child` 的名字**相对本实例**：
``core.child("robot")`` 得到的名字是 ``nacho.robot``（写全名 ``"nacho.robot"`` 也认，
见 :meth:`~nacho.core.logger.base.BaseLogger.qualify`），这个名字就是树上的一个节点。
**挂载是分层的，一层只管自己这一层**：每一层都挂自己的输出设备，而**子节点第一次
用到时从父节点复制一份**——父层挂的设备，子层自动就有一份副本；要改就在子层就地
``attach`` 新增，改的只是子层自己那份::

    core = LogCore("nacho")                          # 树根：全量出口 = 控制台

    core.attach(LocalFileLogProcessor("logs/robot.log"), name="nacho.robot")  # 这层挂文件出口
    core.child("robot").set_level("DEBUG")           # 这层把级别放粗到 DEBUG

    arm = core.child("robot.arm")                    # 这层也挂一个自己的
    core.attach(LocalFileLogProcessor("logs/arm.log"), name="nacho.robot.arm")
    arm.info("关节过载")   # -> arm.log + robot.log + 控制台：本层挂的与复制来的各写一次

    core.child("vision").info("视觉就绪")             # 兄弟分支 -> 只有控制台

树因此长成这样（左列是**本层挂的**，右列是该名字**实际会收到的**）::

    nacho                  全量出口 = [console]
     ├─ nacho.robot        本层=[robot.log]   实际=[robot.log, console]          级别=DEBUG
     │   └─ ...arm         本层=[arm.log]     实际=[arm.log, robot.log, console] 级别=DEBUG（继承）
     └─ nacho.vision       本层=[-]           实际=[console]                      级别=INFO

设备复制规则：

* **复制**：子名字第一次用到（写日志 / 查路由）时，把最近的父节点那条链上的设备复制
  一份存进自己节点，本层再叠加自己挂的，去重后只投一次；
* **冻结**：副本一到手就固定——父层之后再挂 / 再摘，都不回头影响本层；
* 就地增删：本层 :meth:`~nacho.core.logger.base.BaseLogger.attach` 带 ``name`` 新增；
* 级别：沿名字路径**就近取第一个声明过的**（一路都没声明则用实例默认级别），
  **不复制**——父层改了，已经在用的子名字立刻跟着变。

**是复制，不是冒泡。** 路由在子名字**第一次用到**时把父层那条链的设备复制一份，再按
这份副本**只投递一次**；父层设备之所以收到子层日志，是因为子层复制了它，不会因此多写
一遍。复制放在「第一次用到」而不是「建节点」，是为了让运行期挂载照旧生效——节点常在
import 期就建好，出口往往之后才挂上。没建过节点的**裸名字**没有副本，仍现场沿树取
（父层改了立刻跟着变）。

只声明要改的那部分::

    core.child("robot.arm").set_level("ERROR")            # 只改级别，设备照旧复制

运行期挂载与过滤器::

    from nacho.core.logger import LevelFilter

    log = core.child("robot")                            # 名字 = nacho.robot
    log.attach(
        LocalFileLogProcessor("logs/robot.log"),
        name="nacho.robot",
        log_filter=LevelFilter("WARNING"),
    )
    # 过滤器挂在出口一侧：被筛掉的日志连处理机的缓冲区都不进

进程门面（业务代码通常只用这三个）::

    from nacho.core.logger import attach_mount, configure, get_logger

    configure(level="INFO")                  # 建立（或复用）进程默认核心（名 nacho）
    get_logger("api.robot").info("收到请求")   # 名字相对核心 = nacho.api.robot，与核心共享同一个队列
    attach_mount("api.robot", LocalFileLogProcessor("logs/api.log"))   # 子模块自己挂出口（同样相对核心）

内省与排查（随时可查；只读，不会替谁触发复制）::

    logger.routes                                        # 各名字**本层**挂的设备
    logger.outputs                                       # 全量出口（树根）
    logger.effective_outputs("nacho.robot.arm")          # 该名字**实际**会收到的设备
    logger.effective_level("nacho.robot.arm")            # 该名字**实际**生效的级别
    logger.unmounted_modules                             # 沿途一层都没挂出口的名字（只告警一次）
    logger.stats                                         # 队列 / 处理机 / 丢弃条数汇总

检索与刷新::

    await logger.flush()                           # 刷所有出口的缓冲区
    await logger.search(level="ERROR", limit=20)   # 聚合各出口，按时间倒序并按 record_id 去重

完整可运行的示例见 ``examples/logging_demo.py``（分阶段启动 / 名字路由 / 崩溃隔离）
与 ``examples/tree_routing_demo.py``（名字树的复制与冻结）。
"""

from .base import BaseLogger, RouteEntry, RouteTable
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
    "RouteEntry",
    "RouteTable",
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
