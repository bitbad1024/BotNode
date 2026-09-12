"""子实例配置示例：自层覆盖 + 没挂自层出口时回落父级。

运行::

    python examples/child_config_demo.py

子实例派生时只做两件事——**换个名字**、**把父实例实际会投的处理机复制一份**当作自己的
「落回配置」。之后：

* 子实例自己 ``attach`` 过出口（**自层出口**）就进入「自层覆盖」：写日志只投自层那些，
  不再带上父级 / 核心的文件出口（``inherit_on_override`` 的出口，如控制台，仍保留）；
* 一个自层出口都没挂时，整份走落回配置——像 ``d`` 这种没单独挂文件的名字，照旧写进
  核心的 ``a-file``。

名字按 ``.`` 逐段派生，``core.child("b.c")`` 等价于 ``core.child("b").child("c")``，
所以 ``a.b.c`` 的落回配置是 ``a.b`` 那一份::

    a                    自层 = [console, a-file]
    ├─ a.b               派生时复制 a -> 落回 [console, a-file]，本层再挂 b-file
    │  ├─ a.b.c          自层挂 c-file（覆盖）-> console + c-file
    │  ├─ a.b.e          b 改完之后才派生 -> 落回 [console, b-file, b-file2]，没自层 -> 全投
    │  └─ a.b.z          派生之后 b 才挂 z-late，z 的落回配置里没有它
    └─ a.d               没挂自层出口 -> 回落 a：[console, a-file]

演示五件事：

1. **自层覆盖**：``b`` 挂了 ``b-file`` 后，它的日志只进 ``b-file``（外加控制台），
   **不再进核心的 ``a-file``**——一个模块一个文件；
2. **无自层出口就回落**：``d`` 本层没挂任何出口，整份走从核心复制来的落回配置；
3. **冻结**：``b`` 之后再挂 ``b-file2``，已经派生过的 ``c`` 不变；改完之后才派生的 ``e``
   才在落回配置里拿到它；
4. **逐段派生**：``c`` 的落回配置来自 ``b``（不是核心）；
5. **顺序很重要**：落回配置在派生那一刻定格，所以要挂出口就**先挂、再取实例**。
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
    """画一下每个名字实例**实际会投**的出口（只读，不会替谁把实例建出来）。"""
    print("  名字                  实际会投的出口")
    for name in sorted(logger.routes):
        names = [processor.name for processor in logger.routes[name]]
        indent = "  " * name.count(".")
        print(f"  {indent}{name:<22} [{', '.join(names)}]")


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

    for key in ("all", "b", "b2", "c", "late"):
        (log_dir / f"child_{key}.log").unlink(missing_ok=True)

    core = LogCore(name="a")  # 核心自层 = [console]
    core.attach(file_output("all", "a-file"))  # 核心自层 = [console, a-file]
    await core.start()
    try:
        print("=== 0. 核心自层挂着全量文件 a-file ===")
        render(core)
        core.info(message="a：核心自己的日志进 console + a-file")

        print("\n=== 1. b 派生 + 本层挂 b-file（自层覆盖，不再进 a-file） ===")
        b = core.child("b")  # 名字 a.b，落回配置 = 复制核心 = [console, a-file]
        b.attach(file_output("b", "b-file"))  # b 自层 = [b-file]
        render(core)
        b.info(message="b：我挂了 b-file，就只进 console + b-file，a-file 收不到")
        await settle(core)

        print("\n=== 2. c 派生（落回配置来自 b）+ 本层挂 c-file ===")
        c = core.child("b.c")  # 逐段派生：b 已存在，于是复制 b 的解析结果
        c.info(message="c：还没挂自层出口，回落 b -> console + b-file")
        await settle(core)
        c.attach(file_output("c", "c-file"))  # c 自层 = [c-file]
        c.info(message="c：本层挂上 c-file，覆盖回落配置 -> console + c-file")
        await settle(core)
        render(core)
        print("  c 的落回配置来自 b（含 b-file），但自层有 c-file，所以只投 console + c-file。")

        print("\n=== 3. 冻结：b 再挂 b-file2，c 不变而 e 拿到 ===")
        b.attach(file_output("b2", "b-file2"))  # b 自层新增 b-file2
        c.info(message="c：b 改过了，可我早就派生完，照旧 console + c-file")
        e = core.child("b.e")  # b 改完之后**才派生**，落回配置 = b 解析结果（含 b-file2）
        e.info(message="e：我没挂自层出口，整份回落 b -> console + b-file + b-file2")
        await settle(core)
        render(core)
        print("  c 与 e 一比：c 手里那份是冻结的；e 复制到的是 b 修改后的结果。")

        print("\n=== 4. 无自层出口的名字：整份回落核心 ===")
        d = core.child("d")  # 没挂自层出口 -> 回落核心 [console, a-file]
        d.info(message="d：我没挂自己的文件，落回核心 -> console + a-file")
        await settle(core)
        render(core)

        print("\n=== 5. 顺序很重要：先派生再挂载，就复制不进落回配置 ===")
        z = core.child("b.z")  # 此刻从 b 复制落回配置（含 b-file2，但无 z-late）
        b.attach(file_output("late", "z-late"))  # 这一路是 z 派生之后才挂的
        z.info(message="z：我派生之后 b 才挂的 z-late，我这里没有")
        await settle(core)
        render(core)
        print("  z 的落回配置里没有 z-late：要挂出口，先挂到父实例、再取子实例。")

        print("\n=== 6. 落盘结果：谁进了哪个文件，数一数就对得上 ===")
        print(f"  child_all.log  （核心全量文件，a / d 进）: {lines('all')} 行")
        print(f"  child_b.log    （a.b 自层挂的，只收 b / c(未覆盖前) / e / z）: {lines('b')} 行")
        print(f"  child_b2.log   （b 后来才挂，c 收不到；e / z 收到）: {lines('b2')} 行")
        print(f"  child_c.log    （a.b.c 自层挂的，覆盖后只收 c）: {lines('c')} 行")
        print(f"  child_late.log （z 派生之后才挂的，z 收不到）: {lines('late')} 行")

        print("\n=== 内省接口（只读，不会替谁把实例建出来） ===")
        print(f"  a.b.c 实际会投: {[p.name for p in core.effective_outputs('a.b.c')]}")
        print(f"  还没派生的 a.b.q 会投: {[p.name for p in core.effective_outputs('a.b.q')]}")
        print(f"  各实例实际会投的出口总览: {core.stats['routes']}")
    finally:
        await core.stop()


if __name__ == "__main__":
    asyncio.run(main())
