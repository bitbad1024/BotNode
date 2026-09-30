"""Kook 正向 WS 的测试：选项、事件解析、连网关收事件（用本地 WS 服务端模拟 Kook 网关）。

跑在 127.0.0.1 的空闲端口上（每个用例自己挑一个），客户端用 ``websockets``。需要
``websockets``（``pip install "nacho[kook]"``），没装就整文件跳过。
"""
from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import cast

import pytest

pytest.importorskip("websockets", reason="Kook 接入层要装 websockets：pip install \"nacho[kook]\"")

from websockets.asyncio.server import serve  # noqa: E402

from nacho.kook import (  # noqa: E402
    EVENT_TEXT,
    KookActionResponse,
    KookClient,
    KookEvent,
    KookOptions,
    parse_action_response,
    parse_event,
)
from nacho.kook.client import _action_path  # noqa: E402


def free_port() -> int:
    """挑一个当前空闲的端口（让内核分配，测完即释放）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return cast("tuple[str, int]", sock.getsockname())[1]


async def wait_until(predicate, timeout: float = 3.0) -> bool:  # type: ignore[no-untyped-def]
    """轮询等一个条件成立（客户端那条腿是异步跑的，得给它一点时间）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


# --------------------------------------------------------------------------- 选项
def test_options_defaults_and_from_mapping() -> None:
    """缺省值齐全；from_mapping 认的键填进去、多余的键忽略。"""
    options = KookOptions()
    assert options.gateway == ""  # 留空 = 连接前走 gateway/index 动态获取
    assert options.token == ""

    options = KookOptions.from_mapping(
        {"gateway": "wss://x/y", "token": "abc", "heartbeat_interval": 10, "不认的键": 1}
    )
    assert options.gateway == "wss://x/y"
    assert options.token == "abc"
    assert options.heartbeat_interval == 10
    assert options.action_timeout == 30.0  # 没给的回默认


# --------------------------------------------------------------------------- 事件解析
def test_parse_event_and_action_response() -> None:
    """事件解析：字段照收；动作回应 code==0 判 ok。"""
    event = parse_event(
        {
            "type": EVENT_TEXT,
            "channel_type": "GROUP",
            "target_id": "ch-123",
            "author_id": "u-456",
            "msg_id": "m-789",
            "content": "你好",
            "extra": {"k": "v"},
        }
    )
    assert isinstance(event, KookEvent)
    assert event.target_id == "ch-123" and event.author_id == "u-456"
    assert event.content == "你好"
    # 额外字段照单收下（extra="allow"）
    assert "extra" in event.model_extra or "extra" in event.model_fields_set

    ok = parse_action_response({"code": 0, "message": "success", "data": {"msg_id": "m1"}})
    assert ok.ok is True
    bad = parse_action_response({"code": 40001, "message": "参数错误"})
    assert bad.ok is False and bad.message == "参数错误"


def test_action_path_maps_actions() -> None:
    """动作名 -> REST 端点路径（能力表查出来的，不再兜底到 create）。"""
    assert _action_path("send_channel_msg") == "/message/create"
    assert _action_path("send_dm_msg") == "/message/create"
    assert _action_path("delete_msg") == "/message/delete"
    with pytest.raises(ValueError, match="不在注册表"):
        _action_path("未知动作")  # 拼错动作名当场抛，不兜底


async def test_client_call_rejects_bad_action_and_params() -> None:
    """动作层不裸传：动作名拼错 / 缺必填 / 多传未知参数都当场抛（能力表拦下）。"""
    client = KookClient(KookOptions(token="abc"))

    with pytest.raises(ValueError, match="不在注册表"):
        await client.call("send_channel_msg_tpyo", target_id="ch-1", content="hi")

    with pytest.raises(ValueError, match="缺必填参数"):
        await client.call("send_channel_msg", target_id="ch-1")  # 缺 content

    with pytest.raises(ValueError, match="不认识的参数"):
        await client.call("send_channel_msg", target_id="ch-1", content="hi", extra="x")


