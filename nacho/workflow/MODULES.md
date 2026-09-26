# nacho.workflow 模块索引（MODULE MAP）

> 本文件是 `nacho/workflow/` 下**逐文件 → 作用**的速查索引。详细的「为什么」写在每个 `.py` 的
> 模块 docstring 里（`:mod:` 交叉引用），本文件只做「一眼定位」。
>
> **要写自己的节点，直接跳第 5 节**（完整指南：契约、注册即校验、可选依赖、测试写法）。
>
> 同步规则：新增 / 重命名 / 删除文件时，记得更新这里。

---

## 0. 分层总览

```
nacho/workflow/
├── models.py        图（节点 / 边）、校验报告、落库记录、规范 JSON / 摘要
├── validator.py     入库前校验：结构 → 拓扑 → 语义（④ Dry Run 只有阶段名，未接）
├── store.py         落库：定义 / 版本两张表（SQLModel + AsyncSession），按归属隔离
├── nodes/           ★ 节点执行器：一类节点一个文件 + 注册表（**写自己的节点看这里**）
│   ├── base.py          契约：NodeExecutor / NodeSpec / ConfigField / PortSpec / NodeExecutionContext
│   ├── registry.py      注册表：register_node / declare_node_type / get_spec / load_node_modules
│   ├── start.py         内置：start（图起点；trigger=time 时按 cron 登记调度器）
│   ├── end.py           内置：end（图终点）
│   ├── log.py           内置：log（按级别写业务日志；内容从 message 入口来）
│   ├── test.py          内置：test（回显，画布联调用）
│   ├── constant.py      内置：constant（一个节点一个常量值，从 value 出口送下去）
│   └── http.py          内置：http（发一次 HTTP 请求；需要可选依赖 httpx）
├── graph.py         图的小工具：出边索引 / 可达集合 / 入口节点 / 边端口（校验器与运行器共用）
├── executor.py      运行器：只跑 start 可达的主流程，按拓扑顺序执行 + **按边投递数据**
└── runtime.py       运行时：启动只给**开着运行开关**的已发布流登记定时触发（不执行图）；
                     到点后加载该版本跑整条流程；拨开关即时启停（WorkflowTriggers）
```

**依赖方向（单向、无环）**：

```
executor / runtime ──► nodes ──► models
validator ──────────► nodes.registry / nodes.base（读注册规格：角色、字段、校验器）
store ──────────────► models
```

- `nodes/` 只依赖 `models`：**节点不需要知道**运行器、校验器、存储的存在；
- 运行器按**类型名**从注册表取执行函数，不认识任何具体节点；
- 本包不 import FastAPI（HTTP 入口在 `nacho/api/api/workflow/`，装配在 `nacho/api/app.py`）。

---

## 1. models.py —— 图与数据协议

| 名字 | 作用 |
|---|---|
| `WorkflowNode` | 一个节点：`id`（图内唯一）/ `type`（合法值 = 注册表里已登记的类型）/ `config` / 画布坐标 `x`·`y` |
| `WorkflowEdge` | 一条有向边：`source` 的**输出端口** → `target` 的**输入端口**，值就沿它流 |
| `WorkflowGraph` | 一张图：`nodes` + `edges`，入库前校验与版本快照装的都是它 |
| `ValidationIssue` / `ValidationReport` | 校验结果：`node_id` + `code` + 人话 + 建议；失败会带上卡在哪个 `stage` |
| `STAGE_*` | 四个阶段名（`structure` / `topology` / `semantic` / `dry_run`） |
| `WorkflowDefinitionRecord` / `WorkflowVersionRecord` | 落库记录（定义 / 版本快照） |
| `canonical_graph_json` / `graph_checksum` | 规范 JSON 与它的 sha256（内容没变就不产生新版本） |

> **数据沿连线走，没有全局变量**：节点从自己的**输入端口**拿到上游送来的值（引擎按边投递，
> 键 = 目标端口名），把产出放在**输出端口**上（执行函数返回值的键 = 端口 id）。
> `message` 类型端口送值、`trigger` 类型端口只表达先后；边两端端口类型必须相同（见第 5.2 / 5.6 节）。
>
> 边没写端口时按 `trigger` 读（`graph.DEFAULT_EDGE_PORT`）：这类边只表达顺序、不送值。

## 2. validator.py —— 入库前三个阶段（④ Dry Run 还没接）

