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
    PortSpec,
    SimpleWorkflowRunner,
    SqlWorkflowStore,
    ValidationIssue,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNameConflict,
    WorkflowNode,
    apply_config_defaults,
    canonical_graph_json,
    declare_node_type,
    graph_checksum,
    input_value,
    load_node_modules,
    register_node,
    registered_types,
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
    """造一个节点（config 直接平铺传）。"""
    return {"id": node_id, "type": node_type, "config": dict(config)}


def edge(
    source: str,
    target: str,
    source_port: str = "trigger",
    target_port: str = "trigger",
) -> dict[str, str]:
    """造一条边；端口缺省是「触发 -> 触发」（只表达先后的边，见 graph.DEFAULT_EDGE_PORT）。"""
    return {
        "source": source,
        "target": target,
        "source_port": source_port,
        "target_port": target_port,
    }


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
    """必填分两种：普通字段报 MISSING_CONFIG，数据入口没接线没填值报 INPUT_NOT_CONNECTED。

    ``http.method`` 有默认值（保存时补 GET），所以它不算缺；``constant.value`` 是必填且没有
    默认值的普通字段，缺了报 MISSING_CONFIG。
    """
    graph = {
        "nodes": [
            node("s", "start"),
            node("call", "http"),  # url 入口没接线也没填
            node("note", "log"),  # message 入口没接线也没填
            node("c", "constant"),  # value 必填、没有默认值
            node("e", "end"),
        ],
        "edges": [
            edge("s", "call"),
            edge("call", "note"),
            edge("note", "c"),
            edge("c", "e"),
        ],
    }
    report = validate_graph(graph)
    assert not report.valid and report.stage == STAGE_SEMANTIC
    missing = [issue for issue in report.errors if issue.code == "MISSING_CONFIG"]
    assert {issue.node_id for issue in missing} == {"c"}
    unconnected = [issue for issue in report.errors if issue.code == "INPUT_NOT_CONNECTED"]
    assert {issue.node_id for issue in unconnected} == {"call", "note"}  # url / message


def test_semantic_checks_edge_ports() -> None:
    """连线就是数据契约：端口名写错 / 两端类型不配，都在语义阶段拦住。"""
    typo = {
        "nodes": [node("s", "start"), node("l", "log"), node("e", "end")],
        "edges": [
            edge("s", "l"),
            edge("l", "e"),
            edge("s", "l", "mesage", "message"),  # start 上没有 mesage 这个出口
        ],
    }
    report = validate_graph(typo)
    assert not report.valid and report.stage == STAGE_SEMANTIC
    unknown = next(issue for issue in report.errors if issue.code == "UNKNOWN_PORT")
    assert unknown.node_id == "s" and "mesage" in unknown.message
    assert "message" in unknown.suggestion  # 拼错时给「是否想用」

    mismatch = {
        "nodes": [node("s", "start"), node("l", "log"), node("e", "end")],
        "edges": [edge("s", "l"), edge("l", "e"), edge("s", "l", "trigger", "message")],
    }
    codes = {issue.code for issue in validate_graph(mismatch).errors}
    assert "PORT_TYPE_MISMATCH" in codes


def test_semantic_one_data_input_takes_one_edge() -> None:
    """一个数据入口只允许接一条线（要合并就先汇到一个节点再往下送）。"""
    graph = {
        "nodes": [
            node("s", "start"),
            node("c", "constant", value="a"),
            node("l", "log"),
            node("e", "end"),
        ],
        "edges": [
            edge("s", "c"),
            edge("c", "l", "value", "message"),
            edge("s", "l", "message", "message"),  # 第二条线接到同一个入口
            edge("l", "e"),
        ],
    }
    codes = {issue.code for issue in validate_graph(graph).errors}
    assert "DUPLICATE_INPUT_EDGE" in codes


def test_semantic_trigger_ports_allow_convergence() -> None:
    """「只接一条线」只管数据入口：控制流端口允许多条入边汇聚（菱形 / 多分支汇流）。

    引擎按入度排序，两条 trigger 边汇到同一个 end 就是「都跑完才轮到它」；校验器不该拦。
    数据入口（message）照旧只允许一条 —— 两份值进同一个入口没法选。
    """
    diamond = {
        "nodes": [
            node("s", "start"),
            node("a", "test", message="A"),
            node("b", "test", message="B"),
            node("e", "end"),
        ],
        "edges": [
            edge("s", "a"),
            edge("s", "b"),
            edge("a", "e"),  # 两条 trigger 边汇到 e.trigger：允许
            edge("b", "e"),
        ],
    }
    assert validate_graph(diamond).valid

    dup_data = {
        "nodes": [
            node("s", "start"),
            node("t", "test", message="x"),
            node("l", "log"),
            node("e", "end"),
        ],
        "edges": [
            edge("s", "t"),
            edge("t", "l", "message", "message"),
            edge("s", "l", "message", "message"),  # 第二条数据线接到同一个入口
            edge("l", "e"),
        ],
    }
    codes = {issue.code for issue in validate_graph(dup_data).errors}
    assert "DUPLICATE_INPUT_EDGE" in codes


