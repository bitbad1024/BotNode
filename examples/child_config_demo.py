"""子实例配置示例：一个名字 + 一份配置副本，复制完就冻结。

运行::

    python examples/child_config_demo.py

子实例只做两件事——**换个名字**、**复制一份父实例的配置**。名字按 ``.`` 逐段派生，
``core.child("b.c")`` 等价于 ``core.child("b").child("c")``，所以 ``a.b.c`` 拿到的是
``a.b`` 那份配置的副本::

    a                    核心：配置 = [console]
    ├─ a.b               child 时复制核心 -> [console]，本层再挂 b-file
    │  ├─ a.b.c          child 时复制 a.b  -> [console, b-file]，本层再挂 c-file
    │  └─ a.b.e          b 改完之后才派生 -> 复制到改完的 b（含 b-file2）
    └─ …

演示五件事：

1. **复制**：``c`` 只在派生时复制 ``b`` 的配置，``c`` 本层没挂 ``b-file`` 却照样进它；
2. **就地新增**：``c`` 再挂自己的 ``c-file``，父层与兄弟不受影响；
3. **冻结**：``b`` 之后再挂 ``b-file2``，已经派生过的 ``c`` 不变，改完之后才派生的 ``e`` 才拿到；
4. **核心自己也只是一份配置**：``a`` 写日志只进它自己挂的出口；
5. **顺序很重要**：副本在派生那一刻定格，所以要挂出口就**先挂、再取实例**。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nacho.core.logger import LocalFileLogProcessor, LogCore  # noqa: E402


def count_lines(path: Path) -> int:
    """数一数文件里有多少非空行（文件不存在算 0 行）。"""
    if not path.exists():
        return 0
    return len([line for line in path.read_text(encoding="utf-8").splitlines() if line])


def enable_line_buffering() -> None:
    """让 stdout 逐行刷出。

    否则把示例输出重定向 / 用管道捕获时，stdout 变成块缓冲，日志行会晚于 ``print``
    的小标题才出现，看起来像顺序错乱（终端里直接跑没这个问题）。
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(line_buffering=True)


async def settle(logger: LogCore) -> None:
    """等日志真正落地，让输出与小标题对齐。

    写入是**非阻塞**的：``info()`` 只把记录推进队列就返回，由分发器按
    ``dispatch_timeout`` 批量取走交给处理机，处理机再按自己的缓冲区刷出。
    所以演示里每个小节末尾等一下，读者才能看到「这一节的日志就在这一节下面」。
    生产环境不需要这么做，正常业务不关心落盘时机。
    """
    await asyncio.sleep(0.25)  # 给分发器一轮 dispatch_timeout
    await logger.flush()  # 再刷各处理机的缓冲区


def render(logger: LogCore) -> None:
    """画一下每个名字实例**那份配置副本**里的出口（只读，不会替谁把实例建出来）。"""
    print("  名字              该实例配置里的出口")
    for name in sorted(logger.routes):
        names = [processor.name for processor in logger.effective_outputs(name)]
        depth = name.count(".")
        indent = "  " * depth
        print(f"  {indent}{name:<24} [{', '.join(names)}]")


async def main() -> None:
    enable_line_buffering()

    log_dir: Path = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    def file_output(key: str, name: str) -> LocalFileLogProcessor:
        """一路文件出口：``buffer_size=1`` 逐条直写，方便中途数行数。"""
        return LocalFileLogProcessor(
            log_dir / f"child_{key}.log", name=name, buffer_size=1, flush_interval=0.05
        )

    def lines(key: str) -> int:
        return count_lines(log_dir / f"child_{key}.log")

    for key in ("b", "b2", "c", "late"):
        (log_dir / f"child_{key}.log").unlink(missing_ok=True)

    core = LogCore(name="a")  # 核心配置 = [console]
    await core.start()
    try:
        print("=== 0. 只有核心，一个子实例都还没有 ===")
        render(core)
        print("  实例是按需派生的：没调用过 child / get_logger 的名字还不存在。")

        print("\n=== 1. b 派生 + 本层挂 b-file ===")
        b = core.child("b")  # 名字 a.b，配置 = 从核心复制 = [console]
        b.attach(file_output("b", "b-file"))
        render(core)
        print("  a.b 复制到了 console，再加上本层挂的 b-file。")

        print("\n=== 2. c 派生（复制 b）+ 本层挂 c-file ===")
        c = core.child("b.c")  # 名字 a.b.c，逐段派生：b 已存在，于是复制 b
        c.info(message="c 的日志：派生时就复制到了 b-file，本层还没挂任何出口")
        await settle(core)
        c.attach(file_output("c", "c-file"))
        c.info(message="c：本层再挂 c-file，父层与兄弟不受影响")
        await settle(core)
        render(core)
        print("  c 本层只挂 c-file，却还进了 b-file —— 这就是「派生时复制一份配置」。")

        print("\n=== 3. 复制完就冻结：b 再挂 b-file2，c 不变而 e 拿到 ===")
        b.attach(file_output("b2", "b-file2"))  # b 新增一个出口
        c.info(message="c：b 改过了，可我早就复制完，照旧只进 console + b-file + c-file")
        e = core.child("b.e")  # b 改完之后**才派生**，复制到的是改完的 b
        e.info(message="e：我是 b 改完之后才派生的，复制到了 b-file2")
        await settle(core)
        render(core)
        print("  c 与 e 一比：c 手里那份是冻结的，e 复制到的是 b 修改后的结果。")

        print("\n=== 4. 顺序很重要：先派生再挂载，就复制不到 ===")
        z = core.child("b.z")  # 此刻从 b 复制（已含 b-file2）
        b.attach(file_output("late", "z-late"))  # 但这一路是 z 派生之后才挂的
        z.info(message="z：我派生之后 b 才挂的 z-late，我这里没有")
        await settle(core)
        render(core)
        print("  z 那份配置里没有 z-late：要挂出口，先挂到父实例、再取子实例。")

        print("\n=== 5. 核心自己也只是一份配置：a 写日志只进它自己挂的出口 ===")
        core.info(message="a：核心自己的日志，只进 console（核心那份配置里没有文件出口）")
        await settle(core)

        print("\n=== 6. 落盘结果：谁进了哪个文件，数一数就对得上 ===")
        print(f"  child_b.log    （a.b 本层挂的，b / c / e / z 都进）: {lines('b')} 行")
        print(f"  child_b2.log   （b 后来才挂的，c 收不到；e / z 收到）: {lines('b2')} 行")
        print(f"  child_c.log    （a.b.c 本层挂的）: {lines('c')} 行")
        print(f"  child_late.log （z 派生之后才挂的，z 收不到）: {lines('late')} 行")

        print("\n=== 内省接口（只读，不会替谁把实例建出来） ===")
        print(f"  a.b.c 那份配置副本: {[p.name for p in core.routes['a.b.c']]}")
        print(f"  a.b.c 实际会收到的: {[p.name for p in core.effective_outputs('a.b.c')]}")
        print(f"  各实例配置总览: {core.stats['routes']}")
    finally:
        await core.stop()


if __name__ == "__main__":
    asyncio.run(main())