| 阶段 | 查什么 | 典型错误码 |
|---|---|---|
| ① 结构 | 能不能解析成图、节点 id 唯一、边端点存在、主流程上的类型都已注册（孤儿类型不查） | `UNKNOWN_NODE_TYPE` |
| ② 拓扑 | **只看 start 可达的主流程**：start 唯一、至少一个可达 end、无环（Kahn）、注册的出入边约束（分流类节点 ≥2 出边、end 无出边） | `START_NOT_UNIQUE` / `END_MISSING` / `CYCLE_DETECTED` 等 |
| ③ 语义 | 注册字段必填、节点自注册校验器（trigger/cron、log level、http method）、**连线**（端口存在 / 两端同类 / 必填入口接上没）、表达式语法；**全部只查主流程节点** | `MISSING_CONFIG` / `INPUT_NOT_CONNECTED` / `UNKNOWN_PORT` / `PORT_TYPE_MISMATCH` / `DUPLICATE_INPUT_EDGE` / `INVALID_TRIGGER` / `INVALID_CRON` / `INVALID_LOG_LEVEL` / `INVALID_HTTP_METHOD` |
| ④ Dry Run | **还没接**：只留了阶段名常量 `STAGE_DRY_RUN`，等执行引擎就位再加 | —— |

> **短路**：某一阶段出错就不再往后跑 —— 结构都不对，拓扑 / 语义无从谈起。
> 类型专属规则不在校验器里写分支：每个节点在注册时挂自己的校验器（第 5.6 节）。
>
> **孤儿节点永远合法**：从 start 不可达的节点（散点、独立小图、内部带环的组件）不产生
> 任何错误、不参与语义检查，运行器也不执行它们——「用不到，但确实可以保存」。

## 3. store.py —— 落库

| 名字 | 作用 |
|---|---|
| `WorkflowDefinitionTable` | `workflow_definitions`：一个工作流一行（元数据 + 版本指针 + **运行开关** `enabled`），`UNIQUE(owner_id, name)` |
| `WorkflowVersionTable` | `workflow_versions`：每次保存一张**不可变**图快照，`UNIQUE(workflow_id, version)` |
| `SqlWorkflowStore` | 读写实现：查询走 `AsyncSession`（不手写 SQL；`ensure_schema` 里那一句 `ALTER TABLE` 是给老库补列的迁移，属例外）；按 `owner_id` 隔离 |
| `WorkflowError` / `WorkflowNameConflict` | 存储层错误（消息直接给人看） |

> **发布 ≠ 运行**：发布只挪发布指针（`status=published` + `published_version`），**不执行图**；
> 要不要真的跑由 `enabled`（运行开关，默认 `False`）说了算 —— 它是**库里的字段**，重启 / 多进程
> 认的是同一份。老库升级时这一列按 `0` 补（见 `_DEFINITION_ADDED_COLUMNS`），不会因为多了个
> 开关就突然开始跑。开关怎么拨（接口 / 隔离 / 即时启停）见 `nacho/api/api/workflow/`。

引擎由外部注入（同用户 / 会话 / 令牌存储的惯例），本模块不建引擎、不读配置。

## 4. nodes/ —— 节点执行器（一类一文件）

| 文件 | 作用 | 端口（输入 → 输出） | config（必填项加粗） |
|---|---|---|---|
| `base.py` | **契约**：`NodeExecutor` / `NodeSpec` / `ConfigField` / `PortSpec` / `NodeExecutionContext` / `input_value`。`NodeSpec` 除校验规则外还带**展示信息**（`label` / `order` / `inputs` / `outputs`）——画布照它渲染，见 §5.6 ⑥ | —— | —— |
| `registry.py` | **注册表**：`register_node` / `declare_node_type` / `get_spec` / `get_executor` / `registered_types` / `load_node_modules` | —— | —— |
| `start.py` | 图起点（`role="start"`）；`trigger=time` 时把整条流程按 cron 登记到调度器 | — → `trigger` / `message` | `trigger`（缺省 `message`，注册默认值）、`cron`（time 触发必填，自注册校验器）、`name` |
| `end.py` | 图终点（`role="end"`，`max_outgoing=0`）：写一条完成日志 | `trigger` → — | —— |
| `log.py` | 按级别写业务日志；内容从 `message` 入口来 | `trigger` / `message` → `trigger` | `message`（没接线时手填）、`level`（缺省 INFO，注册默认值；枚举由自注册校验器把） |
| `test.py` | 回显（画布联调）：把入口的值原样从出口送下去，夹在中间看「线上流过了什么」 | `trigger` / `message` → `trigger` / `message` | `message`（缺省 `hello`） |
| `constant.py` | **常量**：一个节点一个值，从 `value` 出口送下去 | `trigger` → `trigger` / `value` | **`value`**（必填，没有默认值） |
| `http.py` | 发一次 HTTP 请求 | `trigger` / `url` / `body` → `trigger` / `http_status` / `http_body` | `url`（**入口**必填：接线或手填）、**`method`**（枚举由自注册校验器把）、`body`（没接线时手填）、`timeout`（缺省 10，注册默认值）、`headers`（只能手写，没有对应端口） |

