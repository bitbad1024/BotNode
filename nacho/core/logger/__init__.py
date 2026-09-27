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

    logger.attach(LocalFileLogProcessor("logs", prefix="nacho"))   # 运行期挂载（按天分片）
    await logger.stop()                     # 停机自动冲刷余量

子日志实例：一个名字 + 一份落回配置 + 自己的出口
================================================

:meth:`~nacho.core.logger.base.BaseLogger.child` 的名字**相对本实例**：
``core.child("robot")`` 得到的名字是 ``nacho.robot``（写全名 ``"nacho.robot"`` 也认，
见 :meth:`~nacho.core.logger.base.BaseLogger.qualify`）。名字按 ``.`` 逐段派生，
``child("robot.arm")`` 等价于 ``child("robot").child("arm")``，于是 ``arm`` 的落回配置
是 ``robot`` 那一份::

    core = LogCore("nacho")                                     # 核心 = [console]
    core.attach(LocalFileLogProcessor("logs", prefix="nacho"))  # 核心的全量文件出口

    core.attach(LocalFileLogProcessor("logs", prefix="robot"), name="nacho.robot")
    core.attach(LocalFileLogProcessor("logs", prefix="arm"), name="nacho.robot.arm")

    core.child("robot").info("就绪")           # -> console + robot 的片（自层覆盖，不进核心那份）
    core.child("robot.arm").info("过载")        # -> console + arm 的片（不进 robot / 核心那份）
    core.child("vision").info("没挂自己的出口")  # 自层为空 -> 回落核心：console + 核心那份

   「片」= 该前缀当天的分片文件（``<前缀>-<日期>[.<序号>].log``，见
   :class:`~nacho.core.logger.processors.LocalFileLogProcessor`）。

派生关系长成这样（左列是**落回配置**，右列是该名字**实际会投的**）::

    nacho              自层=[console, 核心片]   落回=[]                  实际=[console, 核心片]
     ├─ nacho.robot     自层=[robot 片]         落回=[console, 核心片]    实际=[console, robot 片]
     │   └─ …arm        自层=[arm 片]           落回=[console, robot 片]  实际=[console, arm 片]
     └─ nacho.vision    自层=[]                 落回=[console, 核心片]    实际=[console, 核心片]

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

默认字段（``bind``）：一段执行打一次标记
========================================

:meth:`~nacho.core.logger.base.BaseLogger.child` 换的是**出口与名字**（派生一个新实例），
:meth:`~nacho.core.logger.base.BaseLogger.bind` 换的只有**每条日志默认带什么** —— 返回
一个 :class:`~nacho.core.logger.base.BoundLogger` 视图：共享源实例的队列、出口与级别，
也不进实例注册表::

    log = get_logger("workflow").bind(workflow_id="w1", owner_id="u-admin")
    log.info("开始")                      # 自动带上 workflow_id / owner_id
    log.info("换归属", owner_id="u-2")    # 当次传的同名键压过默认的

一趟工作流、一次请求打一次标记，后面每个调用点只管写自己那句话，日志自己认得出是谁的。
``owner_id`` 是日志的一等字段（不塞 ``extra``，能按它精确检索），同样可以给它绑默认值。

两条约束决定了它能不能长期用得下去：

* **只读**：再 ``bind`` 一层只产出新视图、原视图不变，所以一份视图可以被多个协程
  同时拿着写，各自当次字段互不污染；
* **不登记**：视图永远不会出现在 ``routes`` / ``stats`` 里，``bind`` 多少次也不会让
  实例表变长。因此**请求级的值别拿它当实例用** —— ``user_id`` 这类每次都变的东西要么
  当次传参（``log.info("...", user_id=uid)``，成本就是一个关键字参数），要么随请求
  生命周期现绑现扔；真要给某个用户建一条固定的名字，那是 ``child`` 的事。

运行期挂载与过滤器::

    from nacho.core.logger import LevelFilter

    core.attach(
        LocalFileLogProcessor("logs", prefix="robot"),
        name="nacho.robot",
        log_filter=LevelFilter("WARNING"),
    )
    # 过滤器挂在出口一侧：被筛掉的日志连处理机的缓冲区都不进

进程门面（业务代码通常只用这三个）::

    from nacho.core.logger import attach_mount, configure, get_logger

    configure(level="INFO")                  # 建立（或复用）进程默认核心（名 nacho）
    attach_mount("api.robot", LocalFileLogProcessor("logs", prefix="api"))   # 名字相对核心
    get_logger("api.robot").info("收到请求")   # 与核心共享同一个队列

内省与排查（随时可查，只读，不会替谁把实例建出来）::

    logger.processors                         # 本实例配置里的处理机
    logger.routes                             # 各实例的配置总览
    logger.effective_outputs("nacho.robot")   # 该名字会收到的设备
    logger.effective_level("nacho.robot")     # 该名字的生效级别
    logger.stats                              # 队列 / 处理机 / 丢弃条数汇总

检索与刷新::

    await logger.flush()                           # 刷所有出口的缓冲区
    found = await logger.search(level="ERROR", limit=20)
    # 聚合各出口，按插入顺序（落库那份的自增 seq）倒序并按 record_id 去重；
    # found.records 是本页，found.total 是命中总数
    await logger.search(owner_id="u-admin")        # 只看某个人名下的日志（不填 = 谁都不限）

完整可运行的示例见 ``examples/logging_demo.py``（分阶段启动 / 模块出口隔离 / 崩溃隔离）
与 ``examples/child_config_demo.py``（子实例的自层覆盖、回落与冻结）。
"""

from .base import BaseLogger, BoundLogger
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
from .models import LogLevel, LogRecord, LogSearchResult, normalize_timestamp
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
    "BoundLogger",
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
    "LogSearchResult",
    "normalize_timestamp",
    # 日志处理机
    "BaseLogProcessor",
    "ConsoleLogProcessor",
    "LocalFileLogProcessor",
    "DatabaseLogProcessor",
    # 依赖协议
    "LogStore",
]
