# nacho.workflow 模块索引（MODULE MAP）

> 本文件是 `nacho/workflow/` 下**逐文件 → 作用**的速查索引。详细的「为什么」写在每个 `.py` 的
> 模块 docstring 里（`:mod:` 交叉引用），本文件只做「一眼定位」。
>
> **要写自己的节点，直接跳第 5 节**（完整指南：契约、注册、白名单、可选依赖、测试写法）。
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
│   ├── base.py          契约：NodeExecutor / NodeExecutionContext / render_variables
│   ├── registry.py      注册表：register_executor / @register_node / get_executor / load_node_modules
│   ├── start.py         内置：start（图起点）
│   ├── end.py           内置：end（图终点）
│   ├── log.py           内置：log（按级别写业务日志）
│   ├── test.py          内置：test（回显，画布联调用）
│   ├── time_trigger.py  内置：time-trigger（按 cron 把整条流程登记到调度器）
│   └── http.py          内置：http（发一次 HTTP 请求；需要可选依赖 httpx）
├── executor.py      运行器：按拓扑顺序把图跑起来（SimpleWorkflowRunner）
└── runtime.py       运行时：加载已发布版本的图并执行（time-trigger 到点后走它）
```

**依赖方向（单向、无环）**：

```
executor / runtime ──► nodes ──► models
validator ──────────► nodes.http      （只为拿 method 白名单）
store ──────────────► models
```

- `nodes/` 只依赖 `models`：**节点不需要知道**运行器、校验器、存储的存在；
- 运行器按**类型名**从注册表取执行函数，不认识任何具体节点；
- 本包不 import FastAPI（HTTP 入口在 `nacho/api/api/workflow/`，装配在 `nacho/api/app.py`）。

---

## 1. models.py —— 图与数据协议

| 名字 | 作用 |
|---|---|
| `NodeType` | 节点类型的字面量白名单（**新增类型要在这里登记**，见第 5.6 节） |
| `WorkflowNode` | 一个节点：`id`（图内唯一）/ `type` / `config` / `outputs`（声明的输出变量名） |
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
| ① 结构 | 能不能解析成图、节点 id 唯一、类型在白名单里 | `UNKNOWN_NODE_TYPE` |
| ② 拓扑 | 有起点 / 有终点、无环（Kahn）、没有只进不出这类悬空结构 | `CYCLE_DETECTED` 等 |
| ③ 语义 | 必填 config、变量作用域（`{{x}}` 得有前置节点声明）、表达式语法、类型专属配置 | `MISSING_CONFIG` / `VARIABLE_NOT_DECLARED` / `VARIABLE_OUT_OF_SCOPE` / `INVALID_CRON` / `INVALID_LOG_LEVEL` / `INVALID_HTTP_METHOD` |
| ④ Dry Run | 预留协议位（`DryRunner` / `ExpressionSyntaxChecker`），当前默认全放行 | —— |

> **短路**：某一阶段出错就不再往后跑 —— 结构都不对，拓扑 / 语义无从谈起。
> 类型专属校验集中在 `_type_specific()`，加一条规则就加一个 `elif node.type == ...` 分支。

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
| `base.py` | **契约**：`NodeExecutor` 类型、`NodeExecutionContext`、`render_variables` | —— |
| `registry.py` | **注册表**：`register_executor` / `@register_node` / `get_executor` / `registered_types` / `load_node_modules` | —— |
| `start.py` | 图起点：透传输入 | —— |
| `end.py` | 图终点：写一条完成日志 | —— |
| `log.py` | 按级别写业务日志（支持 `{{变量}}`） | **`message`**、`level`（缺省 INFO） |
| `test.py` | 回显，画布联调 | `echo`（缺省用节点 id） |
| `time_trigger.py` | 把**整条流程**按 cron 登记到调度器 | **`cron`**、`name` |
| `http.py` | 发一次 HTTP 请求，产出 `http_status` / `http_body` | **`url`**、**`method`**、`headers`、`body`、`timeout` |

**注册表是进程级、内存里的一张表**（`registry._EXECUTORS`，类型名 → 执行函数），不落库、没有配置文件：

```python
_EXECUTORS: dict[str, NodeExecutor] = {}

