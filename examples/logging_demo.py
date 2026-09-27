"""日志系统示例：分阶段启动 + 增量挂载 + 子模块独立挂载。

运行::

    python examples/logging_demo.py

演示四件事：

1. **最小化启动**：``LogCore()`` 天生带一路控制台输出，``start()`` 后立刻可见；
2. **增量挂载**：数据库 / 本地文件出口在运行期挂上去，且自动启动，不用手动
   ``await processor.start()``；
3. **模块解耦**：子模块给一个字符串名字 + 自己的输出设备（``logger.child("robot")``），
   各模块的文件互不混杂；**子实例自己挂了出口就只投那份**（``robot`` 只进
   ``robot.log``，不再进核心的 ``nacho.log``），没挂自层出口的名字才整份走派生
   那一刻从核心复制的落回配置，详见 ``examples/child_config_demo.py``；
4. **落回配置不回溯**：子实例创建之后，父实例再挂 / 再摘都不影响它的落回配置；
   反过来，没单独挂出口的名字也不是「没出口」，而是走从核心复制来的那份落回配置。
   （示例同时演示「单个处理机崩溃不影响业务与其它处理机」的隔离效果。）
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402

from nacho.core.logger import (  # noqa: E402
    BaseLogProcessor,
    DatabaseLogProcessor,
    LocalFileLogProcessor,
    LogCore,
    LogRecord,
    LogSearchResult,
    attach_mount,
)
from nacho.db import SqlLogStore  # noqa: E402


class BrokenLogProcessor(BaseLogProcessor):
    """故意抛异常的处理机，用于验证崩溃隔离。"""

    name = "broken"

    async def write(self, records: list[LogRecord]) -> None:
        raise RuntimeError("模拟处理机崩溃")

    async def search(self, **kwargs: object) -> LogSearchResult:
        return LogSearchResult()


def count_lines(path: Path) -> int:
    """数一数文件里有多少行（文件不存在算 0 行）。"""
    if not path.exists():
        return 0
    return len([line for line in path.read_text(encoding="utf-8").splitlines() if line])


async def main() -> None:
    log_dir: Path = Path(__file__).resolve().parent / "logs"
    all_log: Path = log_dir / "nacho.log"
    robot_log: Path = log_dir / "robot.log"
    vision_log: Path = log_dir / "vision.log"

    # ---- 阶段 1：最小化启动，只有控制台，立即可用 -------------------------
    logger: LogCore = LogCore(name="nacho")
    await logger.start()
    logger.info(message="框架启动完成", version="0.1.0")

    # ---- 阶段 2：业务阶段，后端就绪后增量挂载 ----------------------------
    # 库出口只认「存储」（见 SqlLogStore）：这里挂一块内存 sqlite，要落文件就换
    # "sqlite+aiosqlite:///logs/nacho-log.db"（表由处理机启动时建好）
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    logger.attach(DatabaseLogProcessor(SqlLogStore(engine), buffer_size=5, flush_interval=0.2))
    logger.attach(LocalFileLogProcessor(all_log, buffer_size=5, flush_interval=0.2))
    # 故意挂一个会崩溃的出口：连续 2 批写入失败后自动停用，业务与其它出口不受影响
    logger.attach(BrokenLogProcessor(buffer_size=5, flush_interval=0.2, max_failures=2))

    # ---- 阶段 3：子模块设置名字 + 设置输出设备 ---------------------------
    # robot 派生时把核心的解析结果复制成落回配置（控制台 + 数据库 + nacho.log + broken）；
    # robot.attach 挂上自层文件后进入「自层覆盖」：只投 robot.log（外加控制台），
    # 数据库 / nacho.log 等核心出口对 robot 就失效了
    robot = logger.child("nacho.robot")
    robot.attach(
        LocalFileLogProcessor(robot_log, name="local-robot", buffer_size=5, flush_interval=0.2)
    )
    # 运行期挂载的通道由分发器补启动，这里不需要手动 start
    logger.info(message="子模块 nacho.robot 已挂载自己的日志文件")

    robot.debug(message="这条 DEBUG 会被子模块日志级别过滤掉")
    for index in range(12):
        robot.info(message=f"机器人执行第 {index + 1} 步", step=index + 1, robot_id="r-001")

    try:
        raise ValueError("模拟业务异常")
    except ValueError:
        robot.exception(message="机器人执行失败", robot_id="r-001")

    # 父模块也能按名字挂载；``attach_mount(name, processor, core=...)`` 是等价的便捷函数
    attach_mount(
        "nacho.vision",
        LocalFileLogProcessor(vision_log, name="local-vision", buffer_size=5, flush_interval=0.2),
        core=logger,
    )
    logger.child("nacho.vision").info(message="视觉模块开始工作")

    # ---- 阶段 4：没单独挂出口的名字，走的是从核心复制来的那份落回配置 ----------
    # arm 没挂自层出口，整份回落核心（控制台 + 数据库 + nacho.log + broken）；
    # 想让它有专属文件就 arm.attach(...)
    logger.child("nacho.arm").warning(message="arm 没有专属文件，走核心复制来的落回配置")

    await logger.flush()
    await asyncio.sleep(0.3)

    print("\n=== 检索 ERROR 及以上（聚合本地 + 数据库 + 控制台） ===")
    errors = await logger.search(level="ERROR", limit=5)
    print(f"命中 {errors.total} 条，本页 {len(errors.records)} 条：")
    for record in errors.records:
        print(f"[{record.datetime_text}] {record.level.name:<8} {record.message} {record.extra}")

    print("\n=== 只检索数据库处理机中「机器人执行」相关日志 ===")
    robot_logs = await logger.search(query="机器人执行", limit=3, processors=["database"])
    print(f"命中 {robot_logs.total} 条，本页 {len(robot_logs.records)} 条：")
    for record in robot_logs.records:
        print(f"[{record.datetime_text}] {record.level.name:<8} {record.message}")

    await logger.stop()
    await engine.dispose()  # 日志已冲刷落库，可以关连接了

    print("\n=== 模块解耦效果 ===")
    print(f"全量文件 {all_log.name}: {count_lines(all_log)} 行（核心自己 + 没挂自有出口的模块，如 arm）")
    print(f"模块文件 {robot_log.name}: {count_lines(robot_log)} 行（只有 nacho.robot）")
    print(f"模块文件 {vision_log.name}: {count_lines(vision_log)} 行（只有 nacho.vision）")
    routes = {name: [p.name for p in ps] for name, ps in logger.routes.items()}
    print(f"各名字实例实际会投的出口: {routes}")

    print("\n=== 运行状态 ===")
    # 两级缓冲都可能丢日志，stats["dropped"] 把它们汇成一个视图，排查只看一处
    stats = logger.stats
    print(
        f"丢弃条数（合计 {stats['dropped']['total']}）: "
        f"队列={stats['dropped']['queue']} 处理机缓冲={stats['dropped']['buffers']}"
    )
    for stats in logger.stats["processors"]:
        note = "" if stats["healthy"] else "  <- 连续失败达阈值，已自动停用"
        print(
            f"处理机 {stats['name']:<12} 健康={stats['healthy']} "
            f"已写入={stats['written']} 失败={stats['failed']}{note}"
        )


if __name__ == "__main__":
    asyncio.run(main())
