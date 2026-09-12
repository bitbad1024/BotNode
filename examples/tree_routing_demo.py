"""名字树路由示例：父层挂一次，子层**复制**一份，之后各改各的。

运行::

    python examples/tree_routing_demo.py

「树形挂载」说的是这件事——**挂载分层，一层只说自己这一层，子层复制父层的副本**。
树根叫 ``a``，子模块是 ``b``（名字 ``a.b``），孙子模块是 ``c``（名字 ``a.b.c``）::

    a                        全量出口 = 控制台（所有名字都会进，且实时生效）
    ├─ a.b                   本层挂 b-file
    │  ├─ a.b.c              第一次写日志时复制到 b-file，本层再挂 c-file（副本冻结）
    │  ├─ a.b.d              没建过节点：不复制，现场沿树取（父层改了立刻跟着变）
    │  └─ a.b.e              b 改完之后才建的，复制到的是**改完**的 b
    └─ …

关键在于**复制**，不在「现场取」：

* 父层挂一次，子层第一次用到时拿到一份**自己的副本**，日志名字自动是完整路径
  ``a.b.c``——不用再写一遍 ``a`` / ``a.b``，也不用在日志里拼前缀；
* 用 :meth:`~nacho.core.logger.base.BaseLogger.attach` 的 ``name`` 参数把出口挂到某一层，
  该名字及其子名字都会收到（:func:`~nacho.core.logger.attach_mount` 是它的便捷包装）；
* **副本一到手就冻结**：这一层复制过之后，父层再挂 / 再卸都不回头影响它；
  而在那之后才建的下层，复制到的是修改后的结果——两条一比就看出区别了；
* 只在 :meth:`~nacho.core.logger.base.BaseLogger.child` 里用过、**没建过节点**的裸名字
  没有副本，每次都现场沿树取，父层改了立刻跟着变。

级别是另一条轴，走「继承」而不是复制：父层声明 DEBUG，子名字立刻跟着放行。

演示五件事：

1. **复制**：``b`` 挂一个文件出口，``c`` 第一次写日志就能进它；
2. **就地新增**：``c`` 再挂自己的出口，父层与兄弟不受影响；
3. **冻结**：``b`` 再改，已经复制过的 ``c`` 不变，改完之后建的 ``e`` 才拿到新结果；
4. **裸名字现场解析**：没建过节点的 ``d`` 每次都跟着 ``b`` 的最新状态走；
5. **全量出口是共享实时的**：它不参与复制，随时 ``attach`` 一路，所有名字都会进。
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


def render_tree(logger: LogCore) -> None:
    """画名字树：每层**本层**挂的，以及该名字**实际**会收到的出口。

    只读——**不会替谁触发复制**；``实际`` 一列就是「复制来的 + 本层挂的 + 全量出口」
    去重后的结果。只在 ``child`` 里用过、没建过节点的裸名字不会出现在本层表里。
    """
    routes = logger.routes
    # 由名字反推父子关系：``a.b.c`` 的父亲就是 ``a.b``
    children: dict[str, list[str]] = {}
    for name in routes:
        parent = name.rsplit(".", 1)[0] if "." in name else ""
        children.setdefault(parent, []).append(name)

    def render_subtree(name: str, prefix: str, is_last: bool) -> None:
        own = ", ".join(processor.name for processor in routes[name]) or "-"
        names = [processor.name for processor in logger.effective_outputs(name)]
        actual = ", ".join(names) or "-"
        head = f"{prefix}{'└─ ' if is_last else '├─ '}{name}"
        print(f"{head:<34} 本层=[{own:<16}] 实际=[{actual}]")
        kids: list[str] = sorted(children.get(name, []))
        child_prefix = prefix + ("   " if is_last else "│  ")
        for index, kid in enumerate(kids):
            render_subtree(kid, child_prefix, index == len(kids) - 1)

    outputs = ", ".join(processor.name for processor in logger.outputs)
    print(f"全量出口（不带名字挂载，任何名字的日志都会进，且实时生效）: [{outputs}]")
    print("（本层=[-] 本层没挂；实际= 复制来的 + 本层挂的 + 全量出口，去重后）")
    print()
    # 本实例自己的名字不在树上（它只用来写日志），这里只当排版用的树根
    print(f"{logger.name:<34} （树根：实例自己的名字，它的日志走全量出口）")
    tops: list[str] = sorted(children.get(logger.name, []))
    for index, name in enumerate(tops):
        render_subtree(name, "", index == len(tops) - 1)


def outputs_of(logger: LogCore, name: str) -> str:
    """某个名字实际会收到的出口名字（逗号分隔）。"""
    return ", ".join(processor.name for processor in logger.effective_outputs(name))


async def main() -> None:
    enable_line_buffering()

    log_dir: Path = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    def file_output(key: str, name: str) -> LocalFileLogProcessor:
        """一路文件出口：``buffer_size=1`` 逐条直写，方便中途数行数。"""
        return LocalFileLogProcessor(
            log_dir / f"tree_{key}.log", name=name, buffer_size=1, flush_interval=0.05
        )

    def lines(key: str) -> int:
        return count_lines(log_dir / f"tree_{key}.log")

    for key in ("b", "b2", "c", "e", "all"):
        (log_dir / f"tree_{key}.log").unlink(missing_ok=True)

    # ---- 树根 a：只挂控制台（不带名字 = 全量出口） ----------------------
    core = LogCore(name="a")

    await core.start()
    try:
        print("=== 0. 一层都还没挂出口：全量出口只有控制台 ===")
        render_tree(core)
        print("  名字树还是空的——节点会在第一次给它挂出口（或声明级别）时才建出来。")

        print("\n=== 1. 父层挂一次：b 本层挂上 b-file ===")
        # ``attach(..., name="a.b")`` 把出口挂进「a.b」这一层，同时把节点建出来
        core.attach(file_output("b", "b-file"), name="a.b")
        render_tree(core)
        print("  「a.b」本层有了 b-file；它的子名字写日志时会各复制一份。")

        print("\n=== 2. 子层第一次写日志：c 本层挂 c-file，并复制到 b 的出口 ===")
        core.attach(file_output("c", "c-file"), name="a.b.c")
        c = core.child("a.b.c")
        c.info(message="c 的日志：名字自动是 a.b.c，不用写前缀")
        await settle(core)
        render_tree(core)
        print("  c 本层只挂 c-file，却还进了 b-file —— 这就是「父层挂一次，子层复制一份」。")

        print("\n=== 3. 复制完一遍就冻结：b 再改，c 不变 ===")
        core.attach(file_output("b2", "b-file2"), name="a.b")  # b 新增一个出口
        c.info(message="c：b 改过了，可我早就复制完，照旧只进 c-file + b-file")
        core.attach(file_output("e", "e-file"), name="a.b.e")  # b 改完之后**才建**的弟弟
        e = core.child("a.b.e")
        e.info(message="e：我是 b 改完之后才建的，复制到的是改完的 b（含 b-file2）")
        await settle(core)
        render_tree(core)
        print("  c 与 e 一比：c 手里那份是冻结的，e 复制到的是 b 修改后的结果。")

        print("\n=== 4. 裸名字现场解析：没建过节点的 d 每次都跟着 b 走 ===")
        core.child("a.b.d").info(message="d：我没有自己的节点，每次都现场取 b 的最新出口")
        await settle(core)
        print(f"  d 的实际出口=[{outputs_of(core, 'a.b.d')}]：b-file 与 b-file2 都在（不是副本）。")

        print("\n=== 5. 全量出口是共享实时的：随时挂一路，所有名字都会进 ===")
        core.attach(file_output("all", "everything"))
        c.info(message="c：全量出口新挂的 everything，我也立刻进")
        await settle(core)
        print(f"  c 的实际出口=[{outputs_of(core, 'a.b.c')}]：everything 已实时汇入。")

        print("\n=== 6. 落盘结果：谁进了哪个文件，数一数就对得上 ===")
        for key, label in (
            ("b", "b-file（b 本层挂的，c / d / e 都进）"),
            ("b2", "b-file2（b 后来才挂的，c 收不到）"),
            ("c", "c-file（c 本层挂的）"),
            ("e", "e-file（e 本层挂的）"),
            ("all", "everything（全量出口，各模块都进）"),
        ):
            print(f"  {label}: {lines(key)} 行")

        print("\n=== 内省接口（随时可查树上的状态，只读） ===")
        print(f"  c 本层挂的: {[p.name for p in core.routes['a.b.c']]}")
        print(f"  c 实际会收到的: [{outputs_of(core, 'a.b.c')}]")
        print(f"  各名字「本层」挂的设备: {core.stats['routes']}")
    finally:
        await core.stop()


if __name__ == "__main__":
    asyncio.run(main())