def test_semantic_wired_data_input_passes() -> None:
    """数据入口接上上游的输出端口（类型也对得上）就通过 —— 不需要在 config 里填值。"""
    graph = {
        "nodes": [
            node("s", "start"),
            node("c", "constant", value="https://api.example.com"),
            node("call", "http", method="POST"),
            node("note", "log"),
            node("e", "end"),
        ],
        "edges": [
            edge("s", "c"),
            edge("c", "call", "value", "url"),  # 常量 -> http 的 url 入口
            edge("call", "note", "http_status", "message"),  # 状态码 -> log 的 message 入口
            edge("call", "e"),
        ],
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
    """log 的 message 入口没接线也没手填 → INPUT_NOT_CONNECTED；level 非法 → INVALID_LOG_LEVEL。"""
    g_missing = {
        "nodes": [node("s", "start"), node("l", "log"), node("e", "end")],
        "edges": [edge("s", "l"), edge("l", "e")],
    }
    report = validate_graph(g_missing)
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert any(e.code == "INPUT_NOT_CONNECTED" for e in report.errors)

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
    """test 节点没有必填项：message 入口可选（没接线时用手填值，连键都没有才用节点 id）。"""
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
    """保存版本前的默认值补全：trigger / level / method / message 缺失就填，给了值不覆盖。"""
    raw = {
        "nodes": [
            node("s", "start"),  # trigger 缺
            node("l", "log", message="hi"),  # level 缺
            node("h", "http", url="https://x", timeout=3),  # method 缺、timeout 给了
            node("t", "test"),  # message 缺
            node("e", "end"),
        ],
        "edges": [edge("s", "l"), edge("l", "h"), edge("h", "t"), edge("t", "e")],
    }
    graph = apply_config_defaults(raw)
    configs = {n.id: n.config for n in graph.nodes}
    assert configs["s"]["trigger"] == "message"
    assert configs["l"]["level"] == "INFO"
    assert configs["h"]["method"] == "GET"  # 画布一直替它填 GET，现在后端也这么声明
    assert configs["h"]["timeout"] == 3  # 显式值不被覆盖
    assert configs["t"]["message"] == "hello"  # test 节点的回显内容（没接线时用它）
    # 原 dict 不被修改
    assert "trigger" not in raw["nodes"][0]["config"]


# --------------------------------------------------------------------------- ④ 节点执行器
@pytest.mark.asyncio
async def test_executor_start_end_log_test_run() -> None:
    """start -> test -> log -> end 全链路：test 把入口值回显出来，log 收到的是**线上来的**值。

    这里没有全局变量：test 的 ``message`` 是它自己手填的（没接线），它的 ``message`` 出口又
    接到了 log 的 ``message`` 入口 —— 值真的沿边走了一遍。
    """
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "start"),
                node("t", "test", message="hello nacho"),
                node("l", "log", level="INFO"),
                node("e", "end"),
            ],
            "edges": [
                edge("s", "t"),
                edge("t", "l", "message", "message"),  # test 的回显 -> log 的日志内容
                edge("l", "e"),
            ],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)
    # 日志收集器按顺序记了节点，log 那行是 test 送过来的值
    assert any("[test] t: hello nacho" in line for line in ctx.log)
    assert any("[INFO] l: hello nacho" in line for line in ctx.log)
    # 最后一个节点（end）没接线的入口：触发边不送值
    assert ctx.inputs == {}


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
    # 主流程照常跑完（start -> end），孤儿一点痕迹都没留下
    assert any("[start]" in line for line in ctx.log)
    assert any("[end]" in line for line in ctx.log)


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
async def test_executor_ignores_data_edges_from_nodes_that_never_ran() -> None:
    """孤儿连出来的线**不算数**：别拿空串把手填的兜底值顶掉（与校验器同一口径）。

    constant 没接触发线（孤儿）→ 永不执行，却挂着一根 ``value -> log.message``。那根线不该
    被当成「上游送来了空串」：校验器认为它不算数（所以手填值满足必填入口），运行器也得这么算，
    log 才会用手填的 ``config.message``。
    """
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "start"),
                node("l", "log", message="手填的内容"),
                node("c", "constant", value="孤儿常量"),
                node("e", "end"),
            ],
            "edges": [edge("s", "l"), edge("l", "e"), edge("c", "l", "value", "message")],
        }
    )
    assert validate_graph(graph).valid  # 校验放行：孤儿那根线不算数，手填值就够了

    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)
    assert any("[INFO] l: 手填的内容" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_executor_still_sends_empty_for_wired_port_without_value() -> None:
    """上游**跑过了**、只是那个出口没产出（时间触发的 start 没有 message）→ 照旧送空串。

    这跟「上游根本没跑」是两回事：接的线算数、线上确实没值，空串会盖掉手填值（有意为之，
    见 ``_inputs_of`` 的文档）。别把这条语义一起改掉了。
    """
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "start", trigger="time", cron="*/5 * * * *"),
                node("l", "log", message="手填的内容"),
                node("e", "end"),
            ],
            "edges": [
                edge("s", "l"),
                edge("s", "l", "message", "message"),  # 时间触发没有 message 产出
                edge("l", "e"),
            ],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)
    assert any(line == "[INFO] l: " for line in ctx.log)  # 线上来了个空串
    assert not any("手填的内容" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_executor_start_time_trigger_registers_only_when_priming() -> None:
    """只有「登记那一趟」才动调度器（拨运行开关 / 启动载入 / 发布新版走的都是这一趟）。

    task_id = ``wf-<工作流 id>-<节点 id>``（工作流 + 节点两级，避免不同图的同名节点撞车）；
    重复登记是幂等的：同名旧任务先摘掉再加。
    """
    from nacho.core.scheduler import TaskManager

    scheduler = TaskManager()

    async def run_workflow() -> None: ...

    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "start", trigger="time", cron="*/5 * * * *", name="每5分钟"),
                node("e", "end"),
            ],
            "edges": [edge("s", "e")],
        }
    )
    ctx = NodeExecutionContext(
        scheduler=scheduler, run=run_workflow, workflow_id="demo", register_triggers=True
    )
    await SimpleWorkflowRunner().run(graph, ctx)
    task = scheduler.get("wf-demo-s")
    assert task.name == "每5分钟"

    # 再登记一遍：不报错，仍是同一个 task_id
    await SimpleWorkflowRunner().run(graph, ctx)
    assert scheduler.get("wf-demo-s") is not None


