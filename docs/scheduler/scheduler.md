# 定时任务（`nacho.core.scheduler`）

## 设计要点

- **只有三样东西对业务可见**：`CronExpr`（什么时候跑）、`Task`（跑什么）、
  `TaskManager`（登记、启停、改定义）；执行循环 `Scheduler` 与它背后的时间线
  `TaskTimeline` 都是内部实现，业务不用管；
- **业务代码只碰进程级单例** `scheduler`（`nacho.core.scheduler` 导出的 `TaskManager()`
  实例）；
- **登记不等于排程**：`add()` 只是记进登记簿，`next_run` 由执行循环算 —— 循环没启动时
  一直是 `None`，`start()` 第一圈才排上；
- **改定义立即生效**：启停 / 换 cron / 换函数都会唤醒循环重排，不靠固定间隔轮询；
- **错过不补**：停机或卡顿期间错过的触发点不补跑，醒来后从 `next_after(now)` 重排；
- **失败隔离**：任务抛异常只更新 `Task` 的失败状态并打一条 error 日志，绝不带崩循环。

## 职责划分

| 模块 | 职责 |
| --- | --- |
| `manager.TaskManager` | 任务登记簿 + 门面：增删改查、`run_now`、`preview`，持有 `Scheduler` |
| `core.Scheduler` | 时间驱动的循环：只认 `enabled=True` 的任务，睡到最近触发点并发派发 |
| `timeline.TaskTimeline` | 红黑树 + 哈希表二合一的排程索引 |
| `cron.CronExpr` | 表达式解析与下一次触发点计算 |
| `models.Task` | 一份任务的定义 + 运行状态 |
| `logging` | 本层的日志接入点 |

## 快速开始

```python
from nacho.core.scheduler import CronExpr, scheduler

await scheduler.start()
scheduler.add("*/5 * * * *", my_check, task_id="check", name="巡检")
scheduler.set_enabled("check", False)          # 停用
scheduler.set_func("check", other_func)        # 换激活的函数
scheduler.remove("check")
await scheduler.stop()                         # 等在飞的任务收尾，不是掐断
```

任务函数**同步协程皆可**：同步函数丢线程池跑（阻塞挡的是线程，不是事件循环），
协程函数直接 `await`。

## cron 表达式

5 段「分 时 日 月 周」或 6 段「秒 分 时 日 月 周」，两种写法可以混用，`__str__` 按原样
还原。5 段永远落在 0 秒；要秒级就在最前面加一段秒（`*/30 * * * * *` = 每 30 秒）。

单个字段内支持的语法：

```
*        任意值
5        单值
a-b      区间
a,b,c    列表（各项还可以是区间）
*/n      步进（全域每 n 个）；也可与区间组合：a-b/n、5/n（从 5 到上限每 n 个）
```

字段范围：秒 0-59、分钟 0-59、小时 0-23、日 1-31、月 1-12、星期 0-6（0 与 7 都是周日）。

语义约定：日 / 周遵循标准 vixie cron —— 两者都被限制（不是 `*`）时取 **OR**，否则取
**AND**。

## 单实例 vs 多实例

- **默认单实例**：上次还没跑完又到触发点，跳过本次并记一条 warning；
- **多实例**（`add(..., multi_instance=True)` 或 `set_multi_instance(id, True)`）：到点
  就开新实例，允许叠加（`task.active` 会 > 1）；
- `run_now()` 是**强制**手动触发：单实例任务正在跑时也照跑，排程不动（连停用的任务
  也照跑）。

## 排程失败的两种

- cron 语法 / 越界错：`add()` 当场抛 `CronError`；
- 语法合法但永远等不到触发点（`0 0 30 2 *` —— 2 月没有 30 号）：只在排程时记一条
  error，`next_run` 保持 `None`，循环照跑。

## 排程索引：红黑树 + 哈希表

`TaskTimeline` 仿 Linux CFS 的组织方式：按触发时间挂进红黑树（内核 `struct rb_root`），
树额外缓存最左节点（`rb_leftmost`），所以「下一个该跑谁」是 O(1)；同时每个节点还被
一张 `task_id -> 节点` 的哈希表引用着，按 ID 定位是 O(1)，改一次排程是 O(log n)。
**一个节点同时挂在两个结构里** —— 既按时间有序，又能按 ID 直接点名。

O(n) 的全量对账只在启动和 `wake()`（外部绕过 `set_*` 直接改了任务字段）时走。

## 日志接入

本层接的是 `nacho.core.logger` 的进程门面，名为 `scheduler`（相对核心 `nacho` ->
`nacho.scheduler`）。**业务模块一律不直接 `default_core()`**：要日志实例就调
`scheduler_logger()`；装配层也可把实例经 `TaskManager(..., logger=)` /
`Scheduler(..., logger=)` 传入。

核心由装配层（`nacho.bootstrap` 或 `nacho.wiring.wire_loggers`）经 `set_core()` 存进
本模块槽位。`import` 本模块**零副作用** —— 没装配就调 `scheduler_logger()` 会当场抛错
（fail fast），不会默默按默认参数建一份把配置定死的核心。

## 完整演示

`examples/scheduler_demo.py`（约 15 秒，演示的是秒级 cron）：登记不等于排程、排程失败
的两种、`preview()` 预览、同步 / 协程任务、单 / 多实例、失败隔离、改定义立刻生效、
手动触发、停机与重启。

```bash
python examples/scheduler_demo.py
```
