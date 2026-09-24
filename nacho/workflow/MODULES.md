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
├── validator.py     入库前校验：结构 → 拓扑 → 语义（④ Dry Run 留协议位）
├── store.py         落库：定义 / 版本两张表（SQLModel + AsyncSession），按归属隔离
├── nodes/           ★ 节点执行器：一类节点一个文件 + 注册表（**写自己的节点看这里**）
│   ├── base.py          契约：NodeExecutor / NodeSpec / ConfigField / NodeExecutionContext
│   ├── registry.py      注册表：register_node / declare_node_type / get_spec / load_node_modules
│   ├── start.py         内置：start（图起点；trigger=time 时按 cron 登记调度器）
│   ├── end.py           内置：end（图终点）
│   ├── log.py           内置：log（按级别写业务日志）
│   ├── test.py          内置：test（回显，画布联调用）
│   ├── constant.py      内置：constant（常量：一组「名字 -> 值」，下游连线后 {{引用}}）
│   ├── http.py          内置：http（发一次 HTTP 请求；需要可选依赖 httpx）
│   ├── declared.py      占位：gateway/approval/expression/condition/task（规则已登记，执行器未实现）
│   └── time_trigger.py  兼容：旧版 time-trigger 类型（历史版本快照），行为同 start 的时间触发
├── executor.py      运行器：只跑 start 可达的主流程，按拓扑顺序执行（SimpleWorkflowRunner）
└── runtime.py       运行时：加载已发布版本的图并执行（时间触发的 start 到点后走它）
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
| `WorkflowNode` | 一个节点：`id`（图内唯一）/ `type`（合法值 = 注册表里已登记的类型）/ `config` / `outputs`（声明的输出变量名） |
| `WorkflowEdge` | 一条有向边：`source` → `target`（也是变量作用域的传播方向） |
| `WorkflowGraph` | 一张图：`nodes` + `edges`，入库前校验与版本快照装的都是它 |
| `ValidationIssue` / `ValidationReport` | 校验结果：`node_id` + `code` + 人话 + 建议；失败会带上卡在哪个 `stage` |
| `STAGE_*` | 四个阶段名（`structure` / `topology` / `semantic` / `dry_run`） |
| `WorkflowDefinitionRecord` / `WorkflowVersionRecord` | 落库记录（定义 / 版本快照） |
| `canonical_graph_json` / `graph_checksum` | 规范 JSON 与它的 sha256（内容没变就不产生新版本） |

> **`outputs` 是「声明」不是「产出」**：它只用来让校验器知道「下游的 `{{名字}}` 有没有人声明」。
> 节点真正产出什么，由执行函数**返回的 dict** 决定（见第 5.2 节）——两者要对得上。

## 2. validator.py —— 入库前四阶段

| 阶段 | 查什么 | 典型错误码 |
|---|---|---|
| ① 结构 | 能不能解析成图、节点 id 唯一、边端点存在、主流程上的类型都已注册（孤儿类型不查） | `UNKNOWN_NODE_TYPE` |
| ② 拓扑 | **只看 start 可达的主流程**：start 唯一、至少一个可达 end、无环（Kahn）、注册的出入边约束（gateway≥2 出边、end 无出边） | `START_NOT_UNIQUE` / `END_MISSING` / `CYCLE_DETECTED` 等 |
| ③ 语义 | 注册字段必填、节点自注册校验器（trigger/cron、log level、http method）、变量作用域、表达式语法；**全部只查主流程节点** | `MISSING_CONFIG` / `INVALID_TRIGGER` / `INVALID_CRON` / `INVALID_LOG_LEVEL` / `INVALID_HTTP_METHOD` |
| ④ Dry Run | 预留协议位（`DryRunner` / `ExpressionSyntaxChecker`），当前默认全放行 | —— |

> **短路**：某一阶段出错就不再往后跑 —— 结构都不对，拓扑 / 语义无从谈起。
> 类型专属规则不在校验器里写分支：每个节点在注册时挂自己的校验器（第 5.6 节）。
>
> **孤儿节点永远合法**：从 start 不可达的节点（散点、独立小图、内部带环的组件）不产生
> 任何错误、不参与语义检查，运行器也不执行它们——「用不到，但确实可以保存」。

## 3. store.py —— 落库

| 名字 | 作用 |
|---|---|
| `WorkflowDefinitionTable` | `workflow_definitions`：一个工作流一行（元数据 + 版本指针），`UNIQUE(owner_id, name)` |
| `WorkflowVersionTable` | `workflow_versions`：每次保存一张**不可变**图快照，`UNIQUE(workflow_id, version)` |
| `SqlWorkflowStore` | 读写实现：`AsyncSession`，不写一行 SQL；按 `owner_id` 隔离 |
| `WorkflowError` / `WorkflowNameConflict` | 存储层错误（消息直接给人看） |