> **「入口」= 字段名与端口 id 同名的那个数据端口**：`log.message` / `http.url` / `http.body` 都能
> 被连线覆盖 —— **线上的值优先，没接线才用 config 里手填的**（`input_value` 就是这个口径）。
> 标了 `required=True` 的入口必须「接线或手填」，否则语义阶段报 `INPUT_NOT_CONNECTED`。
>
> **字面量尽量走常量节点**：地址、模板、固定文案这类值写在 `constant` 节点上，谁要用就连一根线
> 过来 —— 别把同一串值复制进每个节点的 config（改一次要翻整张图）。要几个常量就摆几个节点；常量
> 节点必须在 start 可达的主流程里（孤儿不执行，它的线也就没人送值）。

**注册表是进程级、内存里的一张表**（`registry._SPECS`，类型名 → `NodeSpec`），不落库、没有配置文件：

```python
_SPECS: dict[str, NodeSpec] = {}

def register_node(node_type, *, fields=(), validator=None, role="normal",
                  min_outgoing=0, max_outgoing=None, expression_field=None): ...  # 装饰器
def declare_node_type(node_type, *, fields=(), ...): ...   # 只登记规则、执行器留空
def get_spec(node_type) -> NodeSpec | None: ...            # 校验器读的就是它
def get_executor(node_type) -> NodeExecutor | None: ...    # 没注册（或只声明）给 None
def registered_types() -> tuple[str, ...]: ...             # 排查「注册上没有」
def load_node_modules(*module_names) -> list[str]: ...     # 装外部模块
```

包一被 import，各节点模块就自己登记一次（`nodes/__init__.py` 逐个 import 内置节点）。
所以「注册一个节点」= **让那个模块被 import 到**。

## 5. 写自己的节点（开发者指南）★

### 5.1 三步，不用改框架文件

```python
# ① 新建一个模块（照 nacho/workflow/nodes/log.py 的样子；一个节点一个文件）
#    my_pkg/nodes/dingtalk.py
from nacho.workflow.nodes import (
    ConfigField, NodeExecutionContext, PortSpec, input_value, register_node,
)

@register_node(
    "dingtalk",
    # ② 当场注册：执行函数 + 端口 + 校验规则一起声明，校验器 / 模型 / 前端框架代码都不用动
    inputs=[
        # 数据入口：上游把值接到 text；required 表示「必须接线或手填同名字段」
        PortSpec("text", "message", "消息内容", required=True),
    ],
    outputs=[PortSpec("sent", "message", "是否发出")],
    fields=[
        ConfigField("text", "消息内容"),                  # 没接线时的手填兜底
        ConfigField("format", "格式", default="text"),     # 缺失 → 保存时自动补 text
    ],
)
async def exec_dingtalk(node, ctx: NodeExecutionContext) -> dict[str, object]:
    text = str(input_value(node, ctx, "text"))         # 线上来的优先，没接线才用手填值
    ctx.logger.info("发钉钉消息", node_id=node.id, text=text)
    ctx.log.append(f"[dingtalk] {node.id}: {text}")
    return {"sent": True}                              # 键 = 输出端口名
```

```python
# ③ 启动时装进来（app.py 或你自己的入口），一行
from nacho.workflow import load_node_modules
load_node_modules("my_pkg.nodes.dingtalk")
```

`load_node_modules` 只是替你 `import`：**重复加载幂等**（命中 `sys.modules`），模块 import
失败**当场抛** —— 别把「节点没注册上」藏到跑图时才报「暂无执行器」。想核验：

```python
from nacho.workflow import get_executor, registered_types
assert get_executor("dingtalk") is not None
print(registered_types())        # 已注册的类型名（排序）
```

> 也可以不用装饰器，运行时手工登记：`register_executor("dingtalk", exec_dingtalk)`。
> **重复注册是覆盖**，测试里换实现就靠这个。

### 5.2 契约：收什么、回什么

