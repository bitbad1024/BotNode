"""定时调度器示例：cron 触发、任务运行状态、单 / 多实例、失败隔离。

运行::

    python examples/scheduler_demo.py

跑完约 15 秒 —— 演示的是秒级 cron，时间得真等。

调度器只有三样东西：表达式（``CronExpr``）说「什么时候跑」，任务（``Task``）说「跑什么」，
管理器（``TaskManager``）负责登记、启停、改定义。业务代码只碰管理器：
``nacho.core.scheduler`` 里的 ``scheduler`` 就是 ``TaskManager()`` 的进程级单例
（和 ``app.py`` 里用的是同一个），执行循环和它背后的红黑树时间线都不用管。

按顺序演示这几件事：

0. **登记不等于排程**：``add()`` 只是记进登记簿，``next_run`` 由执行循环算 —— 所以
   ``start()`` 之前它一直是 ``None``，循环第一圈才把任务排进时间线；
1. **排程失败的两种**：cron 语法 / 越界错在 ``add()`` 当场抛 :class:`CronError`；
   语法合法但永远等不到触发点的（``0 0 30 2 *`` —— 2 月没有 30 号）只在排程时记一条
   error，``next_run`` 保持 ``None``，循环照跑；
2. **预览触发点**：``preview()`` 不用建任务就能看接下来几次；6 段表达式（最前面是秒）
   能落在任意秒上，5 段永远落在整分 0 秒；
3. **同步 / 协程函数都能当任务**：协程直接 await，同步函数丢线程池 —— 所以同步函数里
   的阻塞挡的是线程，不是事件循环；
4. **单实例 vs 多实例**：默认单实例，上次没跑完就到点就跳过本次并记一条 warning；
   ``multi_instance=True`` 的任务到点就开新实例，允许叠加（``task.active`` 会 > 1）；
5. **失败隔离**：任务抛异常只更新 ``last_ok`` / ``fail_count`` / ``last_error``，
   绝不带崩循环；
6. **改定义立刻生效**：启停 / 换 cron / 换函数都会唤醒循环重排，不用等下一圈轮询；
7. **手动触发**：``run_now()`` 跑一次，排程不动（连停用的任务也照跑）；
8. **停机与重启**：``stop()`` 是等在飞的任务收尾（不是掐断）；重开时错过的触发点
   **不补跑**，直接从 ``next_after(now)`` 重排。
"""

import asyncio
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from unicodedata import east_asian_width

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nacho.core.logger import BaseLogger, LogCore, configure, get_logger  # noqa: E402
from nacho.core.logger import manager as log_manager  # noqa: E402
from nacho.core.scheduler import CronError, TaskManager, scheduler  # noqa: E402

#: 业务日志实例：main 里先 configure() 建进程默认核心，再取它（见 main 开头的说明）
log: BaseLogger


def stamp() -> str:
    """当前时刻，精确到秒 —— 秒级 cron 看时序用。"""
    return f"{datetime.now():%H:%M:%S}"


def enable_line_buffering() -> None:
    """让 stdout 逐行刷出，输出被重定向 / 管道捕获时行序才不乱。"""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(line_buffering=True)


async def settle(core: LogCore) -> None:
    """等日志真正落到控制台，让小标题和它下面的日志行对齐。

    写入是**非阻塞**的：``info()`` 只把记录推进队列，由分发器按 ``dispatch_timeout``
    批量取走，所以 ``print`` 与日志行之间本来有一小段延迟差。示例里每处要对着日志说话
    的地方等一下（生产环境不用管，正常业务不关心落地时机）。
    """
    await asyncio.sleep(0.1)
    await core.flush()


def width(text: str) -> int:
    """显示宽度：中日韩字符在终端里占两列，对齐列宽时得按这个算。"""
    return sum(2 if east_asian_width(char) in "WF" else 1 for char in text)


def pad(text: str, columns: int) -> str:
    """按显示宽度左对齐补空格 —— 中文任务名才和后面的列对得齐。"""
    return text + " " * max(0, columns - width(text))


# --------------------------------------------------------------------------- 任务函数
async def heartbeat() -> None:
    """协程任务：调度器直接 await，不占线程。"""
    log.info(f"心跳 {stamp()}")


def pool_probe() -> None:
    """同步任务：调度器丢线程池跑 —— 里面的阻塞挡的是线程，不是事件循环。"""
    time.sleep(0.4)  # 故意阻塞：心跳照旧按点打，说明事件循环没被挡住
    log.info(f"同步任务在 {threading.current_thread().name} 上跑完（其间阻塞 0.4 秒）")


async def slow_single() -> None:
    """单实例任务：要跑 1.5 秒，而 cron 是每 1 秒 —— 上一次没跑完就到点了。"""
    log.info(f"单实例任务开始 {stamp()}（跑 1.5 秒）")
    await asyncio.sleep(1.5)
    log.info(f"单实例任务结束 {stamp()}")