# --------------------------------------------------------------------------- 连网关收事件
@asynccontextmanager
async def fake_gateway(events: list[dict[str, object]]) -> AsyncGenerator[int, None]:
    """起一个本地 WS 服务端，模拟 Kook 网关：先发 hello，再把 ``events`` 逐条发下来。"""
    port = free_port()

    async def handler(ws) -> None:  # type: ignore[no-untyped-def]
        # hello（signal 1）：带 sn
        await ws.send(json.dumps({"s": 1, "d": {"sn": 100}}))
        for event in events:
            await ws.send(json.dumps({"s": 0, "d": event}))
            await asyncio.sleep(0.05)
        # 挂住，等客户端主动断开
        try:
            await ws.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    server = await serve(handler, "127.0.0.1", port)
    try:
        yield port
    finally:
        server.close()
        await server.wait_closed()


async def test_client_connects_and_receives_events() -> None:
    """连网关 -> 收 hello -> 收事件交给 handler；机器人自身 id 从事件里学到。"""
    received: list[KookEvent] = []

    async def on_event(event: KookEvent) -> None:
        received.append(event)

    event = {
        "type": EVENT_TEXT,
        "channel_type": "GROUP",
        "target_id": "ch-123",
        "author_id": "u-456",
        "msg_id": "m-789",
        "content": "你好",
        "self_id": "bot-1",
    }
    async with fake_gateway([event]) as port:
        client = KookClient(
            KookOptions(gateway=f"ws://127.0.0.1:{port}", token="abc"),
            handler=on_event,
        )
        await client.start()
        try:
            assert await wait_until(lambda: len(received) == 1)
            assert received[0].content == "你好"
            assert client.self_id == "bot-1"  # 从事件里学到
        finally:
            await client.stop()


async def test_client_without_token_cannot_call() -> None:
    """没配 Bot Token：发动作是环境问题，当场抛。"""
    client = KookClient(KookOptions(token=""))
    with pytest.raises(ConnectionError, match="Bot Token"):
        await client.call("send_channel_msg", target_id="ch-1", content="hi")


async def test_client_call_posts_to_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    """发动作走 REST：带 Bot Token 头，回应解析成 KookActionResponse。"""
    client = KookClient(KookOptions(token="abc"))
    captured: dict[str, object] = {}

    class _FakeResp:
        """假的 urlopen 响应：支持上下文管理器 + read()。"""

        def __init__(self, payload: object) -> None:
            self._payload = json.dumps(payload).encode("utf-8")

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return self._payload

    def fake_post(req, timeout: float) -> _FakeResp:
        captured["url"] = req.full_url
        captured["method"] = req.method
        captured["timeout"] = timeout
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp({"code": 0, "message": "success", "data": {"msg_id": "m-1"}})

    monkeypatch.setattr("urllib.request.urlopen", fake_post)

    response: KookActionResponse = await client.call(
        "send_channel_msg", target_id="ch-1", content="你好"
    )
    assert response.ok is True
    assert captured["method"] == "POST"
    assert captured["headers"]["Authorization"] == "Bot abc"
    assert captured["body"] == {"target_id": "ch-1", "content": "你好"}


async def test_client_discovers_gateway_via_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """gateway 留空时，连接前走 gateway/index 拿真实地址（Kook 网关是动态下发的）。"""
    client = KookClient(KookOptions(token="abc"))
    captured: dict[str, object] = {}

    class _FakeResp:
        def __init__(self, payload: object) -> None:
            self._payload = json.dumps(payload).encode("utf-8")

        def __enter__(self) -> _FakeResp:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self) -> bytes:
            return self._payload

    def fake_get(req, timeout: float) -> _FakeResp:  # type: ignore[no-untyped-def]
        captured["url"] = req.full_url
        captured["method"] = req.method
        captured["headers"] = dict(req.headers)
        captured["timeout"] = timeout
        return _FakeResp(
            {"code": 0, "message": "操作成功", "data": {"url": "wss://gw/kook?token=abc&compress=0"}}
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_get)

    url = await client._discover_gateway()
    assert url == "wss://gw/kook?token=abc&compress=0"
    assert captured["method"] == "GET"
    assert "gateway/index" in str(captured["url"])
    assert captured["headers"]["Authorization"] == "Bot abc"