def register_executor(node_type: str, executor: NodeExecutor) -> None: ...   # 重复注册 = 覆盖
def register_node(node_type: str) -> Callable[[NodeExecutor], NodeExecutor]: ...  # 装饰器写法
def get_executor(node_type: str) -> NodeExecutor | None: ...                 # 没注册给 None
def registered_types() -> tuple[str, ...]: ...                              # 排查「注册上没有」
def load_node_modules(*module_names: str) -> list[str]: ...                 # 装外部模块
```

包一被 import，各节点模块就自己登记一次（`nodes/__init__.py` 逐个 import 内置节点）。
所以「注册一个节点」= **让那个模块被 import 到**。

## 5. 写自己的节点（开发者指南）★

### 5.1 三步，不用改框架文件

```python
# ① 新建一个模块（照 nacho/workflow/nodes/log.py 的样子；一个节点一个文件）
#    my_pkg/nodes/dingtalk.py
from nacho.workflow.nodes import NodeExecutionContext, register_node, render_variables

@register_node("dingtalk")                     # ② 当场注册：类型名 -> 这个函数
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
| `ctx.scheduler` | `TaskManager \| None` | 要把流程挂到 cron 就用它（`time-trigger` 的做法）；没注入时是 `None` |
| `ctx.run_workflow()` | `async` 回调 | 触发整条流程（`time-trigger` 到点时调它） |

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

### 5.6 校验那一关：类型白名单（当前的硬约束）

写完执行函数只是**执行**那一半；要让图能存下来、能跑，还得登记类型。当前要动**四处**：

| 位置 | 不登记会怎样 |
|---|---|
| `nacho/workflow/models.py` 的 `NodeType`（pydantic `Literal`） | 图**根本解析不出来**（提交就 422） |
| `nacho/workflow/validator.py` 的 `NODE_TYPES` | 校验报 `UNKNOWN_NODE_TYPE` |
| `nacho/workflow/validator.py` 的 `REQUIRED_CONFIG` | 不强制 config 必填项（可选：想让某个字段必填就登记） |
| 前端 `features/workflow/WorkflowEditor.tsx` 的 `NODE_TYPES` + `workflowApi.ts` 的 `NodeType` | 画布上没有这个节点可选 |

> 这是当前实现里唯一「必须改框架文件」的地方（类型白名单还没从注册表推导）。
> 打算做成「白名单 = 内置 ∪ 已注册执行器」的话，从 `validator.NODE_TYPES` 与
> `models.WorkflowNode.type` 那两处入手。

顺带把**类型专属校验**补上（可选但推荐）：`validator._type_specific()` 里加一个分支，
配置写错就能在**保存时**报出来，而不是等跑起来。`http` 的 `INVALID_HTTP_METHOD` 就是这么加的
（和执行器认同一份 `HTTP_METHODS`）。

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
    node = WorkflowNode.model_construct(          # 非白名单类型用 model_construct 造节点
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

- [ ] 执行函数注册上了（`get_executor("类型") is not None`）
- [ ] 白名单四处登记齐了（`NodeType` / `NODE_TYPES` / `REQUIRED_CONFIG` / 前端两处）
- [ ] 类型专属校验（可选）：配置写错在保存时就报
- [ ] `outputs` 与实际返回的 key 一致，名字带节点前缀
- [ ] 环境问题会抛、业务结果会返回（第 5.5 节）
- [ ] 有单测，且外部依赖是打桩的

## 6. 要加的东西放哪

| 要加的东西 | 放哪 |
|---|---|
| 新节点类型 | `nodes/<类型>.py`（一类一个文件，`@register_node` 注册）+ 四处白名单（第 5.6 节） |
| 节点类型专属的校验规则 | `validator.py` 的 `_type_specific()` |
| 图 / 记录上要加字段 | `models.py`（协议）+ `store.py`（表结构） |
| 新的 HTTP 接口 | `nacho/api/api/workflow/`（入口层，路由 + 请求 / 响应 schema） |
| 新的执行语义（并发 / 分支 / 重试） | `executor.py` 的 `WorkflowRunner` 协议位 |
| 发布 / 触发链路 | `runtime.py`（`make_trigger` / `run_published_workflow`） |