@pytest.mark.asyncio
async def test_executor_start_time_trigger_leaves_scheduler_alone_while_running() -> None:
    """整图执行（cron 到点那一趟）**不碰调度器**：它自己会排下一次。

    回归：以前每次执行都先「摘掉再登记」，等于每跑一次就换一个新的任务对象 ——
    ``run_count`` / ``last_run`` 这些运行统计被清零，连「上一次还没跑完就跳过本次」的
    单实例保护（看 ``task.active``）也一并失效了。
    """
    from nacho.core.scheduler import TaskManager

    scheduler = TaskManager()
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [node("s", "start", trigger="time", cron="*/5 * * * *"), node("e", "end")],
            "edges": [edge("s", "e")],
        }
    )
    # 先把任务登记上（这一趟才是登记）
    priming_ctx = NodeExecutionContext(
        scheduler=scheduler, workflow_id="demo", register_triggers=True
    )
    await SimpleWorkflowRunner().run(graph, priming_ctx)
    task = scheduler.get("wf-demo-s")
    task.run_count = 7  # 假装已经跑过好几轮

    # 再跑一遍 = 到点执行那一趟：同一个任务对象，统计原样
    ctx = NodeExecutionContext(scheduler=scheduler, workflow_id="demo")
    await SimpleWorkflowRunner().run(graph, ctx)
    assert scheduler.get("wf-demo-s") is task
    assert task.run_count == 7
    assert any("执行中" in line for line in ctx.log)


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
    assert ctx.inputs == {}  # 触发边不送值，end 什么都没收到
    assert any("消息触发" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_executor_start_time_trigger_without_scheduler_skips_gracefully() -> None:
    """登记那一趟没注入调度器时（离线 / 测试）：只记一条 warning，不抛异常。"""
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [node("s", "start", trigger="time", cron="*/5 * * * *"), node("e", "end")],
            "edges": [edge("s", "e")],
        }
    )
    ctx = NodeExecutionContext(register_triggers=True)
    await SimpleWorkflowRunner().run(graph, ctx)
    assert any("未注入调度器" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_executor_start_time_trigger_running_pass_needs_no_scheduler() -> None:
    """执行那一趟本来就不碰调度器：没注入也照跑，不该报「未注入调度器」。"""
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [node("s", "start", trigger="time", cron="*/5 * * * *"), node("e", "end")],
            "edges": [edge("s", "e")],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)
    assert any("执行中" in line for line in ctx.log)
    assert not any("未注入调度器" in line for line in ctx.log)


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
    return WorkflowNode.model_construct(id="h1", type="http", config=merged)


@pytest.mark.asyncio
async def test_http_node_takes_url_and_body_from_wires(
    fake_http: type[FakeAsyncClient],
) -> None:
    """url / body 可以走连线（线上的值覆盖手填值）；状态码与正文从两个出口产出。"""
    fake_http.status = 201
    fake_http.text = '{"id": 7}'
    node_obj = http_node(
        url="http://ignored.example.com",  # 被线上来的值覆盖
        method="post",
        headers={"X-Robot": "r-001"},  # headers 只能手写（没有对应端口）
        timeout=3,
    )
    ctx = NodeExecutionContext()
    ctx.inputs = {"url": "https://api.example.com/items/7", "body": '{"id": 7}'}

    outputs = await exec_http(node_obj, ctx)

    assert outputs == {"http_status": 201, "http_body": '{"id": 7}'}
    assert fake_http.calls == [
        {
            "method": "POST",  # 方法大小写不敏感
            "url": "https://api.example.com/items/7",
            "headers": {"X-Robot": "r-001"},
            "content": '{"id": 7}',
        }
    ]
    assert fake_http.client_kwargs["timeout"] == 3.0
    assert any("POST" in line and "201" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_http_node_uses_config_when_not_wired(fake_http: type[FakeAsyncClient]) -> None:
    """没接线就用 config 里手填的 url / body：入口值「线上优先，没有才用手填」。"""
    await exec_http(http_node(url="https://x/y", body="raw"), NodeExecutionContext())

    assert fake_http.calls[0]["url"] == "https://x/y"
    assert fake_http.calls[0]["content"] == "raw"


@pytest.mark.asyncio
async def test_http_status_reaches_downstream_message_input(
    fake_http: type[FakeAsyncClient],
) -> None:
    """http 的 ``http_status`` 出口接到 log 的 ``message`` 入口：值真的沿边走完整条链路。"""
    fake_http.status = 503
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "start"),
                node("h", "http", url="https://api.example.com", method="GET"),
                node("l", "log", level="WARNING"),
                node("e", "end"),
            ],
            "edges": [
                edge("s", "h"),
                edge("h", "l", "http_status", "message"),  # 状态码 -> 日志内容
                edge("h", "e"),  # 触发边继续往下走
                edge("l", "e"),
            ],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)
    assert any("[WARNING] l: 503" in line for line in ctx.log)


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


# ------------------------------------------------------------- ④-C 常量
@pytest.mark.asyncio
async def test_constant_node_produces_one_value_on_its_port() -> None:
    """一个常量节点就一个值：从 ``value`` 出口送给下游连上来的入口。"""
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "start"),
                node("c", "constant", value="https://api.example.com"),
                node("l", "log"),
                node("e", "end"),
            ],
            "edges": [
                edge("s", "c"),
                edge("c", "l", "value", "message"),  # 常量 -> log 的日志内容
                edge("l", "e"),
            ],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)
    assert any("https://api.example.com" in line for line in ctx.log)


@pytest.mark.asyncio
async def test_multiple_constants_are_multiple_nodes() -> None:
    """要几个常量就摆几个节点：各自的线互不串（以前是一个节点塞一组「名字 -> 值」）。"""
    graph = WorkflowGraph.model_validate(
        {
            "nodes": [
                node("s", "start"),
                node("c1", "constant", value="第一"),
                node("c2", "constant", value="第二"),
                node("l1", "log", level="INFO"),
                node("l2", "log", level="WARNING"),
                node("e", "end"),
            ],
            "edges": [
                edge("s", "c1"),
                edge("c1", "l1", "value", "message"),
                edge("c1", "c2"),  # 触发边：先 c1 再 c2
                edge("c2", "l2", "value", "message"),
                edge("l1", "e"),
                edge("l2", "e"),
            ],
        }
    )
    ctx = NodeExecutionContext()
    await SimpleWorkflowRunner().run(graph, ctx)
    assert any("[INFO] l1: 第一" in line for line in ctx.log)
    assert any("[WARNING] l2: 第二" in line for line in ctx.log)


def test_constant_value_is_required() -> None:
    """常量节点的 ``value`` 是必填字段：没写报 MISSING_CONFIG。"""
    graph = {
        "nodes": [node("s", "start"), node("c", "constant"), node("e", "end")],
        "edges": [edge("s", "c"), edge("c", "e")],
    }
    report = validate_graph(graph)
    assert not report.valid and report.stage == STAGE_SEMANTIC
    assert any(i.node_id == "c" and i.code == "MISSING_CONFIG" for i in report.errors)


