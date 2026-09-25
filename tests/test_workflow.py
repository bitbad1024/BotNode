"""工作流框架的测试：校验流水线、双表存储（版本 / 隔离 / 去重）、HTTP 接口。

分三块：

* 校验器：结构 → 拓扑 → 语义三阶段短路与各类错误码（纯函数，不要库）；
* 存储：内存 sqlite 上验多用户隔离、版本自增、checksum 去重、发布与级联删除；
* 接口：``create_app`` + httpx ASGI 直连，验登录隔离、校验失败不写库、保存 / 发布链路。

需要 ``fastapi`` / ``httpx``（``pip install "nacho[dev]"``），没装就整文件跳过。
"""
from __future__ import annotations

import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, ClassVar

import pytest

pytest.importorskip("fastapi", reason="接口层要装 fastapi：pip install \"nacho[api]\"")
pytest.importorskip("httpx", reason="接口层测试用 httpx 发请求：pip install \"nacho[dev]\"")

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402

from nacho.api import ApiOptions, Pbkdf2PasswordHasher, create_app  # noqa: E402
from nacho.workflow import (  # noqa: E402
    ConfigField,
    NodeExecutionContext,
    SimpleWorkflowRunner,
    SqlWorkflowStore,
    ValidationIssue,
    WorkflowGraph,
    WorkflowNameConflict,
    WorkflowNode,
    apply_config_defaults,
    canonical_graph_json,
    declare_node_type,
    graph_checksum,
    load_node_modules,
    register_node,
    registered_types,
    render_variables,
    validate_graph,
)
from nacho.workflow.executor import get_executor  # noqa: E402
from nacho.workflow.nodes import exec_http  # noqa: E402
from nacho.workflow.validator import STAGE_SEMANTIC, STAGE_STRUCTURE, STAGE_TOPOLOGY  # noqa: E402

#: 演示账号（id 即 u-admin / u-robot）
ADMIN = {"account": "admin", "password": "nacho-admin"}
ROBOT = {"account": "robot", "password": "nacho-robot"}
_TEST_HASHER = Pbkdf2PasswordHasher(iterations=1_000)


# --------------------------------------------------------------------------- 图夹具
def node(node_id: str, node_type: str, **config: object) -> dict[str, object]:
    """造一个节点；outputs 用关键字 ``_outputs`` 传，避免和 config 混。"""
    outputs = config.pop("_outputs", [])
    return {"id": node_id, "type": node_type, "config": dict(config), "outputs": list(outputs)}


def edge(source: str, target: str) -> dict[str, str]:
    return {"source": source, "target": target}


def linear_graph() -> dict[str, object]:
    """一张各阶段都该过的最小线性图：start -> end。"""
    return {"nodes": [node("s", "start"), node("e", "end")], "edges": [edge("s", "e")]}


# --------------------------------------------------------------------------- ① 结构校验
def test_valid_linear_graph_passes() -> None:
    report = validate_graph(linear_graph())
    assert report.valid and report.errors == [] and report.stage is None


def test_structure_rejects_unknown_type_and_missing_nodes() -> None:
    bad_type = {"nodes": [node("s", "外星人"), node("e", "end")], "edges": [edge("s", "e")]}
    report = validate_graph(bad_type)
    assert not report.valid and report.stage == STAGE_STRUCTURE
    assert {issue.code for issue in report.errors} == {"UNKNOWN_NODE_TYPE"}

    assert validate_graph({"nodes": []}).stage == STAGE_STRUCTURE  # 空节点列表


def test_structure_rejects_duplicate_id_and_dangling_edge() -> None:
    graph = {
        "nodes": [node("s", "start"), node("s", "end")],
        "edges": [edge("s", "ghost")],
    }
    report = validate_graph(graph)
    assert not report.valid and report.stage == STAGE_STRUCTURE
    codes = {issue.code for issue in report.errors}
    assert "DUPLICATE_NODE_ID" in codes
    assert "EDGE_ENDPOINT_MISSING" in codes


def test_structure_short_circuits_topology() -> None:
    """结构没过时不跑拓扑：两个 start 也不应该报 START_NOT_UNIQUE（短路）。"""
    graph = {
        "nodes": [node("s1", "start"), node("s2", "start"), node("e", "end")],
        "edges": [edge("s1", "ghost")],
    }
    report = validate_graph(graph)
    assert report.stage == STAGE_STRUCTURE
    assert all(issue.code != "START_NOT_UNIQUE" for issue in report.errors)


# --------------------------------------------------------------------------- ② 拓扑校验
def test_topology_start_and_end_counts() -> None:
    no_start = {"nodes": [node("e", "end")], "edges": []}
    report = validate_graph(no_start)
    assert not report.valid and report.stage == STAGE_TOPOLOGY
    assert {issue.code for issue in report.errors} == {"START_NOT_UNIQUE"}

    no_end = {"nodes": [node("s", "start")], "edges": []}
    codes = {issue.code for issue in validate_graph(no_end).errors}
    assert "END_MISSING" in codes


def test_topology_detects_cycle() -> None:
    graph = {
        "nodes": [
            node("s", "start"),
            node("a", "test"),
            node("b", "test"),
            node("e", "end"),
        ],
        "edges": [edge("s", "a"), edge("a", "b"), edge("b", "a"), edge("a", "e")],
    }
    report = validate_graph(graph)
    assert not report.valid and report.stage == STAGE_TOPOLOGY
    assert any(issue.code == "CYCLE_DETECTED" for issue in report.errors)


def test_topology_allows_orphans_but_still_rejects_main_path_self_loop() -> None:
    """孤儿节点（不可达）及其自环 / 环 / 缺配置一律放行；主路径上的自环仍要拦。"""
    with_orphan = {
        "nodes": [node("s", "start"), node("e", "end"), node("lonely", "test")],
        "edges": [edge("s", "e"), edge("lonely", "lonely")],
    }
    assert validate_graph(with_orphan).valid  # 孤儿自环不影响主流程

    # 孤儿组件内部成环 + 是个没配 url 的 http：照样允许保存
    orphan_mess = {
        "nodes": [
            node("s", "start"),
            node("e", "end"),
            node("o1", "http"),  # 缺必填 url/method，但不可达
            node("o2", "test"),
        ],
        "edges": [
            edge("s", "e"),
            edge("o1", "o2"),
            edge("o2", "o1"),  # 孤儿环
        ],
    }
    assert validate_graph(orphan_mess).valid

    # 未注册类型的孤儿也放行（插件没装 / 先画了再说）
    orphan_unknown = {
        "nodes": [node("s", "start"), node("e", "end"), node("x", "火星节点")],
        "edges": [edge("s", "e")],
    }
    assert validate_graph(orphan_unknown).valid

    # 主路径自环仍报错
    main_self_loop = {
        "nodes": [node("s", "start"), node("e", "end")],
        "edges": [edge("s", "e"), edge("s", "s")],
    }
    codes = {issue.code for issue in validate_graph(main_self_loop).errors}
    assert "SELF_LOOP" in codes


