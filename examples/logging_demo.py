"""日志系统示例：分阶段启动 + 增量挂载 + 子模块独立挂载。

运行::

    python examples/logging_demo.py

演示四件事：

1. **最小化启动**：``LogCore()`` 天生带一路控制台输出，``start()`` 后立刻可见；
2. **增量挂载**：数据库 / 本地文件出口在运行期挂上去，且自动启动，不用手动
   ``await processor.start()``；
3. **模块解耦**：子模块给一个字符串名字 + 自己的输出设备，日志系统内部维护一棵
   **名字树**，各模块的文件互不混杂；**父层挂一次，子名字第一次用到时把父名字的设备
   复制一份**（``nacho.robot.arm`` 不用重复配就能进 ``robot.log``），复制后即冻结，
   详见 ``examples/tree_routing_demo.py``；
4. **默认全量输出**：父模块没给某个名字挂出口时只告警一次，日志照旧按全量输出
   投递（示例同时演示「单个处理机崩溃不影响业务与其它处理机」的隔离效果）。
"""

import asyncio
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nacho.core.logger import (  # noqa: E402
    BaseLogProcessor,
    DatabaseLogProcessor,
    LocalFileLogProcessor,
    LogCore,
    LogRecord,
    attach_mount,
    normalize_timestamp,
)
from nacho.core.logger.models import TimestampLike  # noqa: E402


class SqliteAdapter:
    """数据库层适配器示例：用标准库 sqlite3 实现 DatabaseAdapter 协议。"""

    def __init__(self, path: str = ":memory:", table: str = "logs") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = asyncio.Lock()
        self._table = table

    async def execute(self, sql: str, params: Sequence[object] | None = None) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._execute_sync, sql, params)

    async def execute_many(self, sql: str, rows: Sequence[Sequence[object]]) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._execute_many_sync, sql, rows)

    async def fetch_all(
        self, sql: str, params: Sequence[object] | None = None
    ) -> list[Mapping[str, object]]:
        async with self._lock:
            return await asyncio.to_thread(self._fetch_sync, sql, params)

    async def delete_before(self, before: TimestampLike) -> int:
        """删除 ``before`` 之前的历史日志，返回删除行数。"""
        cutoff = normalize_timestamp(before)
        if cutoff is None:
            raise ValueError("delete_before 需要一个明确的时刻，不能是 None")
        return await self.execute(
            f"DELETE FROM {self._table} WHERE timestamp < ?", (cutoff,)
        )

    def _execute_sync(self, sql: str, params: Sequence[object] | None) -> int:
        cursor = self._conn.execute(sql, tuple(params or ()))
        self._conn.commit()
        return cursor.rowcount

    def _execute_many_sync(self, sql: str, rows: Sequence[Sequence[object]]) -> int:
        cursor = self._conn.executemany(sql, [tuple(row) for row in rows])
        self._conn.commit()
        return cursor.rowcount

    def _fetch_sync(
        self, sql: str, params: Sequence[object] | None
    ) -> list[Mapping[str, object]]:
        cursor = self._conn.execute(sql, tuple(params or ()))
        return [dict(row) for row in cursor.fetchall()]


class BrokenLogProcessor(BaseLogProcessor):
    """故意抛异常的处理机，用于验证崩溃隔离。"""

    name = "broken"

    async def write(self, records: list[LogRecord]) -> None:
        raise RuntimeError("模拟处理机崩溃")

    async def search(self, **kwargs: object) -> list[LogRecord]:
        return []


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
    adapter: SqliteAdapter = SqliteAdapter(table="nacho_logs")
    logger.attach(DatabaseLogProcessor(adapter, table="nacho_logs", buffer_size=5, flush_interval=0.2))
    logger.attach(LocalFileLogProcessor(all_log, buffer_size=5, flush_interval=0.2))
    # 故意挂一个会崩溃的出口：连续 2 批写入失败后自动停用，业务与其它出口不受影响
    logger.attach(BrokenLogProcessor(buffer_size=5, flush_interval=0.2, max_failures=2))

    # ---- 阶段 3：子模块设置名字 + 设置输出设备（有则载入同一条转发列表）----
    robot = logger.child("nacho.robot")
    robot.attach(
        LocalFileLogProcessor(robot_log, name="local-robot", buffer_size=5, flush_interval=0.2),
        name="nacho.robot",
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

    # ---- 阶段 4：父模块没给某个名字挂出口 -> 告警一次，日志走默认全量输出 ----
    logger.child("nacho.arm").warning(message="这个模块没有专属出口，只会看到一条告警")

    await logger.flush()
    await asyncio.sleep(0.3)

    print("\n=== 检索 ERROR 及以上（聚合本地 + 数据库 + 控制台） ===")
    for record in await logger.search(level="ERROR", limit=5):
        print(f"[{record.datetime_text}] {record.level.name:<8} {record.message} {record.extra}")

    print("\n=== 只检索数据库处理机中「机器人执行」相关日志 ===")
    for record in await logger.search(query="机器人执行", limit=3, processors=["database"]):
        print(f"[{record.datetime_text}] {record.level.name:<8} {record.message}")

    await logger.stop()

    print("\n=== 模块解耦效果 ===")
    print(f"全量文件 {all_log.name}: {count_lines(all_log)} 行（所有模块）")
    print(f"模块文件 {robot_log.name}: {count_lines(robot_log)} 行（只有 nacho.robot）")
    print(f"模块文件 {vision_log.name}: {count_lines(vision_log)} 行（只有 nacho.vision）")
    routes = {name: [p.name for p in ps] for name, ps in logger.routes.items()}
    print(f"各名字「本层」挂的设备: {routes}")
    print(f"父模块没挂出口的名字: {logger.unmounted_modules}（日志已按全量输出投递）")

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