def test_constant_must_be_wired_to_be_read() -> None:
    """值只能沿边走：常量接到下游才读得到；常量成了孤儿，下游那个入口就是空的。"""
    nodes = [
        node("s", "start"),
        node("c", "constant", value="https://api.example.com"),
        node("l", "log"),
        node("e", "end"),
    ]
    wired = {
        "nodes": nodes,
        "edges": [edge("s", "c"), edge("c", "l", "value", "message"), edge("l", "e")],
    }
    assert validate_graph(wired).valid

    # 常量没接进主流程（孤儿）：它不执行，log 的 message 入口也就没有线 —— 报没接上
    orphan = {"nodes": nodes, "edges": [edge("s", "l"), edge("l", "e")]}
    report = validate_graph(orphan)
    assert not report.valid
    assert any(
        issue.node_id == "l" and issue.code == "INPUT_NOT_CONNECTED" for issue in report.errors
    )


# --------------------------------------------------------------------------- ⑤ 自写节点
def test_builtin_node_executors_are_registered() -> None:
    """包一被 import，内置节点的执行函数就都登记好了（一类一个文件，各自注册）。"""
    for node_type in ("start", "end", "log", "test", "http", "constant"):
        assert get_executor(node_type) is not None
    assert set(registered_types()) >= {"start", "end", "log", "test", "http", "constant"}


def test_builtin_field_metadata_is_declared_in_backend() -> None:
    """枚举选项与默认值都写在注册表里，画布照单渲染（不再自己填 GET / INFO / hello）。

    这几条以前只活在前端的节点表里（后端没声明），是两边最容易各自漂移的地方：
    ``test`` 的 message 字段、``http.method`` 的缺省 GET、``log.level`` / ``start.trigger``
    的可选值。声明清楚了，「后端提供什么、画布显示什么」才立得住。
    """
    from nacho.workflow import get_spec
    from nacho.workflow.nodes import HTTP_METHODS

    http = get_spec("http")
    assert http is not None
    method = next(f for f in http.fields if f.name == "method")
    assert method.default == "GET"
    assert method.options is not None
    assert set(method.options) == HTTP_METHODS  # 下拉选项与校验规则同一份

    log = get_spec("log")
    assert log is not None
    level = next(f for f in log.fields if f.name == "level")
    assert level.default == "INFO"
    assert level.options == ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

    start = get_spec("start")
    assert start is not None
    start_fields = {f.name: f for f in start.fields}
    assert start_fields["trigger"].default == "message"
    assert start_fields["trigger"].options == ("message", "time")
    assert "cron" in start_fields  # 时间形态用到的字段也照实声明
    assert "name" in start_fields

    test = get_spec("test")
    assert test is not None
    assert [f.name for f in test.fields] == ["message"]
    assert test.fields[0].default == "hello"


def test_builtin_node_ports_and_labels_are_declared() -> None:
    """内置节点的中文名 / 面板顺序 / 端口都在注册表里（画布照它画，不再自己维护一份）。

    端口是**图的执行契约**：edge 的 ``source_port`` / ``target_port`` 存的就是这些 id，
    ``message`` 端口送值、``trigger`` 端口只表达先后；字段名与端口 id 同名的（``log`` 的
    ``message``、``http`` 的 ``url``）就是「可以被连线覆盖的那个入口」。
    """
    from nacho.workflow import get_spec

    expected: dict[str, tuple[int, str, list[str], list[str]]] = {
        "start": (10, "开始", [], ["trigger", "message"]),
        "end": (20, "结束", ["trigger"], []),
        "constant": (30, "常量", ["trigger"], ["trigger", "value"]),
        "log": (40, "写日志", ["trigger", "message"], ["trigger"]),
        "test": (50, "测试", ["trigger", "message"], ["trigger", "message"]),
        "http": (60, "HTTP", ["trigger", "url", "body"], ["trigger", "http_status", "http_body"]),
    }
    orders: list[int] = []
    for node_type, (order, label, inputs, outputs) in expected.items():
        spec = get_spec(node_type)
        assert spec is not None
        assert spec.order == order, node_type
        assert spec.label == label, node_type
        assert [p.id for p in spec.inputs] == inputs, node_type
        assert [p.id for p in spec.outputs] == outputs, node_type
        orders.append(spec.order)
    assert orders == sorted(orders)  # 面板顺序：内置节点依次排开、互不打架

    # 端口类型也要在：trigger 是控制流、message 是数据流，连线时按它配对
    log = get_spec("log")
    assert log is not None
    assert [(p.id, p.type) for p in log.inputs] == [
        ("trigger", "trigger"),
        ("message", "message"),
    ]
    assert log.inputs[0].label == "触发"  # 显示名同样来自后端
    assert log.inputs[1].required is True  # 必填入口：接线或手填同名字段
    assert log.inputs[0].required is False  # 触发端口不谈必填

    http = get_spec("http")
    assert http is not None
    http_inputs = {p.id: p for p in http.inputs}
    assert http_inputs["url"].required is True  # url 是必填入口
    assert http_inputs["body"].required is False  # body 可选
    assert [(p.id, p.type) for p in http.outputs] == [
        ("trigger", "trigger"),
        ("http_status", "message"),
        ("http_body", "message"),
    ]


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
        return {"echo": node.id, "seen": len(ctx.inputs)}

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
    """自写的节点类型：声明输入 / 输出端口后，引擎按边把值送进来、再按出口送下去。

    这里直接构造图（不经过校验）：本条验的是「注册表 + 引擎投递」这条链路。
    校验那一关的合法性同样查注册表（见 test_validation_rules_are_driven_by_registration_*）。
    """
    seen: list[str] = []

    @register_node(
        "my-upper",
        inputs=[PortSpec("text", "message", "文本")],
        outputs=[PortSpec("upper", "message", "大写")],
    )
    async def exec_my_upper(node: WorkflowNode, ctx: NodeExecutionContext) -> dict[str, Any]:
        text = str(input_value(node, ctx, "text"))
        seen.append(text)
        return {"upper": text.upper()}

    graph = WorkflowGraph(
        nodes=[
            WorkflowNode(id="s", type="start", config={}),
            WorkflowNode(id="c", type="constant", config={"value": "hi nacho"}),
            WorkflowNode(id="u", type="my-upper", config={}),
        ],
        edges=[
            WorkflowEdge(source="s", target="c", source_port="trigger", target_port="trigger"),
            # 常量节点的 value 出口 -> 自写节点的 text 入口
            WorkflowEdge(source="c", target="u", source_port="value", target_port="text"),
        ],
    )
    ctx = NodeExecutionContext()

    await SimpleWorkflowRunner().run(graph, ctx)

    assert seen == ["hi nacho"]  # 值确实沿 c.value -> u.text 送到了
    assert ctx.inputs == {"text": "hi nacho"}  # 最后一个节点的入口就是它收到的那份