```python
NodeExecutor = Callable[[WorkflowNode, NodeExecutionContext], Awaitable[dict[str, Any]]]
```

- 入参：节点本身（`id` / `type` / `config`）+ 运行时上下文；
- 返回：**本节点产出的值**（`dict`，**键 = 已声明的输出端口名**）。引擎按边把它投递给下游的
  对应入口；多出来的键不会被投递（`start` 的 `scheduled` / `task_id` 就是这种「只给日志看」的
  信息）。没有产出就返回 `{}`（像 `log` / `end` 那样）。
- 执行是**串行**的（节点之间有数据依赖）；并行 / 分支语义留给将来新增的分流类节点。

### 5.3 上下文 `NodeExecutionContext` 能给什么

| 成员 | 是什么 | 用来干嘛 |
|---|---|---|
| `ctx.inputs` | `dict[str, Any]`，**引擎按入边投递进来的值**（键 = 目标端口名） | 用 `input_value(node, ctx, "名字")` 取；测试里直接 `ctx.inputs["x"] = ...` 预置。**上游没执行过的边不算数**（孤儿连出来的线不送值，`input_value` 回落到同名字段的手填值）；上游跑了但那个出口没产出才送空串 |
| `ctx.trigger_data` | `dict[str, Any]`，消息触发时外面送进来的数据 | `start` 的 `message` 出口从它取（`ctx.trigger_data["message"]`） |
| `ctx.logger` | `BaseLogger`（`nacho.core.logger`） | 写业务日志（节点自己的运行痕迹） |
| `ctx.log` | `list[str]` | 节点产出的文字行（给前端回显 / 测试断言，不落日志文件） |
| `ctx.scheduler` | `TaskManager \| None` | 要把流程挂到 cron 就用它（`start` 的 `trigger=time` 的做法）；没注入时是 `None` |
| `ctx.run_workflow()` | `async` 回调 | 触发整条流程（cron 到点时调它） |

`input_value(node, ctx, name, default="")`：取某个数据入口的值 —— **线上的值优先，没接线才用
config 里同名字段的手填值**，两者都没有才用 `default`。这是「字段名 = 端口名」那条约定的唯一
实现处，节点不用自己判断有没有接线。

节点拿到的**只有指向自己的那些边送来的值**：别的节点产出什么它看不见（没有全局变量上下文）。

### 5.4 端口怎么设计

- **端口名就是对外契约**：它写进 edge 的 `source_port` / `target_port`，也是执行函数返回值的
  键名 —— 改端口等于改「这张图还能不能跑」（以前端口只是画布上的装饰）；
- 输出端口用**带节点前缀**的名字（`http_status` / `http_body`、`dingtalk_sent`），别用 `result`、
  `data` 这种通用词：端口虽然是每个节点自己一份（不会互相覆盖），但下游连线时要一眼看出线上是什么；
- 数据入口用**普通名词**（`url` / `body` / `message`），并给同名字段留个手填兜底：画布会把
  「已接线 / 未接线」标出来，校验器只在「既没接线也没填」时才报 `INPUT_NOT_CONNECTED`；
- **一个数据入口只允许一条入边**（`DUPLICATE_INPUT_EDGE`）：要合并多个上游，就先各自接到一个
  中间节点，再从那一个节点往下送。

### 5.5 失败怎么处理：分两类

| 情况 | 怎么办 | 例子 |
|---|---|---|
| **业务结果**（对方回了错、查不到、校验不过） | 记日志（`ctx.logger.warning`）+ 正常返回，让流程继续往下走 | `http` 节点的 4xx / 5xx |
| **环境问题**（连不上、超时、配置写错、依赖没装） | 直接 `raise`：整条流程失败并留下堆栈，别伪装成「成功但没内容」 | `http` 节点连不上、`url` 入口没接线也没填 |

跑图的失败长这样（`executor.py`）：执行函数一抛，`SimpleWorkflowRunner.run` 就中断，
日志里那条异常带着堆栈 —— 比「跑完了但什么都没发生」好查得多。

### 5.6 校验那一关：注册什么，就校验什么

校验器（`validator.py`）**没有任何具体节点类型的知识**：它只从注册表读每个类型的
`NodeSpec`，按规格办事。新增类型不用动 `validator.py` / `models.py` 一行，也**不用动画布**
（画布从节点目录接口读，见 ⑥）。

**① 端口与 config 字段，都在注册处声明**：