def test_topology_end_must_be_reachable_from_start() -> None:
    """end 存在但没接进主流程（另一个孤儿）不算数，报 END_MISSING。"""
    graph = {
        "nodes": [node("s", "start"), node("e", "end"), node("x", "test")],
        "edges": [edge("s", "x")],  # end 孤立
    }
    codes = {issue.code for issue in validate_graph(graph).errors}
    assert "END_MISSING" in codes


def test_topology_min_outgoing_comes_from_registration() -> None:
    """出边下限由注册时声明（``min_outgoing``）：少了就报 GATEWAY_NEEDS_BRANCHES。"""
    declare_node_type("test-split", min_outgoing=2)
    graph = {
        "nodes": [node("s", "start"), node("g", "test-split"), node("e", "end")],
        "edges": [edge("s", "g"), edge("g", "e")],
    }
    report = validate_graph(graph)
    assert report.stage == STAGE_TOPOLOGY
    assert any(issue.code == "GATEWAY_NEEDS_BRANCHES" for issue in report.errors)

    # 两条分支后转通过（继续跑到语义：这张图语义干净）
    graph["nodes"].append(node("e2", "end"))
    graph["edges"].append(edge("g", "e2"))
    assert validate_graph(graph).valid


def test_topology_end_with_outgoing_rejected() -> None:
    graph = {
        "nodes": [node("s", "start"), node("e", "end"), node("x", "log", message="hi")],
        "edges": [edge("s", "e"), edge("e", "x")],
    }
    codes = {issue.code for issue in validate_graph(graph).errors}
    assert "END_HAS_OUTGOING" in codes


# --------------------------------------------------------------------------- ③ 语义校验
def test_semantic_missing_required_config() -> None:
    graph = {
        "nodes": [
            node("s", "start"),
            node("call", "http"),  # 没给 url / method
            node("note", "log"),  # 没给 message
            node("e", "end"),
        ],
        "edges": [edge("s", "call"), edge("call", "note"), edge("note", "e")],
    }
    report = validate_graph(graph)
    assert not report.valid and report.stage == STAGE_SEMANTIC
    missing = [issue for issue in report.errors if issue.code == "MISSING_CONFIG"]
    assert {issue.node_id for issue in missing} == {"call", "note"}


def test_semantic_variable_scope_and_spell_hint() -> None:
    graph = {
        "nodes": [
            node("s", "start", _outputs=["orderAmount"]),
            node("calc", "log", message="{{orderAmount}} * 0.9", _outputs=["price"]),
            node("typo", "log", message="{{orderAmout}} + 1"),
            node("side", "log", message="{{price}}"),
            node("e", "end"),
        ],
        "edges": [
            edge("s", "calc"),
            edge("calc", "typo"),
            edge("s", "side"),  # price 声明在 calc，side 不在它的下游
            edge("typo", "e"),
            edge("side", "e"),
        ],
    }
    report = validate_graph(graph)
    assert not report.valid and report.stage == STAGE_SEMANTIC
    by_code: dict[str, str] = {issue.code: issue for issue in report.errors}
    # orderAmout 没声明：给拼写建议
    not_declared = next(issue for issue in report.errors if issue.code == "VARIABLE_NOT_DECLARED")
    assert not_declared.node_id == "typo"
    assert "orderAmount" in not_declared.suggestion
    # price 声明了但不在 side 的前置链路上
    out_of_scope = next(issue for issue in report.errors if issue.code == "VARIABLE_OUT_OF_SCOPE")
    assert out_of_scope.node_id == "side" and "calc" in out_of_scope.message
    assert by_code  # 字典非空仅为抑制未用告警


def test_semantic_downstream_variable_passes() -> None:
    """前置节点声明的变量，下游引用应该通过（{{}} 递归进嵌套 config）。"""
    graph = {
        "nodes": [
            node("s", "start", _outputs=["token"]),
            node("call", "http", url="http://x/{{token}}", method="POST"),
            node("e", "end"),
        ],
        "edges": [edge("s", "call"), edge("call", "e")],
    }
    assert validate_graph(graph).valid