# --------------------------------------------------------------------------- checksum
def test_checksum_stable_under_key_order_and_spacing() -> None:
    """同一张图不同写法（键序、空白）算同一个摘要；内容变了摘要才变。"""
    first = canonical_graph_json(linear_graph())
    reordered = {"edges": [
        {"targetPort": "trigger", "target": "e", "sourcePort": "trigger", "source": "s"},
    ], "nodes": [
        {"config": {}, "type": "start", "id": "s"},
        {"config": {}, "type": "end", "id": "e"},
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

    # 端口是图内容的一部分：同样的连线、没写端口，摘要就不同（缺省端口只影响运行期口径）
    no_ports = {
        "nodes": [node("s", "start"), node("e", "end")],
        "edges": [{"source": "s", "target": "e"}],
    }
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


async def test_definition_enabled_defaults_off_and_toggles() -> None:
    """运行开关：新建默认关（发布 ≠ 运行），能拨开能拨回，不存在返回 ``None``。"""
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    try:
        await store.ensure_schema()
        created = await store.create("u-admin", "开关流")
        assert created.enabled is False  # 默认不跑

        turned_on = await store.set_enabled(created.id, True)
        assert turned_on is not None and turned_on.enabled is True
        stored = await store.get(created.id)
        assert stored is not None and stored.enabled is True  # 真写进去了

        turned_off = await store.set_enabled(created.id, False)
        assert turned_off is not None and turned_off.enabled is False
        assert await store.set_enabled("not-exist", True) is None
    finally:
        await engine.dispose()


async def test_definition_settings_default_single_instance_and_update() -> None:
    """工作流**设置**（实例策略）：新建默认**单实例**，能改成多实例再改回来；不存在返回 ``None``。

    设置与运行开关 / 发布指针各管各的：改设置不该顺手动了那两样。
    """
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    try:
        await store.ensure_schema()
        created = await store.create("u-admin", "设置流")
        assert created.multi_instance is False  # 默认单实例

        updated = await store.update_settings(created.id, multi_instance=True)
        assert updated is not None and updated.multi_instance is True
        stored = await store.get(created.id)
        assert stored is not None and stored.multi_instance is True  # 真写进去了
        assert stored.enabled is False  # 只碰设置：开关没被顺手拨开

        back = await store.update_settings(created.id, multi_instance=False)
        assert back is not None and back.multi_instance is False
        assert await store.update_settings("not-exist", multi_instance=True) is None
    finally:
        await engine.dispose()


async def test_old_definition_table_gets_the_added_columns() -> None:
    """老库（建表时还没有 enabled / multi_instance 列）在 ``ensure_schema`` 时补上，都填 0。

    补列不能让升级上来的库突然开始跑、也不能让定时任务突然变成多实例 —— 所以 ALTER 的默认值
    取「关」和「单实例」（见 store 的 ``_DEFINITION_ADDED_COLUMNS``）。
    """
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        # 造一张「老表」：只有最早的几列，没有 enabled / 暂存区那几列
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "CREATE TABLE workflow_definitions ("
                "id VARCHAR(64) PRIMARY KEY, owner_id VARCHAR(64), name VARCHAR(128),"
                " status VARCHAR(16), current_version INTEGER, published_version INTEGER,"
                " created_at FLOAT, updated_at FLOAT)"
            )
            await conn.exec_driver_sql(
                "INSERT INTO workflow_definitions (id, owner_id, name, status,"
                " current_version, published_version, created_at, updated_at)"
                " VALUES ('wf-old', 'u-admin', '老数据', 'published', 1, 1, 1.0, 1.0)"
            )

        store = SqlWorkflowStore(engine)
        await store.ensure_schema()  # 建表跳过（已存在）+ 补增量列

        old = await store.get("wf-old")
        assert old is not None
        assert old.enabled is False  # 升级上来默认「不跑」
        assert old.multi_instance is False  # 实例策略默认「单实例」
        assert old.published_version == 1  # 别的列没被碰
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------- HTTP 接口
def api_app() -> FastAPI:
    """接口层应用（演示账号在 lifespan 里种好；工作流双表在同一块内存 sqlite）。"""
    return create_app(ApiOptions(prefix="/api"), hasher=_TEST_HASHER)


class FakeTriggers:
    """假触发器：只记账（``start`` / ``stop`` 各被叫了几次、拿的哪一版）。

    验的是「接口层有没有按开关去即时启停」，不用真调度器（那个由运行时那组用例覆盖）。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def start(self, workflow_id: str, version: int) -> int:
        self.calls.append(("start", workflow_id, version))
        return 1

    async def stop(self, workflow_id: str, version: int) -> int:
        self.calls.append(("stop", workflow_id, version))
        return 1


def api_app_with_triggers(triggers: FakeTriggers) -> FastAPI:
    """接口层应用 + 运行时触发器（主程序就是这么传的，见 ``nacho.bootstrap``）。"""
    return create_app(
        ApiOptions(prefix="/api"), hasher=_TEST_HASHER, workflow_triggers=triggers
    )


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


async def test_api_node_types_catalog_matches_registry() -> None:
    """``GET /workflows/node-types``：画布要的 label / 端口 / 字段全从这儿来，顺序也排好了。

    这就是「后端提供什么、画布显示什么」的那份数据 —— 没有它，前端只能自己维护一份节点表，
    两边迟早漂移（``test`` 的 echo 字段就是这么漂出去的）。
    """
    async with api_client(api_app()) as client:
        token = await login(client, ADMIN)
        response = await client.get("/api/workflows/node-types", headers=auth(token))
        assert response.status_code == 200, response.text
        payload = response.json()["data"]

    nodes = {item["type"]: item for item in payload["nodes"]}
    assert set(nodes) == set(registered_types())  # 注册了什么就有什么

    http = nodes["http"]
    assert http["label"] == "HTTP"
    assert http["role"] == "normal"
    assert http["has_executor"] is True
    assert [port["id"] for port in http["outputs"]] == ["trigger", "http_status", "http_body"]
    assert http["outputs"][2]["label"] == "响应正文"  # 端口显示名也来自后端
    assert [port["type"] for port in http["outputs"]] == ["trigger", "message", "message"]
    # 输入端口：触发 + url（必填入口）+ body（可选）
    assert [(port["id"], port["required"]) for port in http["inputs"]] == [
        ("trigger", False),
        ("url", True),
        ("body", False),
    ]
    method = next(field for field in http["fields"] if field["name"] == "method")
    assert method["default"] == "GET" and method["has_default"] is True
    assert method["options"] == ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    url = next(field for field in http["fields"] if field["name"] == "url")
    # url 的「必填」落在入口上：字段本身没默认值，接线或手填都行
    assert url["required"] is False and url["has_default"] is False and url["default"] is None

    end = nodes["end"]
    assert end["max_outgoing"] == 0 and end["outputs"] == []
    orders = [item["order"] for item in payload["nodes"]]
    assert orders == sorted(orders)  # 面板顺序：接口给的就已经排好


async def test_api_trace_id_is_filled_in_every_workflow_response() -> None:
    """每个工作流响应的 body 里都要有 trace_id（= 响应头 X-Trace-Id），不能是占位符 "-"。

    以前只有写操作（新建 / 改名 / 暂存 / 提交 / 发布）填了它，只读接口落到响应壳的默认值
    ``"-"`` —— 响应头一直是对的，缺的是 body 这个字段，于是排障时用户只能报一个 "-"。
    """
    async with api_client(api_app()) as client:
        token = await login(client, ADMIN)
        created = await client.post(
            "/api/workflows", headers=auth(token), json={"name": "带编号的流"}
        )
        workflow_id = created.json()["data"]["id"]

        for path in (
            "/api/workflows",
            f"/api/workflows/{workflow_id}",
            f"/api/workflows/{workflow_id}/draft",
            f"/api/workflows/{workflow_id}/versions",
            "/api/workflows/node-types",
        ):
            response = await client.get(path, headers=auth(token))
            assert response.status_code == 200, (path, response.text)
            body_id = response.json()["trace_id"]
            assert body_id != "-", f"{path} 的 trace_id 是占位符"
            assert body_id == response.headers["X-Trace-Id"], path

        # 校验接口（业务结果走 200）同样要带上
        checked = await client.post(
            "/api/workflows/validate", headers=auth(token), json={"graph": {"nodes": []}}
        )
        assert checked.json()["trace_id"] == checked.headers["X-Trace-Id"]


async def test_api_delete_workflow_stops_its_scheduled_tasks() -> None:
    """删工作流要**先把定时触发摘掉**：只删库的话任务还在调度器里，到点空跑一趟。

    顺序是先摘后删 —— 任务名要照这一版的图算，图没了就算不出来。
    """
    triggers = FakeTriggers()
    async with api_client(api_app_with_triggers(triggers)) as client:
        token = await login(client, ADMIN)
        created = await client.post(
            "/api/workflows", headers=auth(token), json={"name": "待删的流"}
        )
        workflow_id = created.json()["data"]["id"]
        saved = await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(token),
            json={"graph": linear_graph()},
        )
        assert saved.status_code == 201, saved.text
        published = await client.post(
            f"/api/workflows/{workflow_id}/publish", headers=auth(token), json={}
        )
        assert published.status_code == 200, published.text

        deleted = await client.delete(f"/api/workflows/{workflow_id}", headers=auth(token))
        assert deleted.status_code == 204

    # 发布时开关还默认关着（不会 start），删除时应当 stop 一次、带着发布版本号
    assert ("stop", workflow_id, 1) in triggers.calls


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
    """启动载入：**开着运行开关**的已发布定时流才把 start 登记到调度器。

    「已发布但开关关着」与「只存了版本没发布」两种都不登记 —— 发布 ≠ 运行。
    """
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
    assert await store.set_enabled(published_def.id, True) is not None  # 拨开开关才算「要跑」

    # 只有版本、没发布的工作流不该被载入（消息触发也不会登记任务）
    draft_def = await store.create("u-admin", "草稿流")
    await store.add_version(
        draft_def,
        graph_json=canonical_graph_json(linear_graph()),
        checksum=graph_checksum(linear_graph()),
    )

    # 发布过、但开关没拨开的：同样不登记（另起一个节点 id，免得和上面那个撞 task_id）
    off_graph = {
        "nodes": [node("so", "start", trigger="time", cron="*/5 * * * *"), node("e", "end")],
        "edges": [edge("so", "e")],
    }
    off_def = await store.create("u-admin", "发了但不跑的流")
    await store.add_version(
        off_def,
        graph_json=canonical_graph_json(off_graph),
        checksum=graph_checksum(off_graph),
    )
    assert await store.publish(off_def.id, 1) is not None

    scheduler = TaskManager()
    try:
        loaded = await load_published_workflows(store, scheduler)
        assert loaded == 1
        assert scheduler.get(f"wf-{published_def.id}-s") is not None
        # 开关关着的不登记（`get` 对不存在的任务是抛 KeyError，所以按清单看）
        assert [task.task_id for task in scheduler.list()] == [f"wf-{published_def.id}-s"]
    finally:
        await engine.dispose()


async def test_load_published_workflows_passes_the_instance_strategy_to_the_scheduler() -> None:
    """实例策略是**工作流设置**（定义表里的列）：登记时传给调度器，单 / 多实例各按各的。

    调度器靠 ``Task.multi_instance`` 决定「上一次还没跑完、到点又到点」时是跳过本次还是开新
    实例，所以这条断言的是「设置真的落到了那个任务上」——登记那一趟读定义表，见
    :func:`nacho.workflow.runtime.register_published_workflow`。
    """
    from nacho.core.scheduler import TaskManager
    from nacho.workflow.runtime import load_published_workflows

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    await store.ensure_schema()

    graph = {
        "nodes": [node("s", "start", trigger="time", cron="*/5 * * * *"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    plain = await store.create("u-admin", "单实例流")
    stacked = await store.create("u-admin", "多实例流")
    for definition in (plain, stacked):
        await store.add_version(
            definition,
            graph_json=canonical_graph_json(graph),
            checksum=graph_checksum(graph),
        )
        assert await store.publish(definition.id, 1) is not None
        assert await store.set_enabled(definition.id, True) is not None
    assert await store.update_settings(stacked.id, multi_instance=True) is not None

    scheduler = TaskManager()
    try:
        assert await load_published_workflows(store, scheduler) == 2
        assert scheduler.get(f"wf-{plain.id}-s").multi_instance is False  # 缺省单实例
        assert scheduler.get(f"wf-{stacked.id}-s").multi_instance is True
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
    assert await store.set_enabled(definition.id, True) is not None  # 开关拨开（否则连登记都不做）

    scheduler = TaskManager()
    try:
        assert await load_published_workflows(store, scheduler) == 1
        assert scheduler.get(f"wf-{definition.id}-s") is not None  # 定时开始节点登记上了
        assert ran == []  # 而下游（probe）一次都没跑
    finally:
        await engine.dispose()


async def test_stop_published_workflow_removes_the_registered_tasks() -> None:
    """停用：把这一版登记的定时任务摘掉（**不跑图**），重复停、停不存在的都无害。"""
    from nacho.core.scheduler import TaskManager
    from nacho.workflow.runtime import (
        register_published_workflow,
        stop_published_workflow,
    )

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    await store.ensure_schema()

    graph = {
        "nodes": [node("s", "start", trigger="time", cron="*/5 * * * *"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    definition = await store.create("u-admin", "定时流")
    await store.add_version(
        definition,
        graph_json=canonical_graph_json(graph),
        checksum=graph_checksum(graph),
    )
    assert await store.publish(definition.id, 1) is not None

    scheduler = TaskManager()
    try:
        assert await register_published_workflow(definition.id, 1, store, scheduler) == 1
        assert scheduler.get(f"wf-{definition.id}-s") is not None

        assert await stop_published_workflow(definition.id, 1, store, scheduler) == 1
        assert scheduler.list() == []  # 摘干净了
        # 再停一次 / 停一个不存在的版本：都是 0，不抛
        assert await stop_published_workflow(definition.id, 1, store, scheduler) == 0
        assert await stop_published_workflow(definition.id, 9, store, scheduler) == 0
    finally:
        await engine.dispose()


async def test_workflow_task_ids_carry_the_workflow_id() -> None:
    """任务名 = 工作流 + 节点：两条工作流的**同名**开始节点不会互相顶掉。

    以前只按节点 id 算（``wf-<node.id>``），而节点 id 只在**一张图内**唯一 —— 两条图都有
    ``s`` 时，后登记的会把先登记的那条移除，等于悄悄停掉别人的定时。
    """
    from nacho.core.scheduler import TaskManager
    from nacho.workflow.runtime import load_published_workflows

    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlWorkflowStore(engine)
    await store.ensure_schema()

    graph = {
        "nodes": [node("s", "start", trigger="time", cron="*/5 * * * *"), node("e", "end")],
        "edges": [edge("s", "e")],
    }
    first = await store.create("u-admin", "第一条")
    second = await store.create("u-admin", "第二条")  # 故意用同一个节点 id
    for definition in (first, second):
        await store.add_version(
            definition,
            graph_json=canonical_graph_json(graph),
            checksum=graph_checksum(graph),
        )
        assert await store.publish(definition.id, 1) is not None
        assert await store.set_enabled(definition.id, True) is not None

    scheduler = TaskManager()
    try:
        assert await load_published_workflows(store, scheduler) == 2
        task_ids = sorted(task.task_id for task in scheduler.list())
        assert task_ids == sorted([f"wf-{first.id}-s", f"wf-{second.id}-s"])
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


# --------------------------------------------------------------------------- 运行开关
def _timed_graph(cron: str = "*/5 * * * *") -> dict[str, object]:
    """一张最小的定时图（换 cron 就换 checksum，用来造第二个版本）。"""
    return {
        "nodes": [node("s", "start", trigger="time", cron=cron), node("e", "end")],
        "edges": [edge("s", "e")],
    }


async def test_api_enabled_switch_and_published_snapshot() -> None:
    """开关接口：新建默认关、发布 ≠ 运行、拨开即时登记、关掉即时摘掉；已发布的那一份读得到。

    这是这次改动的核心口径：**发布只挪指针**（不登记、不执行图），跑不跑由开关说了算。
    """
    triggers = FakeTriggers()
    async with api_client(api_app_with_triggers(triggers)) as client:
        token = await login(client, ADMIN)
        created = await client.post(
            "/api/workflows", headers=auth(token), json={"name": "开关流"}
        )
        workflow_id = created.json()["data"]["id"]
        assert created.json()["data"]["enabled"] is False  # 新建就是「不跑」

        # 还没发布：拨开是 409（没东西可跑），而且一次都没碰触发器
        early = await client.put(
            f"/api/workflows/{workflow_id}/enabled",
            headers=auth(token),
            json={"enabled": True},
        )
        assert early.status_code == 409
        assert triggers.calls == []

        await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(token),
            json={"graph": _timed_graph(), "note": "首版"},
        )
        published = await client.post(
            f"/api/workflows/{workflow_id}/publish", headers=auth(token), json={}
        )
        assert published.status_code == 200
        assert published.json()["data"]["enabled"] is False
        assert triggers.calls == []  # 发布不跑、也不登记

        # 「已发布的那一份」：开关状态 + 版本 + 图，一次看全
        snapshot = await client.get(
            f"/api/workflows/{workflow_id}/published", headers=auth(token)
        )
        assert snapshot.status_code == 200
        body = snapshot.json()["data"]
        assert body["workflow"]["published_version"] == 1
        assert body["workflow"]["enabled"] is False
        assert [item["id"] for item in body["version"]["graph"]["nodes"]] == ["s", "e"]

        # 拨开 → 即时按已发布版本登记
        turned_on = await client.put(
            f"/api/workflows/{workflow_id}/enabled",
            headers=auth(token),
            json={"enabled": True},
        )
        assert turned_on.status_code == 200
        assert turned_on.json()["data"]["enabled"] is True
        assert triggers.calls == [("start", workflow_id, 1)]

        # 关掉 → 即时摘掉
        turned_off = await client.put(
            f"/api/workflows/{workflow_id}/enabled",
            headers=auth(token),
            json={"enabled": False},
        )
        assert turned_off.json()["data"]["enabled"] is False
        assert triggers.calls[-1] == ("stop", workflow_id, 1)


async def test_api_workflow_settings_apply_and_reregister() -> None:
    """设置接口：改「实例策略」落库并回在响应里；**已经在跑的**会即时按新设置重新登记。

    这里用假触发器记账 —— 验的是接口层在设置变化后有没有按已发布版本重新登记；
    「新设置真的传给了调度器」由运行时那条用例（真调度器）覆盖。
    """
    triggers = FakeTriggers()
    async with api_client(api_app_with_triggers(triggers)) as client:
        token = await login(client, ADMIN)
        created = await client.post(
            "/api/workflows", headers=auth(token), json={"name": "设置流"}
        )
        workflow_id = created.json()["data"]["id"]
        assert created.json()["data"]["multi_instance"] is False  # 新建就是单实例

        # 还没发布 / 开关关着：只落库，不碰触发器
        saved = await client.put(
            f"/api/workflows/{workflow_id}/settings",
            headers=auth(token),
            json={"multi_instance": True},
        )
        assert saved.status_code == 200
        assert saved.json()["data"]["multi_instance"] is True
        assert triggers.calls == []

        detail = await client.get(f"/api/workflows/{workflow_id}", headers=auth(token))
        assert detail.json()["data"]["multi_instance"] is True  # 读回来也是新值

        # 发布 + 拨开开关：按已发布版本登记一次
        await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(token),
            json={"graph": _timed_graph(), "note": "首版"},
        )
        await client.post(
            f"/api/workflows/{workflow_id}/publish", headers=auth(token), json={}
        )
        await client.put(
            f"/api/workflows/{workflow_id}/enabled",
            headers=auth(token),
            json={"enabled": True},
        )
        assert triggers.calls == [("start", workflow_id, 1)]

        # 正在跑的时候改设置：即时按新设置重新登记一遍（登记幂等，同名任务被换掉）
        again = await client.put(
            f"/api/workflows/{workflow_id}/settings",
            headers=auth(token),
            json={"multi_instance": False},
        )
        assert again.json()["data"]["multi_instance"] is False
        assert triggers.calls == [("start", workflow_id, 1), ("start", workflow_id, 1)]


async def test_api_enabled_switch_and_snapshot_are_owner_scoped() -> None:
    """个人隔离：别人的开关与已发布快照都按「不存在」处理（同一个 404）。"""
    async with api_client(api_app()) as client:
        admin_token = await login(client, ADMIN)
        robot_token = await login(client, ROBOT)
        created = await client.post(
            "/api/workflows", headers=auth(admin_token), json={"name": "管理员的定时流"}
        )
        workflow_id = created.json()["data"]["id"]
        await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(admin_token),
            json={"graph": _timed_graph(), "note": "首版"},
        )
        await client.post(
            f"/api/workflows/{workflow_id}/publish", headers=auth(admin_token), json={}
        )

        toggled = await client.put(
            f"/api/workflows/{workflow_id}/enabled",
            headers=auth(robot_token),
            json={"enabled": True},
        )
        snapshot = await client.get(
            f"/api/workflows/{workflow_id}/published", headers=auth(robot_token)
        )
    assert toggled.status_code == 404
    assert snapshot.status_code == 404


async def test_api_publishing_while_switch_off_does_not_register() -> None:
    """开关关着时发布：一次都不登记（发布 ≠ 运行）。"""
    triggers = FakeTriggers()
    async with api_client(api_app_with_triggers(triggers)) as client:
        token = await login(client, ADMIN)
        created = await client.post(
            "/api/workflows", headers=auth(token), json={"name": "不跑的流"}
        )
        workflow_id = created.json()["data"]["id"]
        await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(token),
            json={"graph": _timed_graph(), "note": "首版"},
        )
        await client.post(
            f"/api/workflows/{workflow_id}/publish", headers=auth(token), json={}
        )
    assert triggers.calls == []


async def test_api_publishing_again_while_enabled_re_registers() -> None:
    """开着开关时再发一版：按**新版本**重新登记一遍（别让线上还跑旧版的触发配置）。"""
    triggers = FakeTriggers()
    async with api_client(api_app_with_triggers(triggers)) as client:
        token = await login(client, ADMIN)
        created = await client.post(
            "/api/workflows", headers=auth(token), json={"name": "改过定时的流"}
        )
        workflow_id = created.json()["data"]["id"]
        await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(token),
            json={"graph": _timed_graph(), "note": "首版"},
        )
        await client.post(
            f"/api/workflows/{workflow_id}/publish", headers=auth(token), json={}
        )
        await client.put(
            f"/api/workflows/{workflow_id}/enabled",
            headers=auth(token),
            json={"enabled": True},
        )
        # 第二版：把 cron 改掉（checksum 不同才会真的多一版）
        await client.post(
            f"/api/workflows/{workflow_id}/versions",
            headers=auth(token),
            json={"graph": _timed_graph("*/10 * * * *"), "note": "二版"},
        )
        again = await client.post(
            f"/api/workflows/{workflow_id}/publish", headers=auth(token), json={}
        )
    assert again.json()["data"]["published_version"] == 2
    # **先停旧版、再起新版**：任务名里带节点 id，新版要是改了开始节点，光靠登记时同名覆盖
    # 盖不住旧任务（那会留下一个永远没人摘的定时）
    assert triggers.calls == [
        ("start", workflow_id, 1),
        ("stop", workflow_id, 1),
        ("start", workflow_id, 2),
    ]