| 声明 | 写法 | 语义 |
|---|---|---|
| 数据入口 | `inputs=[PortSpec("url", "message", "请求地址", required=True)]` | 上游把值接进来；`required` = 「必须接线或手填同名字段」，否则 `INPUT_NOT_CONNECTED` |
| 控制流端口 | `PortSpec("trigger", "trigger", "触发")`（内置节点用常量 `TRIGGER_PORT`） | 只表达先后，不送值；多条入边允许（汇聚） |
| 数据出口 | `outputs=[PortSpec("http_status", "message", "状态码")]` | 执行函数返回值的键；下游把线接过来才拿得到 |
| 出口 / 入口与字段同名 | `ConfigField("url", "请求地址")` 配 `PortSpec("url", ...)` | 该字段可被连线覆盖：**线上优先**，没接线才用手填 |
| 不可缺失字段 | `ConfigField("url", required=True)` | 主流程上的节点直接报 `MISSING_CONFIG`（`None` / 空串也算缺失） |
| 默认值字段 | `ConfigField("level", default="INFO")` | 校验前先补默认值（自定义校验器看到的是补全后的 config）；保存版本时写进快照 |
| 枚举字段 | `ConfigField("level", default="INFO", options=LOG_LEVEL_ORDER)` | 同上；`options` 只描述「有哪些可选值、按什么顺序显示」（画布渲染成下拉），校验仍归自定义校验器 |

连线本身的规则（端口存不存在 `UNKNOWN_PORT`、两端同不同类 `PORT_TYPE_MISMATCH`、数据入口只接
一条线 `DUPLICATE_INPUT_EDGE`）由校验器统一查，**不用自己写**。

> 一个类型**完全没声明端口**时（只 `declare_node_type` 占位的扩展节点），校验不查它的那一端 ——
> 没声明就谈不上「端口名对不对」；声明了才查。

未声明的字段一律不查，原样留在 config 里。

**② 类型专属规则挂自定义校验器**（普通必填 / 默认表达不了的，比如枚举、跨字段条件）：

```python
def validate_dingtalk(node: WorkflowNode) -> list[ValidationIssue]:
    if node.config.get("format") not in ("text", "markdown"):
        return [ValidationIssue(node_id=node.id, code="BAD_FORMAT",
                                message="format 只认 text/markdown")]
    return []

@register_node("dingtalk", fields=[...], validator=validate_dingtalk)
async def exec_dingtalk(node, ctx): ...
```

校验器收到的是**补完默认值**的节点，只负责返回 issue 列表（空列表 = 通过），
错误码自定义（照 `http.py` 的 `INVALID_HTTP_METHOD`、`start.py` 的 `INVALID_CRON` 抄）。

**③ 拓扑角色与出入边约束也在注册处声明**：`role="start"|"end"|"normal"`、
`min_outgoing` / `max_outgoing`、`expression_field`（指定哪个字段按表达式做语法检查）。
比如分流类节点用 `min_outgoing=2` 表达「至少两个分支」、`end` 用 `max_outgoing=0` 表达
「不能有出边」，都是通用约束，没有特判代码。

**④ 只声明、不实现：`declare_node_type`**。执行器还没写、但希望类型已经能进画布、
能保存、能被校验时，只登记规格。**内置节点里已经没有这种类型**（都在自己文件里带执行器）；
这一条留给扩展方：先占位，以后再补一个 `register_node` 覆盖掉即可。这种类型真被主流程
跑到时，运行器按老规矩报「暂无执行器」。

**⑤ 孤儿节点**：从 start 不可达的节点**一律放行**——类型未注册、config 缺失、自带环都不报错，
保存可以、运行不跑。所以字段规则只对主流程（start 可达）上的节点生效。

**⑥ 画布不自己定义节点**：编辑器启动时拉一次节点目录
（`GET <prefix>/workflows/node-types`，见 `api/workflow/router.py`），**面板项 / 中文名 /
端口 / 配置表单全按注册表渲染** —— 加一个节点类型只改后端这一个文件，画布与接口都不用动。

前端只留一样东西：**颜色**（皮肤，后端不管；认不出的类型用灰的）。

还有一处**固有例外**（形状本来就随 config 变，不是「前端另有定义」）：`start` 的端口与卡片上
的字段条随 `config.trigger` 变（时间触发的图里不显示 `message` 出口）。

> 那份目录就是「两边对得上」的契约：面板上摆的必然是后端登记过的类型。万一旧图里还有认不出
> 的类型，画布画成灰色未知节点，保存时被 `UNKNOWN_NODE_TYPE` 挡下 —— 以前那批只有声明没有
> 执行器的类型（gateway / approval / expression / condition / task）已前后端一起删掉。