引擎由外部注入（同用户 / 会话 / 令牌存储的惯例），本模块不建引擎、不读配置。

## 4. nodes/ —— 节点执行器（一类一文件）

| 文件 | 作用 | config（必填项加粗） |
|---|---|---|
| `base.py` | **契约**：`NodeExecutor` / `NodeSpec` / `ConfigField` / `NodeExecutionContext` / `render_variables` | —— |
| `registry.py` | **注册表**：`register_node` / `declare_node_type` / `get_spec` / `get_executor` / `registered_types` / `load_node_modules` | —— |
| `start.py` | 图起点（`role="start"`）；`trigger=time` 时把整条流程按 cron 登记到调度器 | `trigger`（缺省 `message`，注册默认值）、`cron`（time 触发必填，自注册校验器）、`name` |
| `end.py` | 图终点（`role="end"`，`max_outgoing=0`）：写一条完成日志 | —— |
| `log.py` | 按级别写业务日志（支持 `{{变量}}`） | **`message`**、`level`（缺省 INFO，注册默认值；枚举由自注册校验器把） |
| `test.py` | 回显，画布联调 | `echo`（缺省用节点 id，运行期兜底） |
| `constant.py` | **常量**：config 里每一个键就是一个常量（键名 = 变量名），执行时原样产出，下游连线后用 `{{名字}}` 读 | 一组「名字 -> 值」，直接平铺在 config 上；至少要有一个，键名必须能当变量名（自注册校验器） |
| `http.py` | 发一次 HTTP 请求 | **`url`**、**`method`**（枚举由自注册校验器把）、`timeout`（缺省 10，注册默认值）、`headers`、`body` |
| `declared.py` | 占位声明 gateway / approval / expression / condition / task：规则可校验、执行器未实现 | approval 的 **`assignee`**、expression 的 **`expression`**、condition 的 **`condition`** |
| `time_trigger.py` | 兼容：旧版 `time-trigger` 类型（历史版本快照），行为同 `start` 的时间触发 | `cron`（复用 start 的时间校验器） |

> **字面量尽量走常量节点**：地址、模板、固定文案这类字符串写在 `constant` 节点上，谁要用就连
> 一根线过来用 `{{名字}}` 读 —— 别把同一串值复制进每个节点的 config（改一次要翻整张图）。常量节点
> 必须在 start 可达的主流程里（孤儿不执行，它的变量也就没人声明）。

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
    ConfigField, NodeExecutionContext, register_node, render_variables,
)

@register_node(
    "dingtalk",
    # ② 当场注册：执行函数 + 校验规则一起声明，校验器 / 模型 / 前端框架代码都不用动
    fields=[
        ConfigField("text", "消息内容", required=True),   # 缺失 → MISSING_CONFIG
        ConfigField("format", "格式", default="text"),    # 缺失 → 保存时自动补 text
    ],
)
async def exec_dingtalk(node, ctx: NodeExecutionContext) -> dict[str, object]:
    text = render_variables(str(node.config.get("text", "")), ctx.variables)
    ctx.logger.info("发钉钉消息", node_id=node.id, text=text)
    ctx.log.append(f"[dingtalk] {node.id}: {text}")
    return {"dingtalk_text": text}             # 本节点的输出变量（下游 {{dingtalk_text}}）
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

- 入参：节点本身（`id` / `type` / `config` / `outputs`）+ 运行时上下文；
- 返回：**本节点产出的变量**（`dict`），运行器把它 `update` 进 `ctx.variables`，下游用
  `{{名字}}` 引用。不产出变量就返回 `{}`（像 `start` / `end` 那样）。
- 执行是**串行**的（节点之间有数据依赖）；并行 / 分支语义留给将来的 `gateway` 节点。

### 5.3 上下文 `NodeExecutionContext` 能给什么

| 成员 | 是什么 | 用来干嘛 |
|---|---|---|
| `ctx.variables` | `dict[str, Any]`，上游所有节点的输出合并而来 | 取上游的值；`render_variables` 用的就是它 |
| `ctx.logger` | `BaseLogger`（`nacho.core.logger`） | 写业务日志（节点自己的运行痕迹） |
| `ctx.log` | `list[str]` | 节点产出的文字行（给前端回显 / 测试断言，不落日志文件） |
| `ctx.scheduler` | `TaskManager \| None` | 要把流程挂到 cron 就用它（`start` 的 `trigger=time` 的做法）；没注入时是 `None` |
| `ctx.run_workflow()` | `async` 回调 | 触发整条流程（cron 到点时调它） |

`render_variables(template, variables)`：把 `{{名字}}` 换成变量值（变量不存在就留空串）。
名字规则是 `[A-Za-z_][A-Za-z0-9_]*`（**只有这种名字算变量**，其余原样保留）。
配置里凡是让用户填文本的地方都走它，口径才一致。

### 5.4 输出变量的命名

