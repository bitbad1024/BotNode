# 异步日志系统（`nacho.core.logger`）

## 设计要点

- **一根 root**：出口长在 `LogCore`（也就是 `BaseLogger`，前者是别名）身上，写日志默认推到消息队列；
- **实例化即带一路控制台输出**，文件 / 数据库等出口在运行时按需 `mount` 上去，挂载后自动启动，无需手动启动处理机；
- **分发器从队列批量取日志，照着每条记录自带的目标扇出**给处理机，处理机异常被完全隔离，单点崩溃不会影响业务与其它处理机；
- **业务侧拿到的要么是 `bind` 出来的上下文视图，要么是 `child` 出来的命名层级节点**：`child` 在派生那一刻固化一份配置（缓存树，同名返回同一对象），要新配置就再 `child` 一层；
- **过滤挂在目标上**（`Target.log_filter`）：一条日志只有通过某个目标的过滤器才会被投递给它，被过滤掉的日志连处理机的缓冲区都不进 —— 处理机只负责落地，不含任何过滤器。

## 职责划分

- **`write` 是写入方法**：默认把日志推到消息队列（非阻塞），业务侧永远不会因为落盘 / 落库而卡住；
- **`bind` 是绑定**：得到一份 `BoundLogger` 视图，名字、出口、级别、默认字段四样都能换（见下文「绑定」）；
- **内部分发器**从队列批量取日志，照着每条记录自带的目标扇出给各处理机，处理机异常被隔离；
- **`flush` 刷新缓冲区**、**`search` 检索**：都聚合各处理机的结果（见「检索与刷新」）。

## 两级缓冲：交接 vs 攒批

- **`AsyncLogQueue`（交接）**：同步写入方与异步分发器之间的边界。`dispatch_timeout` 只决定「一次最多等多久把手上这批取走」，是**交接窗口**而非攒批窗口，所以取小值，让日志尽快交到处理机；
- **处理机缓冲区（攒批）**：每个出口自己的批大小与刷盘周期（`buffer_size` / `flush_interval`）。控制台可以 `buffer_size=1` 逐条直写，文件 / 数据库则成批落，互不牵制。

## 快速开始（最小化启动 + 增量挂载）

```python
from nacho.core.logger import LogCore, LocalFileLogProcessor

logger = LogCore()                      # 只有控制台，立即可用
await logger.start()
logger.info("机器人已启动", robot_id="r-001")

logger.mount(LocalFileLogProcessor("logs", prefix="nacho"))   # 运行期挂载（按天分片）
await logger.stop()                     # 停机自动冲刷余量
```

「片」= 该前缀当天的分片文件（`<前缀>-<日期>[.<序号>].log`）。

## 具名路由（route）

发布过一条具名路由之后，这条路上的日志只投它自己那一份：

```python
core = LogCore("nacho")                                        # root = [console]
core.mount(LocalFileLogProcessor("logs", prefix="nacho"))      # root 的全量文件出口

core.route("robot", targets=[LocalFileLogProcessor("logs", prefix="robot")])
core.route("robot.arm", targets=[LocalFileLogProcessor("logs", prefix="arm")])

core.route("robot").info("就绪")        # -> 只投 robot 那份
core.route("robot.arm").info("过载")     # -> 只投 arm 那份
core.route("vision").info("没发布过")    # -> 跟着 root：console + root 那份
```

名字**相对 root**（`"robot"` 即 `"nacho.robot"`），按名字发布过就一直返回同一份视图，不需要「先挂载、再取实例」。

## 命名层级（child）

`child` 派生**缓存树节点**（`ChildLogger`）：派生那一刻就把生效的级别 / 目标**解析并固化**进新节点，之后读 `level` / `targets` 不再沿父链向上找；同名 `child` 命中缓存返回**同一个对象**。

```python
api = core.child("api")            # 固化 core 当时的级别与目标
core.child("api.robot")            # 或 api.child("robot")，名字相对本节点拼接
api.info("只投 api 那份")           # 用固化下来的那一份目标
```

- **创建时固化**：父级后挂出口、改级别对**已派生**节点不生效 —— 要新配置就再 `child` 一层；
- **只投固化下来的那一份目标**，不沿父链重复投递（无 propagate）；
- `child` 换的是**名字与出口**（名字即身份，可继续 `child` 生长）；`bind` 换的是**默认字段**（产物只能 `bind` / `log` / `mute`，不能再 `child`）。

## 绑定（bind）

`bind` 返回一个 `BoundLogger` 视图，四项随用随给 —— 给了就覆盖，没给就沿用来源那份：

- `targets`：**这条路上每条日志投给谁**。元素可以是 `Target`（能挂过滤器与投放优先级），也可以直接是处理机（等价于 `Target(processor)`：全收、优先级 0）。目标写进 `LogRecord.targets` 跟着记录走，于是**分发不再需要「名字 -> 实例」那张表**；里面新出现的处理机会被自动接纳，停机照样 flush、`search` / `stats` 照样看得到；
- `name`：写进 `record.logger_name` 的**标签**（检索按它精确匹配），不再是身份；
- `level`：本视图的最低级别；
- 其余关键字参数就是默认字段，当次传的同名键压过默认的：

```python
log = core.bind(
    name="nacho.api.access",
    targets=[Target(file), Target(db, priority=-1)],   # 目标随记录走，db 先投
    trace_id="t-1",
)
log.info("一条", owner_id=uid)      # extra 带 trace_id；owner_id 是一等字段
log.info("换个归属", owner_id="u-2")
```

一趟工作流、一次请求打一次标记，后面每个调用点只管写自己那句话，日志自己认得出是谁的。`owner_id` 是日志的一等字段（不塞 `extra`，能按它精确检索），同样可以绑默认值。

**视图只读、且不登记**（详见 `BoundLogger`）：再 `bind` 一层只产出新视图、原视图不变，所以一份视图能被多个协程同时拿着写；也正因为不登记，**请求级的值别拿它当实例用** —— `user_id` 这类每次都变的东西要么当次传参（成本就是一个关键字参数），要么随请求生命周期现绑现扔。

## 运行期挂载与过滤器

```python
from nacho.core.logger import Target

core.mount(LocalFileLogProcessor("logs", prefix="robot"),
           level="WARNING")                                  # 挂一个：只收 WARNING 及以上
core.mount(Target(outlet, level="ERROR", priority=-1))       # 带优先级
# 级别门槛挂在目标一侧：被筛掉的日志连处理机的缓冲区都不进
```

## 进程门面（业务代码通常只用这三个）

```python
from nacho.core.logger import configure, get_logger, mount_module

configure(level="INFO")                  # 建立（或复用）进程默认核心（名 nacho）
mount_module("api.robot", LocalFileLogProcessor("logs", prefix="api"))   # 名字相对核心
get_logger("api.robot").info("收到请求")   # 一条具名绑定，与核心共享同一个队列
```

## 内省与排查（随时可查，只读）

```python
logger.processors                         # root 的默认目标里的处理机
logger.targets                            # 默认目标（处理机 + 过滤器 + 优先级）
logger.named_routes                       # 发布过的具名路由（名字 -> 视图）
logger.stats                              # 队列 / 处理机 / 丢弃条数汇总
```

## 检索与刷新

```python
await logger.flush()                           # 刷所有出口的缓冲区
found = await logger.search(level="ERROR", limit=20)
# 聚合各出口，按插入顺序（落库那份的自增 seq）倒序并按 record_id 去重；
# found.records 是本页，found.total 是命中总数
await logger.search(owner_id="u-admin")        # 只看某个人名下的日志（不填 = 谁都不限）
```