async def slow_multi() -> None:
    """多实例任务：同样的时长与频率，但允许叠加。"""
    log.info(f"多实例任务开始 {stamp()}（跑 1.5 秒）")
    await asyncio.sleep(1.5)
    log.info(f"多实例任务结束 {stamp()}")


async def boom() -> None:
    """会抛异常的任务：调度器只记账，不带崩循环。"""
    raise RuntimeError("模拟任务内部异常")


async def dormant_old() -> None:
    """休眠任务最初的函数（换掉之前跑的是它，示例里它一次都没跑上）。"""
    log.info(f"休眠任务跑的是旧函数 {stamp()}")


async def dormant_new() -> None:
    """换上去的新函数（set_func 之后跑它）。"""
    log.info(f"休眠任务换了新函数，这一拍跑的就是它 {stamp()}")


# --------------------------------------------------------------------------- 展示
def render(task_manager: TaskManager) -> None:
    """打印任务表：顺序就是触发顺序（list() 按 next_run 从近到远排，排不上的垫底）。"""
    header = (
        pad("任务", 12)
        + pad("cron", 18)
        + pad("状态", 8)
        + pad("在跑", 6)
        + pad("执行", 6)
        + pad("失败", 6)
        + pad("下次触发", 12)
        + "上次结果"
    )
    print("  " + header)
    for task in task_manager.list():
        if not task.enabled:
            state = "停用"
        elif task.running:
            state = "运行中"
        else:
            state = "待触发"
        if task.last_ok is None:
            outcome = "还没跑过"
        elif task.last_ok:
            outcome = "成功"
        else:
            outcome = f"失败：{task.last_error}"
        moments = f"{task.next_run:%H:%M:%S}" if task.next_run else "—"
        print(
            "  "
            + pad(task.display_name, 12)
            + pad(str(task.cron), 18)
            + pad(state, 8)
            + pad(str(task.active), 6)
            + pad(str(task.run_count), 6)
            + pad(str(task.fail_count), 6)
            + pad(moments, 12)
            + outcome
        )