### 5.7 带可选依赖的节点（照 `http.py` 抄）

节点要用的第三方库如果不是框架的必装项，**别在模块顶层 import**（那会让整个 `nodes` 包 import 失败）：

```python
def _import_httpx() -> Any:
    try:
        import httpx
    except ImportError as exc:
        hint = 'HTTP 节点需要 httpx：pip install httpx（或 pip install "nacho[workflow]"）'
        raise RuntimeError(hint) from exc
    return httpx
```

再在 `pyproject.toml` 的 `[project.optional-dependencies]` 里加一个按能力命名的 extra
（`workflow = ["httpx>=0.27"]`），并在模块 docstring 里写明「不装只影响这个节点」。

### 5.8 测试怎么写

```python
@pytest.mark.asyncio
async def test_my_node_outputs(...) -> None:
    node = WorkflowNode(                          # type 已是自由字符串，正常构造即可
        id="d1", type="dingtalk", config={}       # text 走连线，不写在 config 里
    )
    ctx = NodeExecutionContext()
    ctx.inputs = {"text": "hi nacho"}             # 引擎投递进来的入口值（测试直接预置）
    assert await exec_dingtalk(node, ctx) == {"sent": True}
```

- **直接调函数**，不必为了测节点去拼一张图（要验「值真的沿边走」再拼 `WorkflowGraph` +
  `SimpleWorkflowRunner`，见 `tests/test_workflow.py` 里的 `test_http_status_reaches_*`）；
- 对外部 IO **打桩**，别走真实网络：`monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)`
  （`tests/test_workflow.py` 里的 `FakeAsyncClient` 就是这个套路），本项目的惯例是打桩而不是起服务；
- 注册本身也值得测：`assert get_executor("dingtalk") is exec_dingtalk`、
  `load_node_modules("你的模块")` 幂等、模块不存在时抛 `ModuleNotFoundError`。

### 5.9 上线前自查

- [ ] 类型在模块里注册上了（`get_spec("类型") is not None`；有执行函数再查 `get_executor`）
- [ ] 端口声明齐了：数据入口 / 出口都是 `"message"` 类型，控制流用 `TRIGGER_PORT`；必填入口标 `required=True`
- [ ] 返回值键 = 输出端口 id；取入口值用 `input_value`（别直接读 `ctx.inputs`，那会绕过「没接线用手填」的兜底）
- [ ] config 字段规则在注册处声明齐了：必填的 `required=True`，有缺省的给 `default`
- [ ] 类型专属校验（可选）：注册时挂 `validator`，配置写错在保存时就报
- [ ] 需要的拓扑约束：`role` / `min_outgoing` / `max_outgoing` / `expression_field`
- [ ] 画布**不用改**：`label` / `order` / 端口 / `fields` 声明全了，节点就自动出现在面板上
- [ ] 环境问题会抛、业务结果会返回（第 5.5 节）
- [ ] 有单测，且外部依赖是打桩的

## 6. 要加的东西放哪

| 要加的东西 | 放哪 |
|---|---|
| 新节点类型 | `nodes/<类型>.py`（一类一个文件，`@register_node` 一次声明执行器 + 字段 + 校验规则，第 5.6 节）；只有规则没有执行器用 `declare_node_type` |
| 节点类型专属的校验规则 | 该节点模块里写校验函数，注册时挂 `validator=`（不动 `validator.py`） |
| 通用的图层面校验（新的拓扑规则 / 新阶段） | `validator.py`（只放跨类型、与具体节点无关的规则） |
| 图 / 记录上要加字段 | `models.py`（协议）+ `store.py`（表结构） |
| 新的 HTTP 接口 | `nacho/api/api/workflow/`（入口层，路由 + 请求 / 响应 schema） |
| 新的执行语义（并发 / 分支 / 重试） | `executor.py`（现在的 `SimpleWorkflowRunner` 是串行版；换引擎就换这个类，调用方只认 `run()`） |
| 图算法（可达集合 / 拓扑遍历 / 找入口） | `graph.py`（校验器与运行器共用一份，**别再各写一份 BFS**） |
| 发布 / 触发链路 | `runtime.py`（启动 `load_published_workflows` 只登记**开着开关**的；`WorkflowTriggers.start/stop` 给接口层即时启停；到点 `make_trigger` → `run_published_workflow` 跑整条流程） |