变量上下文是**全图一份**（不是每个节点一份），所以：

- 起**带前缀**的名字（`log_message` / `echo` / `http_status` / `dingtalk_text`），别用 `result`、
  `data` 这种通用词 —— 两个节点都产出 `result` 时，后跑的会覆盖先跑的；
- 节点 `outputs` 里要声明这些名字：校验器按「前置节点声明的变量」判断 `{{名字}}` 合不合法
  （`VARIABLE_NOT_DECLARED` / `VARIABLE_OUT_OF_SCOPE` 就是它报的）。

### 5.5 失败怎么处理：分两类

| 情况 | 怎么办 | 例子 |
|---|---|---|
| **业务结果**（对方回了错、查不到、校验不过） | 记日志（`ctx.logger.warning`）+ 正常返回，让流程继续往下走 | `http` 节点的 4xx / 5xx |
| **环境问题**（连不上、超时、配置写错、依赖没装） | 直接 `raise`：整条流程失败并留下堆栈，别伪装成「成功但没内容」 | `http` 节点连不上、`url` 渲染后为空 |

跑图的失败长这样（`executor.py`）：执行函数一抛，`SimpleWorkflowRunner.run` 就中断，
日志里那条异常带着堆栈 —— 比「跑完了但什么都没发生」好查得多。

### 5.6 校验那一关：注册什么，就校验什么

校验器（`validator.py`）**没有任何具体节点类型的知识**：它只从注册表读每个类型的
`NodeSpec`，按规格办事。新增类型不用动 `validator.py` / `models.py` 一行。

**① config 字段分两类，在注册处声明**（`fields=[ConfigField(...)]`）：

| 字段类别 | 写法 | 缺失时 |
|---|---|---|
| 不可缺失字段 | `ConfigField("url", required=True)` | 主流程上的节点直接报 `MISSING_CONFIG`（`None` / 空串也算缺失） |
| 默认值字段 | `ConfigField("level", default="INFO")` | 校验前先补默认值（自定义校验器看到的是补全后的 config）；保存版本时写进快照 |

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
`gateway` 用 `min_outgoing=2` 表达「至少两个分支」，`end` 用 `max_outgoing=0` 表达「不能有出边」，
都是通用约束，没有特判代码。

**④ 只声明、不实现：`declare_node_type`**。执行器还没写、但希望类型已经能进画布、
能保存、能被校验时，只登记规格（`nodes/declared.py` 里的 gateway / approval 等就是占位）。
这种类型真被主流程跑到时，运行器按老规矩报「暂无执行器」。

**⑤ 孤儿节点**：从 start 不可达的节点**一律放行**——类型未注册、config 缺失、自带环都不报错，
保存可以、运行不跑。所以字段规则只对主流程（start 可达）上的节点生效。

**⑥ 前端**：节点在画布上的图标 / 名称 / 配置表单仍是前端自己的清单
（`WorkflowEditor.tsx` 的 `NODE_TYPES`、`workflowApi.ts` 的 `NodeType`，后者已是
`NodeType | string` 不挡新类型）；后端不再需要同步任何白名单。

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
        id="d1", type="dingtalk", config={"text": "hi {{name}}"}, outputs=["dingtalk_text"]
    )
    ctx = NodeExecutionContext()
    ctx.variables["name"] = "nacho"
    assert await exec_dingtalk(node, ctx) == {"dingtalk_text": "hi nacho"}
```

- **直接调函数**，不必为了测节点去拼一张图（要走全链路再 `WorkflowGraph` + `SimpleWorkflowRunner`）；
- 对外部 IO **打桩**，别走真实网络：`monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)`
  （`tests/test_workflow.py` 里的 `FakeAsyncClient` 就是这个套路），本项目的惯例是打桩而不是起服务；
- 注册本身也值得测：`assert get_executor("dingtalk") is exec_dingtalk`、
  `load_node_modules("你的模块")` 幂等、模块不存在时抛 `ModuleNotFoundError`。

### 5.9 上线前自查

- [ ] 类型在模块里注册上了（`get_spec("类型") is not None`；有执行函数再查 `get_executor`）
- [ ] config 字段规则在注册处声明齐了：必填的 `required=True`，有缺省的给 `default`
- [ ] 类型专属校验（可选）：注册时挂 `validator`，配置写错在保存时就报
- [ ] 需要的拓扑约束：`role` / `min_outgoing` / `max_outgoing` / `expression_field`
- [ ] 前端画布清单（`WorkflowEditor.tsx`）加了入口；`workflowApi.ts` 无需改动
- [ ] `outputs` 与实际返回的 key 一致，名字带节点前缀
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
| 新的执行语义（并发 / 分支 / 重试） | `executor.py` 的 `WorkflowRunner` 协议位 |
| 发布 / 触发链路 | `runtime.py`（`make_trigger` / `run_published_workflow`） |