# --------------------------------------------------------------------------- 主流程
async def main() -> None:
    global log  # 日志实例要在 configure() 之后取，见下面两行

    enable_line_buffering()
    # 先建进程默认核心再取日志实例：调度器自己记的 warning / error 也进这个核心，
    # 所以它的提示和业务日志会出现在同一个控制台上（app.py 里是按配置 config 来建这个核心）
    core: LogCore = configure("nacho", console_color=True, dispatch_timeout=0.05)
    await core.start()
    log = get_logger("demo.scheduler")

    try:
        # ---------------------------------------------------------------- 0. 登记
        print("=== 0. 登记任务：还没 start()，next_run 一律是 None ===")
        print("  说明：add() 只把任务记进登记簿，next_run 由执行循环算 —— 循环没跑，谁也排不了程。")
        scheduler.add(
            "*/2 * * * * *", heartbeat, task_id="heartbeat", name="心跳", description="每 2 秒"
        )
        scheduler.add(
            "*/3 * * * * *", pool_probe, task_id="pool_probe", name="同步任务", description="丢线程池"
        )
        scheduler.add(
            "*/1 * * * * *", slow_single, task_id="slow_single", name="单实例", description="跑 1.5 秒"
        )
        scheduler.add(
            "*/1 * * * * *",
            slow_multi,
            task_id="slow_multi",
            name="多实例",
            description="跑 1.5 秒，允许叠加",
            multi_instance=True,
        )
        scheduler.add("*/4 * * * * *", boom, task_id="boom", name="会失败", description="每次抛异常")
        scheduler.add(
            "* * * * *",  # 5 段写法：每分钟的 0 秒（示例不到一分钟，所以先停用着）
            dormant_old,
            task_id="dormant",
            name="休眠",
            description="先停用，后面再唤醒",
            enabled=False,
        )
        render(scheduler)

        # ---------------------------------------------------------------- 1. 启动
        print("\n=== 1. 启动：循环第一圈把登记簿全量对账，next_run 立刻落定 ===")
        await scheduler.start()
        await scheduler.start()  # 重复启动是空操作
        await asyncio.sleep(1.2)
        render(scheduler)

        # --------------------------------------------------- 2. 排不上的两种写法
        print("\n=== 2. 排程失败的两种：登记就抛错 vs 只记一条 error ===")
        scheduler.add("0 0 30 2 *", heartbeat, task_id="dead", name="死配置")
        await settle(core)
        print("  上面那条 ERROR 就是刚登记的死配置：2 月没有 30 号 —— 语法合法，但排程时")
        print("  一年内都找不到触发点。它 next_run 一直是 None，待在登记簿里不影响别的任务。")
        try:
            scheduler.add("*/0 * * * *", heartbeat, task_id="bad", name="非法步进")
        except CronError as exc:
            print(f"  步进写成 0 则是登记当场抛 CronError：{exc}")
        render(scheduler)

        # ---------------------------------------------------------------- 3. 预览
        print("\n=== 3. preview()：不建任务也能看触发点（6 段落秒，5 段落整分 0 秒） ===")
        for expr in ("*/30 * * * * *", "*/5 * * * *", "0 9 * * 1-5"):
            moments = " | ".join(f"{moment:%m-%d %H:%M:%S}" for moment in scheduler.preview(expr, 3))
            print(f"  {expr:<16} -> {moments}")

        # ---------------------------------------------------------- 4. 跑一段看现象
        print("\n=== 4. 跑 6 秒：心跳、同步任务、单 / 多实例、会失败的任务同时进行 ===")
        for round_no in (1, 2, 3):
            await asyncio.sleep(2)
            print(f"  -- 第 {round_no} 次观察（{stamp()}） --")
            render(scheduler)
        print("  看点一：单实例 的「在跑」只在 0/1 之间跳 —— 没跑完就到点，跳过本次（日志里有 warning）；")
        print("          多实例 的「在跑」会到 2/3 —— 到点就开新实例，允许叠加。")
        print("  看点二：心跳 每 2 秒、同步任务 每 3 秒都照常推进 —— 同步任务在别的线程上阻塞，")
        print("          事件循环没被挡住（执行完那行会带上线程名 asyncio_N）。")
        print("  看点三：会失败 的「失败」在涨、上次结果是异常摘要，而其它任务照跑。")

        # ------------------------------------------------------ 5. 改定义立刻生效
        print("\n=== 5. 改定义：停用 / 换 cron / 换函数 / 改名字，都立刻重排 ===")
        scheduler.set_enabled("heartbeat", False)
        await asyncio.sleep(0.4)  # 给循环一拍，看重排结果
        print("  停用 心跳 之后：next_run 变成 —（摘出时间线、不占排程），执行数也不再涨。")
        scheduler.set_cron("dormant", "*/1 * * * * *")
        scheduler.set_func("dormant", dormant_new)
        scheduler.set_name("dormant", "已唤醒")
        scheduler.set_enabled("dormant", True)
        await asyncio.sleep(2.5)
        render(scheduler)
        print("  休眠 本来停用着（连 next_run 都没有），换 cron + 换函数 + 启用后就跑起来了。")

        # ---------------------------------------------------------- 6. 手动触发一次
        print("\n=== 6. run_now()：手动跑一次，排程不动（连停用的任务也照跑） ===")
        target = scheduler.get("heartbeat")
        before = target.next_run
        await scheduler.run_now("heartbeat")  # 返回 asyncio.Task，await 它就等这次跑完
        await settle(core)
        moments = "—" if before is None else f"{before:%H:%M:%S}"
        print(f"  心跳 正停用着，但上面那行「心跳」还是打出来了：执行数涨到 {target.run_count}。")
        print(f"  排程没被动过，next_run 仍是 {moments}（停用着，本来就是空的）。")

        # -------------------------------------------------------- 7. 停机是等收尾
        print("\n=== 7. stop() 不是掐断：等在飞的任务跑完（最多等 timeout 秒） ===")
        scheduler.run_now("slow_multi")  # 故意留一个正在跑的任务
        await asyncio.sleep(0.1)
        started = time.perf_counter()
        await scheduler.stop()
        await settle(core)
        print(f"  stop() 花了 {time.perf_counter() - started:.1f} 秒 —— 它在等上面那个 1.5 秒的任务收尾。")
        print(f"  running = {scheduler.running}，停机后任务表照样能读；注意「下次触发」停在过去的")
        print("  时刻上（循环不跑了就不再往前排），重启时会被统一重排：")
        render(scheduler)

        # -------------------------------------------------------- 8. 重启：不补跑
        print("\n=== 8. 重启：停掉的这段时间错过的触发点不补跑 ===")
        missed_from = scheduler.get("slow_single").run_count
        await asyncio.sleep(1.5)  # 停机期间至少错过 1 拍（这个任务每秒一拍）
        await scheduler.start()
        await asyncio.sleep(0.3)
        await settle(core)
        revived = scheduler.get("slow_single")
        print(f"  停机 1.5 秒里错过了 1~2 拍，执行数却只从 {missed_from} 涨到 {revived.run_count}：")
        print("  醒来后是从 next_after(now) 重排的，不在过去的触发点上补跑一串。")
        print(f"  下次触发 {revived.next_run:%H:%M:%S}（此刻 {stamp()}），确实落在未来。")
        print("  顺带：这轮重启的全量对账又碰了一次死配置，所以上面还会再记一条 ERROR ——")
        print("  它每轮对账都会被重试一次，但绝不会带崩循环。")
    finally:
        await scheduler.stop()
        await log_manager.stop()  # 停机冲刷日志余量
        print("\n=== 完：调度器与日志核心都已停掉 ===")


if __name__ == "__main__":
    asyncio.run(main())