# --------------------------------------------------------------------------- ③-D 类型专属语义校验
def test_semantic_start_time_trigger_requires_cron_and_validates_it() -> None:
    """start 选时间触发：缺 cron 报 MISSING_CONFIG，cron 非法报 INVALID_CRON；消息触发免配置。"""
    g_missing = {
        "nodes": [node("s", "start", trigger="time"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    report = validate_graph(g_missing)
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert any(e.code == "MISSING_CONFIG" for e in report.errors)

    g_bad = {
        "nodes": [node("s", "start", trigger="time", cron="not a cron"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    report = validate_graph(g_bad)
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert any(e.code == "INVALID_CRON" for e in report.errors)

    g_good = {
        "nodes": [node("s", "start", trigger="time", cron="*/5 * * * *"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    assert validate_graph(g_good).valid

    # trigger 非法值
    g_bad_trigger = {
        "nodes": [node("s", "start", trigger="webhook"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    report = validate_graph(g_bad_trigger)
    assert not report.valid
    assert any(e.code == "INVALID_TRIGGER" for e in report.errors)

    # 消息触发（含完全不配 trigger 的旧 start）无需任何配置
    g_message = {
        "nodes": [node("s", "start", trigger="message"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    assert validate_graph(g_message).valid
    assert validate_graph(linear_graph()).valid


def test_semantic_log_requires_message_and_validates_level() -> None:
    """log 缺 message 报 MISSING_CONFIG；level 非法报 INVALID_LOG_LEVEL。"""
    g_missing = {
        "nodes": [node("s", "start"), node("l", "log"), node("e", "end")],
        "edges": [edge("s", "l"), edge("l", "e")],
    }
    report = validate_graph(g_missing)
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert any(e.code == "MISSING_CONFIG" for e in report.errors)

    g_bad_level = {
        "nodes": [node("s", "start"), node("l", "log", message="hi", level="TRACE"), node("e", "end")],
        "edges": [edge("s", "l"), edge("l", "e")],
    }
    report = validate_graph(g_bad_level)
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert any(e.code == "INVALID_LOG_LEVEL" for e in report.errors)

    g_good = {
        "nodes": [node("s", "start"), node("l", "log", message="hi", level="WARNING"), node("e", "end")],
        "edges": [edge("s", "l"), edge("l", "e")],
    }
    assert validate_graph(g_good).valid


def test_semantic_test_node_passes_without_config() -> None:
    """test 节点无必填配置，应该直接通过。"""
    g = {
        "nodes": [node("s", "start"), node("t", "test"), node("e", "end")],
        "edges": [edge("s", "t"), edge("t", "e")],
    }
    assert validate_graph(g).valid


# ------------------------------------------------------------------- ③-E 注册驱动的校验
def test_validation_rules_are_driven_by_registration_not_validator_code() -> None:
    """新增类型只在注册处声明规则：必填 / 默认值 / 自定义校验器全部生效，不碰 validator。"""

    def validate_ping(n: WorkflowNode) -> list[ValidationIssue]:
        if str(n.config.get("mode", "")) not in {"sync", "async"}:
            return [
                ValidationIssue(
                    node_id=n.id, code="BAD_PING_MODE", message="mode 只能是 sync/async"
                )
            ]
        return []

    @register_node(
        "reg-ping",
        fields=[
            ConfigField("url", "地址", required=True),
            ConfigField("mode", "模式", default="sync"),
        ],
        validator=validate_ping,
    )
    async def exec_ping(n: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        return {}

    # 未接进主流程时什么都不查；接进主流程后规则全生效
    base_nodes = [node("s", "start"), node("p", "reg-ping"), node("e", "end")]

    missing = {"nodes": base_nodes, "edges": [edge("s", "p"), edge("p", "e")]}
    report = validate_graph(missing)
    assert report.stage == STAGE_SEMANTIC
    assert any(
        i.node_id == "p" and i.code == "MISSING_CONFIG" for i in report.errors
    )

    bad_mode = {
        "nodes": [
            node("s", "start"),
            node("p", "reg-ping", url="https://x", mode="weird"),
            node("e", "end"),
        ],
        "edges": [edge("s", "p"), edge("p", "e")],
    }
    codes = {i.code for i in validate_graph(bad_mode).errors}
    assert "BAD_PING_MODE" in codes

    good = {
        "nodes": [
            node("s", "start"),
            node("p", "reg-ping", url="https://x"),  # mode 缺，默认 sync，校验器放行
            node("e", "end"),
        ],
        "edges": [edge("s", "p"), edge("p", "e")],
    }
    assert validate_graph(good).valid

    # 同类型作为孤儿：缺 url + mode 非法，照样通过
    orphan = {
        "nodes": [node("s", "start"), node("e", "end"), node("p2", "reg-ping")],
        "edges": [edge("s", "e")],
    }
    assert validate_graph(orphan).valid
    _ = exec_ping  # 装饰器返回原函数，保留引用仅为表明这一点


def test_apply_config_defaults_fills_registered_defaults() -> None:
    """保存版本前的默认值补全：trigger/level/timeout 缺失就填，给了值不覆盖。"""
    raw = {
        "nodes": [
            node("s", "start"),  # trigger 缺
            node("l", "log", message="hi"),  # level 缺
            node("h", "http", url="https://x", method="GET", timeout=3),  # timeout 给了
            node("t", "test"),  # 没有默认值字段
            node("e", "end"),
        ],
        "edges": [edge("s", "l"), edge("l", "h"), edge("h", "t"), edge("t", "e")],
    }
    graph = apply_config_defaults(raw)
    configs = {n.id: n.config for n in graph.nodes}
    assert configs["s"]["trigger"] == "message"
    assert configs["l"]["level"] == "INFO"
    assert configs["h"]["timeout"] == 3  # 显式值不被覆盖
    assert "echo" not in configs["t"]
    # 原 dict 不被修改
    assert "trigger" not in raw["nodes"][0]["config"]


# --------------------------------------------------------------------------- ④ 节点执行器
@pytest.mark.asyncio
async def test_executor_start_end_log_test_run() -> None:
    """start -> test -> log -> end 全链路：log 内容被 {{变量}} 替换，test 的 echo 进上下文。"""
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "start", _outputs=["name"]),
                node("t", "test", echo="hello {{name}}"),
                node("l", "log", message="echo was: {{echo}}", level="INFO"),
                node("e", "end"),
            ],
            "edges": [edge("s", "t"), edge("t", "l"), edge("l", "e")],
        }
    )
    ctx = NodeExecutionContext()
    ctx.variables["name"] = "nacho"
    await SimpleWorkflowRunner().run(graph, ctx)
    assert ctx.variables["echo"] == "hello nacho"
    assert ctx.variables["log_message"] == "echo was: hello nacho"
    # 日志收集器按顺序记了四个节点
    assert any("hello nacho" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_executor_skips_orphan_nodes_entirely() -> None:
    """孤儿节点不执行：没执行器的孤儿不拖垮主流程，有执行器的孤儿副作用也不发生。"""

    ran: list[str] = []

    @register_node("orphan-marker")
    async def exec_marker(n: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        ran.append(n.id)
        return {"orphan_var": True}

    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "start"),
                node("e", "end"),
                node("m", "orphan-marker"),  # 可达性外的有执行器孤儿：不该跑
                node("ghost", "not-a-registered-type"),  # 没登记过（连规格都没）的孤儿
            ],
            "edges": [edge("s", "e")],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)  # 不因孤儿缺执行器而抛错
    assert ran == []  # 孤儿副作用没发生
    assert "orphan_var" not in ctx.variables


@pytest.mark.asyncio
async def test_executor_orphan_edge_into_main_path_does_not_block() -> None:
    """孤儿有条边指向主流程节点时，入度只算主流程内部，主节点不会被永不执行的孤儿卡死。"""

    ran: list[str] = []

    @register_node("orphan-feeder")
    async def exec_feeder(n: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        ran.append(n.id)
        return {}

    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "start"),
                node("e", "end"),
                node("o", "orphan-feeder"),  # 不可达，但有一条 o -> e 的入边
            ],
            # 主流程 s -> e；孤儿 -> e 是从可达域外伸进来的边
            "edges": [edge("s", "e"), edge("o", "e")],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)  # 旧实现这里会卡在 e 的入度上
    assert ran == []  # 孤儿依旧不执行


@pytest.mark.asyncio
async def test_executor_start_time_trigger_registers_with_scheduler() -> None:
    """start（时间触发）注入调度器时按 cron 登记，task_id = wf-<node.id>，可幂等重登记。"""
    from nacho.core.scheduler import TaskManager

    scheduler = TaskManager()
    triggered: list[str] = []

    async def run_workflow() -> None:
        triggered.append("fired")

    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "start", trigger="time", cron="*/5 * * * *", name="每5分钟"),
                node("e", "end"),
            ],
            "edges": [edge("s", "e")],
        }
    )
    ctx = NodeExecutionContext(scheduler=scheduler, run=run_workflow)
    await SimpleWorkflowRunner().run(graph, ctx)
    task = scheduler.get("wf-s")
    assert task is not None
    assert task.name == "每5分钟"

    # 再跑一遍：先移除再登记，不报错且仍是同一个 task_id
    await SimpleWorkflowRunner().run(graph, ctx)
    assert scheduler.get("wf-s") is not None


@pytest.mark.asyncio
async def test_executor_start_message_trigger_does_not_register() -> None:
    """消息触发的 start 不登记调度器，只写一条开始日志。"""
    from nacho.core.scheduler import TaskManager

    scheduler = TaskManager()
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [node("s", "start", trigger="message"), node("e", "end")],
            "edges": [edge("s", "e")],
        }
    )
    ctx = NodeExecutionContext(scheduler=scheduler)
    await SimpleWorkflowRunner().run(graph, ctx)
    assert scheduler.list() == []
    assert "scheduled" not in ctx.variables
    assert any("消息触发" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_executor_start_time_trigger_without_scheduler_skips_gracefully() -> None:
    """没注入调度器时，时间触发 start 不抛异常，返回 scheduled=False。"""
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [node("s", "start", trigger="time", cron="*/5 * * * *"), node("e", "end")],
            "edges": [edge("s", "e")],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)
    assert ctx.variables.get("scheduled") is False
    assert any("未注入调度器" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_executor_unsupported_node_type_raises() -> None:
    """只声明了规格、没实现执行器的类型跑图时抛 NotImplementedError。"""
    declare_node_type("test-noexec")
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [node("s", "start"), node("c", "test-noexec"), node("e", "end")],
            "edges": [edge("s", "c"), edge("c", "e")],
        }
    )
    with pytest.raises(NotImplementedError):
        await SimpleWorkflowRunner().run(graph, NodeExecutionContext())


# ------------------------------------------------------------- ④-B http 节点（打桩，不走网络）
class FakeResponse:
    """假的 httpx 响应：http 节点只用到 ``status_code`` / ``text`` 两样。"""

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class FakeAsyncClient:
    """替身 ``httpx.AsyncClient``：记下收到的请求，回一个编好的响应（或抛编好的异常）。

    ``httpx`` 是可选依赖、真发请求又要走网络，所以按本项目一贯的做法**打桩**：节点内部
    取的就是 ``httpx.AsyncClient`` 这个名字，替换掉它即可（见 :func:`fake_http`）。
    """

    #: 编好的行为（fixture 每次重置）
    status: int = 200
    text: str = ""
    error: Exception | None = None
    #: 收到的请求 / 建客户端时的参数
    calls: ClassVar[list[dict[str, object]]] = []
    client_kwargs: ClassVar[dict[str, object]] = {}

    def __init__(self, **kwargs: object) -> None:
        FakeAsyncClient.client_kwargs = dict(kwargs)

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        content: str | None = None,
    ) -> FakeResponse:
        FakeAsyncClient.calls.append(
            {"method": method, "url": url, "headers": headers, "content": content}
        )
        if FakeAsyncClient.error is not None:
            raise FakeAsyncClient.error
        return FakeResponse(FakeAsyncClient.status, FakeAsyncClient.text)


@pytest.fixture
def fake_http(monkeypatch: pytest.MonkeyPatch) -> type[FakeAsyncClient]:
    """把 ``httpx.AsyncClient`` 换成替身（节点内部就取它这个名字，打这里够用）。"""
    import httpx

    FakeAsyncClient.calls = []
    FakeAsyncClient.client_kwargs = {}
    FakeAsyncClient.status = 200
    FakeAsyncClient.text = '{"ok": true}'
    FakeAsyncClient.error = None
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    return FakeAsyncClient


def http_node(**config: object) -> WorkflowNode:
    """造一个 http 节点；config 缺省补一份能跑通的（url / method 是必填项）。"""
    merged: dict[str, object] = {"url": "https://api.example.com/items", "method": "GET"}
    merged.update(config)
    return WorkflowNode.model_construct(id="h1", type="http", config=merged, outputs=[])


@pytest.mark.asyncio
async def test_http_node_renders_config_and_returns_outputs(
    fake_http: type[FakeAsyncClient],
) -> None:
    """url / headers / body 都过 ``{{变量}}`` 渲染；状态码与正文作为输出交给下游。"""
    fake_http.status = 201
    fake_http.text = '{"id": 7}'
    node_obj = http_node(
        url="https://api.example.com/items/{{item_id}}",
        method="post",
        headers={"X-Robot": "{{robot}}"},
        body='{"id": {{item_id}}}',
        timeout=3,
    )
    ctx = NodeExecutionContext()
    ctx.variables.update({"item_id": "7", "robot": "r-001"})

    outputs = await exec_http(node_obj, ctx)

    assert outputs == {"http_status": 201, "http_body": '{"id": 7}'}
    assert fake_http.calls == [
        {
            "method": "POST",  # 方法大小写不敏感
            "url": "https://api.example.com/items/7",
            "headers": {"X-Robot": "r-001"},  # 头里也渲染
            "content": '{"id": 7}',
        }
    ]
    assert fake_http.client_kwargs["timeout"] == 3.0
    assert any("POST" in line and "201" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_http_node_keeps_error_status_as_a_result(fake_http: type[FakeAsyncClient]) -> None:
    """4xx / 5xx 是「对方的回答」：不抛异常，状态码与正文照常交给下游。"""
    fake_http.status = 500
    fake_http.text = "boom"

    outputs = await exec_http(http_node(), NodeExecutionContext())

    assert outputs == {"http_status": 500, "http_body": "boom"}


@pytest.mark.asyncio
async def test_http_node_raises_on_connection_failure(fake_http: type[FakeAsyncClient]) -> None:
    """连不上 / 超时是环境问题：直接抛出去，别伪装成「成功但没内容」。"""
    import httpx

    fake_http.error = httpx.ConnectError("连不上")

    with pytest.raises(httpx.ConnectError):
        await exec_http(http_node(), NodeExecutionContext())


@pytest.mark.asyncio
async def test_http_node_rejects_unknown_method_and_empty_url() -> None:
    """配置写错当场抛（校验阶段也会拦，见 INVALID_HTTP_METHOD）。"""
    with pytest.raises(ValueError, match="method"):
        await exec_http(http_node(method="FETCH"), NodeExecutionContext())
    with pytest.raises(ValueError, match="url"):
        await exec_http(http_node(url=""), NodeExecutionContext())


@pytest.mark.asyncio
async def test_http_node_without_httpx_says_how_to_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没装 httpx（可选依赖）：报错里给安装提示，而不是莫名的 AttributeError。"""
    monkeypatch.setitem(sys.modules, "httpx", None)  # 之后再 import httpx 会抛 ImportError

    with pytest.raises(RuntimeError, match="httpx"):
        await exec_http(http_node(), NodeExecutionContext())


def test_http_method_is_checked_at_validation() -> None:
    """method 拼错在校验阶段就报；合法方法放行。"""

    def graph_with(method: str) -> dict[str, object]:
        return {
            "nodes": [
                node("s", "start"),
                node("h", "http", url="https://api.example.com", method=method),
                node("e", "end"),
            ],
            "edges": [edge("s", "h"), edge("h", "e")],
        }

    bad = validate_graph(graph_with("FETCH"))
    assert [issue.code for issue in bad.errors] == ["INVALID_HTTP_METHOD"]

    assert validate_graph(graph_with("GET")).valid


# ------------------------------------------------------------- ④-C 常量节点
async def test_constant_node_outputs_its_values_as_variables() -> None:
    """config 里每一个键就是一个常量：键名作变量名，值原样产出。"""
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node(
                    "c",
                    "constant",
                    base_url="https://api.example.com",
                    greeting="你好",
                    _outputs=["base_url", "greeting"],
                ),
            ],
            "edges": [],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)
    assert ctx.variables["base_url"] == "https://api.example.com"
    assert ctx.variables["greeting"] == "你好"


async def test_constant_value_may_reference_upstream_variable() -> None:
    """常量值也走同一套 ``{{变量}}`` 渲染 —— 与别处口径一致（引用不到在保存时就报）。"""
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "start", _outputs=["owner"]),
                node("c", "constant", who="{{owner}} 的机器人", _outputs=["who"]),
            ],
            "edges": [edge("s", "c")],
        }
    )
    ctx = NodeExecutionContext()
    ctx.variables["owner"] = "nacho"
    await SimpleWorkflowRunner().run(graph, ctx)
    assert ctx.variables["who"] == "nacho 的机器人"


def test_constant_node_needs_at_least_one_constant() -> None:
    """一个常量都没写：MISSING_CONFIG。"""
    graph = {
        "nodes": [node("s", "start"), node("c", "constant"), node("e", "end")],
        "edges": [edge("s", "c"), edge("c", "e")],
    }
    report = validate_graph(graph)
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert any(i.node_id == "c" and i.code == "MISSING_CONFIG" for i in report.errors)


def test_constant_name_must_work_as_a_variable() -> None:
    """键名当不了变量名（下游 ``{{名字}}`` 引用不到）：INVALID_CONSTANT_NAME。"""
    graph = {
        "nodes": [
            node("s", "start"),
            {"id": "c", "type": "constant", "config": {"bad-name": "x"}, "outputs": []},
            node("e", "end"),
        ],
        "edges": [edge("s", "c"), edge("c", "e")],
    }
    report = validate_graph(graph)
    assert not report.valid
    assert any(
        issue.node_id == "c" and issue.code == "INVALID_CONSTANT_NAME"
        for issue in report.errors
    )


def test_constant_is_only_readable_downstream() -> None:
    """常量沿边往下传：连上了才引用得到；常量节点没接进主流程就没人声明它的变量。"""
    nodes = [
        node("s", "start"),
        node("c", "constant", base_url="https://api.example.com", _outputs=["base_url"]),
        node("l", "log", message="GET {{base_url}}"),
        node("e", "end"),
    ]
    connected = {"nodes": nodes, "edges": [edge("s", "c"), edge("c", "l"), edge("l", "e")]}
    assert validate_graph(connected).valid

    # 常量节点成了孤儿：它不执行，base_url 也就没有节点声明
    orphan = {"nodes": nodes, "edges": [edge("s", "l"), edge("l", "e")]}
    report = validate_graph(orphan)
    assert not report.valid
    assert any(
        issue.node_id == "l" and issue.code == "VARIABLE_NOT_DECLARED"
        for issue in report.errors
    )


# --------------------------------------------------------------------------- ⑤ 自写节点
def test_builtin_node_executors_are_registered() -> None:
    """包一被 import，内置节点的执行函数就都登记好了（一类一个文件，各自注册）。"""
    for node_type in ("start", "end", "log", "test", "http", "constant"):
        assert get_executor(node_type) is not None
    assert set(registered_types()) >= {"start", "end", "log", "test", "http", "constant"}


def test_declare_node_type_gives_rules_without_executor() -> None:
    """``declare_node_type``：规则在（必填字段 / 出边下限生效），执行器留空。

    内置节点里已经没有这种「只声明不实现」的类型了（见 nodes/ 的文件表），这条给扩展方用。
    """
    from nacho.workflow import get_spec

    declare_node_type(
        "test-declared",
        fields=[ConfigField("who", "审批人", required=True)],
        min_outgoing=2,
    )
    spec = get_spec("test-declared")
    assert spec is not None
    assert spec.min_outgoing == 2
    assert spec.executor is None  # 只有声明
    assert get_spec("end").max_outgoing == 0  # 内置节点的约束同样在注册表里

    # 出边给够（两条），好让流水线走到语义阶段去查必填字段
    graph = {
        "nodes": [
            node("s", "start"),
            node("a", "test-declared"),
            node("e", "end"),
            node("e2", "end"),
        ],
        "edges": [edge("s", "a"), edge("a", "e"), edge("a", "e2")],
    }
    codes = {i.code for i in validate_graph(graph).errors}
    assert "MISSING_CONFIG" in codes  # who 必填规则来自注册声明


def test_register_node_decorator_registers_and_returns_the_function() -> None:
    """``@register_node`` 当场注册，并返回原函数（照旧能直接调用 / 拿去单测）。"""

    @register_node("my-echo")
    async def exec_my_echo(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        return {"echo": node.id, "seen": len(ctx.variables)}

    assert get_executor("my-echo") is exec_my_echo


def test_load_node_modules_is_idempotent_and_loud_on_failure() -> None:
    """装别人的节点模块：重复加载幂等；模块不存在当场抛（别把「没注册上」藏到跑图时才报）。"""
    assert load_node_modules("nacho.workflow.nodes.log") == ["nacho.workflow.nodes.log"]
    # 第二次命中 sys.modules 缓存：模块体不会再执行一遍
    assert load_node_modules("nacho.workflow.nodes.log") == ["nacho.workflow.nodes.log"]

    with pytest.raises(ModuleNotFoundError):
        load_node_modules("nacho.workflow.nodes.no_such_module")


@pytest.mark.asyncio
async def test_custom_node_type_runs_end_to_end() -> None:
    """自写的节点类型：注册进注册表后，运行器按类型就能取到并跑出变量。

    这里直接构造图（不经过校验）：本条验的是「注册表 + 运行器」这条链路。
    校验那一关的合法性同样查注册表（见 test_validation_rules_are_driven_by_registration_*）。
    """

    @register_node("my-upper")
    async def exec_my_upper(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        text = render_variables(str(node.config.get("text", "")), ctx.variables)
        return {"upper": text.upper()}

    custom = WorkflowNode.model_construct(
        id="u", type="my-upper", config={"text": "hi {{name}}"}, outputs=["upper"]
    )
    graph = WorkflowGraph(nodes=[custom], edges=[])
    ctx = NodeExecutionContext()
    ctx.variables["name"] = "nacho"

    await SimpleWorkflowRunner().run(graph, ctx)

    assert ctx.variables["upper"] == "HI NACHO"


# --------------------------------------------------------------------------- checksum
def test_checksum_stable_under_key_order_and_spacing() -> None:
    """同一张图不同写法（键序、空白）算同一个摘要；内容变了摘要才变。"""
    first = canonical_graph_json(linear_graph())
    reordered = {"edges": [{"target": "e", "source": "s"}], "nodes": [
        {"outputs": [], "config": {}, "type": "start", "id": "s"},
        {"config": {}, "outputs": [], "type": "end", "id": "e"},
    ]}
    assert graph_checksum(reordered) == graph_checksum(linear_graph())
    changed = {"nodes": [node("s", "start"), node("e2", "end")], "edges": [edge("s", "e2")]}
    assert graph_checksum(changed) != graph_checksum(linear_graph())
    assert json_loads(first)["nodes"][0]["id"] == "s"


def test_checksum_ignores_node_positions_but_snapshot_keeps_them() -> None:
    """挪动节点坐标不改变摘要（不产生新版本），但规范快照里坐标仍然保留。"""
    base = {"nodes": [node("s", "start"), node("e", "end")], "edges": [edge("s", "e")]}
    moved: dict[str, object] = {
        "nodes": [
            {**node("s", "start"), "x": 120, "y": 240},
            {**node("e", "end"), "x": 480, "y": 96},
        ],
        "edges": [edge("s", "e")],
    }
    assert graph_checksum(moved) == graph_checksum(base)

    snapshot = json_loads(canonical_graph_json(moved))
    assert snapshot["nodes"][0]["x"] == 120
    assert snapshot["nodes"][0]["y"] == 240
    # 再挪一次：摘要依旧相同（坐标字段不进 hash）
    moved_again = {
        "nodes": [
            {**node("s", "start"), "x": 1, "y": 1},
            node("e", "end"),
        ],
        "edges": [edge("s", "e")],
    }
    assert graph_checksum(moved_again) == graph_checksum(base)


def test_edge_ports_round_trip_and_affect_checksum() -> None:
    """边的端口字段（驼峰 / 下划线两种写法）都能解析，且属于图内容、参与摘要。"""
    camel = {
        "nodes": [node("s", "start"), node("e", "end")],
        "edges": [{"source": "s", "target": "e", "sourcePort": "trigger",
                   "targetPort": "trigger"}],
    }
    snake = {
        "nodes": [node("s", "start"), node("e", "end")],
        "edges": [{"source": "s", "target": "e", "source_port": "trigger",
                   "target_port": "trigger"}],
    }
    camel_g = WorkflowGraph.model_validate(camel)
    snake_g = WorkflowGraph.model_validate(snake)
    assert camel_g.edges[0].source_port == "trigger"
    assert snake_g.edges[0].source_port == "trigger"
    assert graph_checksum(camel_g) == graph_checksum(snake_g)

    no_ports = linear_graph()
    assert graph_checksum(camel_g) != graph_checksum(no_ports)


def test_draft_graph_is_lenient() -> None:
    """暂存图允许空节点列表 / 缺字段 / 额外 UI 数据（提交版本时才严格校验）。"""
    from nacho.workflow import DraftGraph

    draft = DraftGraph.model_validate({"nodes": [], "edges": []})
    assert draft.nodes == [] and draft.edges == []
    half = DraftGraph.model_validate(
        {"nodes": [{"id": "n1"}], "edges": [{"source": "n1"}], "viewport": {"zoom": 1.5}}
    )
    assert half.nodes[0].type == ""
    assert half.edges[0].target == ""
    assert half.model_dump()["viewport"] == {"zoom": 1.5}


def json_loads(text: str) -> dict[str, object]:
    """测试用小工具（放文件尾部避免遮蔽标准库导入位置）。"""
    import json

    return json.loads(text)


# --------------------------------------------------------------------------- 存储
_MEMORY_ENGINES: list[AsyncEngine] = []


@pytest.fixture
async def store() -> AsyncGenerator[SqlWorkflowStore]:
    """每个用例一块内存 sqlite（表已建好），用完 dispose。"""
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    _MEMORY_ENGINES.append(engine)
    created = SqlWorkflowStore(engine)
    await created.ensure_schema()
    yield created


@pytest.fixture(autouse=True)
async def _dispose_memory_engines() -> AsyncGenerator[None]:
    yield
    while _MEMORY_ENGINES:
        await _MEMORY_ENGINES.pop().dispose()


async def test_store_owner_isolation_and_name_conflict(
    store: SqlWorkflowStore,
) -> None:
    alice = await store.create("u-admin", "审批流")
    bob = await store.create("u-robot", "审批流")  # 不同归属允许同名
    assert alice.id != bob.id

    with pytest.raises(WorkflowNameConflict):
        await store.create("u-admin", "审批流")  # 同归属同名拒绝

    assert {item.id for item in await store.list(owner_id="u-admin")} == {alice.id}
    assert {item.id for item in await store.list(owner_id=None)} == {alice.id, bob.id}
    assert (await store.list(owner_id="u-robot"))[0].current_version == 0


async def test_store_version_increment_dedup_and_publish(
    store: SqlWorkflowStore,
) -> None:
    definition = await store.create("u-admin", "发版流")
    graph = linear_graph()

    first, created_first = await store.add_version(
        definition,
        graph_json=canonical_graph_json(graph),
        checksum=graph_checksum(graph),
        note="首版",
    )
    assert created_first and first.version == 1

    # 提交后当前指针切到 version
    pointed = await store.get(definition.id)
    assert pointed is not None and pointed.current_ref == "version"

    # 内容没变：命中最新版本，不新增
    again, created_again = await store.add_version(
        definition,
        graph_json=canonical_graph_json(graph),
        checksum=graph_checksum(graph),
    )
    assert not created_again and again.version == 1
    # 命中已有版本也算一次「提交」：指针仍是 version
    pointed_again = await store.get(definition.id)
    assert pointed_again is not None and pointed_again.current_ref == "version"

    # 改了：新版本 2，定义指针跟着挪
    graph_v2 = {"nodes": [node("s", "start"), node("m", "test"), node("e", "end")],
                "edges": [edge("s", "m"), edge("m", "e")]}
    second, created_second = await store.add_version(
        definition,
        graph_json=canonical_graph_json(graph_v2),
        checksum=graph_checksum(graph_v2),
    )
    assert created_second and second.version == 2
    latest = await store.get(definition.id)
    assert latest is not None and latest.current_version == 2 and latest.status == "draft"

    # 版本历史倒序、按号取快照
    history = await store.list_versions(definition.id)
    assert [item.version for item in history] == [2, 1]
    snapshot = await store.get_version(definition.id, 1)
    assert snapshot is not None and snapshot.graph().nodes[0].id == "s"

    # 发布最新版；发布不存在的版本返回 None
    published = await store.publish(definition.id, 2)
    assert published is not None and published.status == "published"
    assert published.published_version == 2
    assert await store.publish(definition.id, 99) is None


async def test_store_delete_cascades_versions(store: SqlWorkflowStore) -> None:
    definition = await store.create("u-admin", "待删流")
    await store.add_version(
        definition,
        graph_json=canonical_graph_json(linear_graph()),
        checksum=graph_checksum(linear_graph()),
    )
    assert await store.delete(definition.id) is True
    assert await store.get(definition.id) is None
    assert await store.list_versions(definition.id) == []
    assert await store.delete(definition.id) is False  # 再删一次


async def test_store_draft_save_overwrites_and_switches_pointer(
    store: SqlWorkflowStore,
) -> None:
    """暂存覆盖式写图、指针切 draft；提交版本后指针切 version；暂存内容原样可读。"""
    from nacho.workflow import canonical_draft_json

    definition = await store.create("u-admin", "暂存流")
    # 新建默认指针 draft，没暂存过
    assert definition.current_ref == "draft"
    assert definition.draft_graph_json == "" and definition.draft_updated_at == 0.0

    # 半张图也能暂存（不校验）
    half = {"nodes": [{"id": "s", "type": "start"}], "edges": []}
    saved = await store.save_draft(definition.id, canonical_draft_json(half))
    assert saved is not None and saved.current_ref == "draft"
    draft = saved.draft_graph()
    assert draft is not None and draft.nodes[0].id == "s"
    assert saved.draft_updated_at > 0

    # 提交版本：指针切到 version，暂存内容不受影响
    version, created = await store.add_version(
        saved,
        graph_json=canonical_graph_json(linear_graph()),
        checksum=graph_checksum(linear_graph()),
    )
    assert created and version.version == 1
    committed = await store.get(definition.id)
    assert committed is not None and committed.current_ref == "version"

    # 再暂存：指针切回 draft，暂存被覆盖
    redraft = await store.save_draft(definition.id, canonical_draft_json(half))
    assert redraft is not None and redraft.current_ref == "draft"

    # 不存在的工作流暂存返回 None
    assert await store.save_draft("not-exist", canonical_draft_json(half)) is None


# --------------------------------------------------------------------------- HTTP 接口
def api_app() -> FastAPI:
    """接口层应用（演示账号在 lifespan 里种好；工作流双表在同一块内存 sqlite）。"""
    return create_app(ApiOptions(prefix="/api"), hasher=_TEST_HASHER)


@asynccontextmanager
async def api_client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    """直连 ASGI 并手动跑一遍 lifespan（建表 / 种账号在里面）。"""
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client


async def login(client: httpx.AsyncClient, who: dict[str, str]) -> str:
    response = await client.post("/api/auth/login", json=who)
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["token"])


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_api_create_validate_save_publish_full_chain() -> None:
    async with api_client(api_app()) as client:
        token = await login(client, ADMIN)

        # 校验接口：坏图返回 valid=false 且不碰库
        invalid = await client.post(
            "/api/workflows/validate", headers=auth(token), json={"graph": {"nodes": []}}
        )
        assert invalid.status_code == 200 and invalid.json()["data"]["valid"] is False

        # 新建
        created = await client.post(
            "/api/workflows", headers=auth(token), json={"name": "接口链路流"}
        )
        assert created.status_code == 201, created.text
        workflow_id = created.json()["data"]["id"]

        # 保存坏版本：200 + 报告，版本号没动
        rejected = await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(token),
            json={"graph": {"nodes": [node("s", "start")]}},  # 没 end
        )
        assert rejected.status_code == 200
        assert rejected.json()["data"]["valid"] is False
        detail = await client.get(f"/api/workflows/{workflow_id}", headers=auth(token))
        assert detail.json()["data"]["current_version"] == 0

        # 保存好版本：201 + created=true；再存同样内容 created=false
        saved = await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(token),
            json={"graph": linear_graph(), "note": "首版"},
        )
        assert saved.status_code == 201 and saved.json()["data"]["created"] is True
        saved_again = await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(token),
            json={"graph": linear_graph()},
        )
        # 内容没变：200（没创建新资源）+ created=false
        assert saved_again.status_code == 200
        assert saved_again.json()["data"]["created"] is False

        # 版本历史 / 单版本 / 发布
        versions = await client.get(
            f"/api/workflows/{workflow_id}/versions", headers=auth(token)
        )
        assert versions.status_code == 200 and len(versions.json()["data"]) == 1
        published = await client.post(
            f"/api/workflows/{workflow_id}/publish", headers=auth(token), json={}
        )
        assert published.status_code == 200
        assert published.json()["data"]["status"] == "published"

        # 删除
        removed = await client.delete(
            f"/api/workflows/{workflow_id}", headers=auth(token)
        )
        assert removed.status_code == 204
        assert (await client.get(f"/api/workflows/{workflow_id}", headers=auth(token))).status_code == 404


async def test_api_draft_stage_then_commit_then_publish() -> None:
    """暂存链路：空暂存 → 半张图可暂存（含坐标 / 端口）→ 指针随动作切换 → 提交 → 发布。"""
    async with api_client(api_app()) as client:
        token = await login(client, ADMIN)
        created = await client.post(
            "/api/workflows", headers=auth(token), json={"name": "暂存链路流"}
        )
        workflow_id = created.json()["data"]["id"]

        # 初始：暂存区为空，指针默认 draft
        empty_draft = await client.get(
            f"/api/workflows/{workflow_id}/draft", headers=auth(token)
        )
        assert empty_draft.status_code == 200
        assert empty_draft.json()["data"]["graph"] is None
        assert (await client.get(f"/api/workflows/{workflow_id}", headers=auth(token))) \
            .json()["data"]["current_ref"] == "draft"

        # 半张图（只有 start、带坐标）也能暂存，不校验；坐标原样回来
        half = {"nodes": [{"id": "s", "type": "start", "x": 12.5, "y": 34}], "edges": []}
        put = await client.put(
            f"/api/workflows/{workflow_id}/draft",
            headers=auth(token),
            json={"graph": half},
        )
        assert put.status_code == 200, put.text
        assert put.json()["data"]["current_ref"] == "draft"
        assert put.json()["data"]["draft_updated_at"] > 0

        got_draft = await client.get(
            f"/api/workflows/{workflow_id}/draft", headers=auth(token)
        )
        nodes = got_draft.json()["data"]["graph"]["nodes"]
        assert nodes[0]["id"] == "s" and nodes[0]["x"] == 12.5 and nodes[0]["y"] == 34

        # 图形态不合法（nodes 不是数组）-> 422，不写库
        bad = await client.put(
            f"/api/workflows/{workflow_id}/draft",
            headers=auth(token),
            json={"graph": {"nodes": "oops"}},
        )
        assert bad.status_code == 422

        # 提交合法版本后：指针切到 version
        graph_with_ports = {
            "nodes": [node("s", "start"), node("e", "end")],
            "edges": [{"source": "s", "target": "e", "sourcePort": "trigger",
                       "targetPort": "trigger"}],
        }
        committed = await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(token),
            json={"graph": graph_with_ports},
        )
        assert committed.status_code == 201, committed.text
        detail = await client.get(f"/api/workflows/{workflow_id}", headers=auth(token))
        assert detail.json()["data"]["current_ref"] == "version"

        # 版本快照里带回了端口字段（下划线形态）
        versions = await client.get(
            f"/api/workflows/{workflow_id}/versions", headers=auth(token)
        )
        snap_edge = versions.json()["data"][0]["graph"]["edges"][0]
        assert snap_edge["source_port"] == "trigger"
        assert snap_edge["target_port"] == "trigger"

        # 再暂存：指针切回 draft；发布只挪指针、200
        redraft = await client.put(
            f"/api/workflows/{workflow_id}/draft",
            headers=auth(token),
            json={"graph": half},
        )
        assert redraft.status_code == 200
        assert redraft.json()["data"]["current_ref"] == "draft"
        published = await client.post(
            f"/api/workflows/{workflow_id}/publish", headers=auth(token), json={}
        )
        assert published.status_code == 200
        assert published.json()["data"]["status"] == "published"
        # 发布不改变当前查看指针（仍指向暂存区）
        assert published.json()["data"]["current_ref"] == "draft"


async def test_load_published_workflows_registers_crons() -> None:
    """启动载入：已发布的定时流把 start 登记到调度器；草稿 / 未发布不登记。"""
    from nacho.core.scheduler import TaskManager
    from nacho.workflow.runtime import load_published_workflows

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    await store.ensure_schema()

    time_graph = {
        "nodes": [node("s", "start", trigger="time", cron="*/5 * * * *"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    published_def = await store.create("u-admin", "定时流")
    await store.add_version(
        published_def,
        graph_json=canonical_graph_json(time_graph),
        checksum=graph_checksum(time_graph),
    )
    assert await store.publish(published_def.id, 1) is not None

    # 只有版本、没发布的工作流不该被载入（消息触发也不会登记任务）
    draft_def = await store.create("u-admin", "草稿流")
    await store.add_version(
        draft_def,
        graph_json=canonical_graph_json(linear_graph()),
        checksum=graph_checksum(linear_graph()),
    )

    scheduler = TaskManager()
    try:
        loaded = await load_published_workflows(store, scheduler)
        assert loaded == 1
        assert scheduler.get("wf-s") is not None
    finally:
        await engine.dispose()


async def test_load_published_workflows_registers_without_running_the_graph() -> None:
    """启动载入**只登记、不执行图**：下游节点一个都不许跑。

    回归：以前靠「跑一遍整张图」让开始节点顺带登记 cron，于是每次开机都把整条流程真的
    执行了一次（没到点也跑）。现在登记只调开始节点自己。
    """

    from nacho.core.scheduler import TaskManager
    from nacho.workflow.runtime import load_published_workflows

    ran: list[str] = []

    @register_node("probe")
    async def exec_probe(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        ran.append(node.id)
        return {}

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    await store.ensure_schema()

    graph = {
        "nodes": [
            node("s", "start", trigger="time", cron="*/5 * * * *"),
            node("p", "probe"),
            node("e", "end"),
        ],
        "edges": [edge("s", "p"), edge("p", "e")],
    }
    definition = await store.create("u-admin", "带下游的定时流")
    await store.add_version(
        definition,
        graph_json=canonical_graph_json(graph),
        checksum=graph_checksum(graph),
    )
    assert await store.publish(definition.id, 1) is not None

    scheduler = TaskManager()
    try:
        assert await load_published_workflows(store, scheduler) == 1
        assert scheduler.get("wf-s") is not None  # 定时开始节点登记上了
        assert ran == []  # 而下游（probe）一次都没跑
    finally:
        await engine.dispose()


async def test_api_owner_isolation_between_users() -> None:
    async with api_client(api_app()) as client:
        admin_token = await login(client, ADMIN)
        robot_token = await login(client, ROBOT)

        created = await client.post(
            "/api/workflows", headers=auth(admin_token), json={"name": "管理员的流"}
        )
        workflow_id = created.json()["data"]["id"]

        # 普通用户看不到管理员的：详情 404，列表里也没有
        forbidden = await client.get(
            f"/api/workflows/{workflow_id}", headers=auth(robot_token)
        )
        assert forbidden.status_code == 404
        robot_list = await client.get("/api/workflows", headers=auth(robot_token))
        assert robot_list.json()["data"] == []

        # 管理员默认看全部；也能用 owner_id 缩
        admin_list = await client.get("/api/workflows", headers=auth(admin_token))
        assert len(admin_list.json()["data"]) == 1
        scoped = await client.get(
            "/api/workflows?owner_id=u-robot", headers=auth(admin_token)
        )
        assert scoped.json()["data"] == []

        # 普通用户的 owner_id 过滤参数被忽略，强制只看自己
        sneak = await client.get(
            "/api/workflows?owner_id=u-admin", headers=auth(robot_token)
        )
        assert sneak.json()["data"] == []


async def test_api_requires_login() -> None:
    async with api_client(api_app()) as client:
        assert (await client.get("/api/workflows")).status_code == 401
        assert (await client.post("/api/workflows/validate", json={"graph": linear_graph()})
                ).status_code == 401
