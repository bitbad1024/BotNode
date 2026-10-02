"""Kook 正向 WS 的测试：选项、事件解析、连网关收事件（用本地 WS 服务端模拟 Kook 网关）。

跑在 127.0.0.1 的空闲端口上（每个用例自己挑一个），客户端用 ``websockets``。需要
``websockets``（``pip install "nacho[kook]"``），没装就整文件跳过。
"""
from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from typing import cast

import pytest

pytest.importorskip("websockets", reason="Kook 接入层要装 websockets：pip install \"nacho[kook]\"")

from websockets.asyncio.server import serve  # noqa: E402

from nacho.platforms.kook import (  # noqa: E402
    EVENT_TEXT,
    KookActionResponse,
    KookClient,
    KookEvent,
    KookOptions,
    parse_action_response,
    parse_event,
)
from nacho.platforms.kook.client import _action_path, _reconnect_delay, _with_resume  # noqa: E402


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


class _FakeResponse:
    """假的 http.client 响应：有 status / read() / close()。"""

    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        pass


class _FakeConnection:
    """假的 ``http.client.HTTPSConnection``：记录请求，按共享计数给脚本响应。

    ``script`` 收 ``(n, ...)`` -> ``(status, payload)``：n 是共享计数里的第几次请求
    （跨重试共享，因为 429 / 连接错误会丢连接重建，计数不能挂在单条连接上）。
    """

    def __init__(
        self,
        host: str,
        *,
        timeout: float,
        script=None,
        captured: dict[str, object] | None = None,
        counter: dict[str, int] | None = None,
    ) -> None:
        self._host = host
        self._timeout = timeout
        self._script = script or (
            lambda n: (200, {"code": 0, "message": "success", "data": {}})
        )
        self._captured = captured
        self._counter = counter

    def request(self, method: str, url: str, body: object = None, headers: object = None) -> None:
        if self._captured is not None:
            self._captured["host"] = self._host
            self._captured["timeout"] = self._timeout
            self._captured["method"] = method
            self._captured["target"] = url
            self._captured["headers"] = dict(headers or {})
            if body is not None:
                self._captured["body"] = json.loads((body or b"").decode("utf-8"))
        if self._counter is not None:
            self._counter["n"] = self._counter.get("n", 0) + 1

    def getresponse(self) -> _FakeResponse:
        n = self._counter.get("n", 1) if self._counter is not None else 1
        status, payload = self._script(n)
        return _FakeResponse(status, payload)

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- 选项
def test_options_defaults_and_from_mapping() -> None:
    """缺省值齐全；from_mapping 认的键填进去、多余的键忽略。"""
    options = KookOptions()
    assert options.gateway == ""  # 留空 = 连接前走 gateway/index 动态获取
    assert options.token == ""
    assert options.heartbeat_jitter == 5.0  # 官方 30 秒 + rand(-5, +5)
    assert options.reconnect_interval == 2.0  # 官方退避序列的基准
    assert options.reconnect_max_interval == 60.0  # 官方：获取 gateway 那一步上限 60
    assert options.rest_min_interval == 0.2
    assert options.rest_max_retries == 3

    options = KookOptions.from_mapping(
        {"gateway": "wss://x/y", "token": "abc", "heartbeat_interval": 10, "不认的键": 1}
    )
    assert options.gateway == "wss://x/y"
    assert options.token == "abc"
    assert options.heartbeat_interval == 10
    assert options.action_timeout == 30.0  # 没给的回默认
    assert options.reconnect_max_interval == 60.0  # 新字段没给也回默认


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
    # 额外字段照单收下（extra="allow"）：extra 没声明，原样进 model_extra
    assert event.model_extra["extra"] == {"k": "v"}

    ok = parse_action_response({"code": 0, "message": "success", "data": {"msg_id": "m1"}})
    assert ok.ok is True
    bad = parse_action_response({"code": 40001, "message": "参数错误"})
    assert bad.ok is False and bad.message == "参数错误"


def test_action_path_maps_actions() -> None:
    """动作名 -> REST 端点路径（能力表查出来的，不再兜底到 create）。"""
    assert _action_path("send_channel_msg") == "/message/create"
    assert _action_path("send_dm_msg") == "/direct-message/create"  # 私聊是另一套端点
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
    """起一个本地 WS 服务端，模拟 Kook 网关：先发 hello，再把 ``events`` 逐条发下来。

    ``events`` 每项是事件体；带上 ``"sn"`` 键（如 ``{"sn": 101, "type": ...}``）就发成
    顶层 sn 的报文，不写 sn 就按序从 101 递增（Kook 事件报文的 sn 在顶层）。
    """
    port = free_port()

    async def handler(ws) -> None:  # type: ignore[no-untyped-def]
        # hello（signal 1）：官方报文 d 里只有 code/session_id，sn 只在事件报文里有
        await ws.send(json.dumps({"s": 1, "d": {"code": 0, "session_id": "sess-1"}}))
        for i, item in enumerate(events):
            body = dict(item)
            sn = body.pop("sn", 100 + i + 1)  # 没给 sn 就按序从 101 编
            await ws.send(json.dumps({"s": 0, "d": body, "sn": sn}))
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

    def fake_conn(host: str, *, timeout: float) -> _FakeConnection:
        return _FakeConnection(host, timeout=timeout, captured=captured)

    monkeypatch.setattr("http.client.HTTPSConnection", fake_conn)

    response: KookActionResponse = await client.call(
        "send_channel_msg", target_id="ch-1", content="你好"
    )
    assert response.ok is True
    assert captured["method"] == "POST"
    assert captured["host"] == "www.kookapp.cn"
    assert captured["headers"]["Authorization"] == "Bot abc"
    assert captured["body"] == {"target_id": "ch-1", "content": "你好"}


async def test_client_discovers_gateway_via_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """gateway 留空时，连接前走 gateway/index 拿真实地址（Kook 网关是动态下发的）。"""
    client = KookClient(KookOptions(token="abc"))
    captured: dict[str, object] = {}

    def fake_conn(host: str, *, timeout: float) -> _FakeConnection:
        return _FakeConnection(
            host,
            timeout=timeout,
            captured=captured,
            script=lambda n: (
                200,
                {
                    "code": 0,
                    "message": "操作成功",
                    "data": {"url": "wss://gw/kook?token=abc&compress=0"},
                },
            ),
        )

    monkeypatch.setattr("http.client.HTTPSConnection", fake_conn)

    url = await client._discover_gateway()
    assert url == "wss://gw/kook?token=abc&compress=0"
    assert captured["method"] == "GET"
    assert "gateway/index" in str(captured["target"])
    assert captured["headers"]["Authorization"] == "Bot abc"

async def test_stop_returns_serve_forever_quickly(monkeypatch: pytest.MonkeyPatch) -> None:
    """stop() 用 Event 唤醒 serve_forever：不等 0.1s 轮询间隔。"""

    async def fake_connect(self) -> None:  # type: ignore[no-untyped-def]
        await asyncio.sleep(3600)  # 一直"连接中"，直到被 stop 取消

    monkeypatch.setattr(KookClient, "_connect_once", fake_connect)
    client = KookClient(KookOptions(token="abc"))
    task = asyncio.create_task(client.serve_forever())
    await asyncio.sleep(0.05)  # 让 serve_forever 先跑起来
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await client.stop()
    await task
    elapsed = loop.time() - t0
    assert elapsed < 0.1  # 远小于原来的轮询间隔 0.1s


def test_reconnect_delay_grows_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    """重连退避：连续失败指数增长、封顶上限；计数清零回到基准。"""
    monkeypatch.setattr("nacho.platforms.kook.client._jitter", lambda: 1.0)  # 去掉抖动便于断言
    options = KookOptions(reconnect_interval=3.0, reconnect_max_interval=30.0)
    assert _reconnect_delay(options, 0) == 3.0  # 重置 / 刚断开
    assert _reconnect_delay(options, 1) == 3.0
    assert _reconnect_delay(options, 2) == 6.0
    assert _reconnect_delay(options, 3) == 12.0
    assert _reconnect_delay(options, 5) == 30.0  # 封顶
    assert _reconnect_delay(options, 10) == 30.0


async def test_client_rest_reuses_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """连接复用：同一 host 连续请求复用一条连接（不再新开）。"""
    client = KookClient(KookOptions(token="abc", rest_min_interval=0.0))
    created: dict[str, int] = {"n": 0}

    def fake_conn(host: str, *, timeout: float) -> _FakeConnection:
        created["n"] += 1
        return _FakeConnection(
            host,
            timeout=timeout,
            script=lambda n: (
                200,
                {"code": 0, "message": "success", "data": {"msg_id": "m"}},
            ),
        )

    monkeypatch.setattr("http.client.HTTPSConnection", fake_conn)
    await client.call("send_channel_msg", target_id="ch-1", content="a")
    await client.call("send_channel_msg", target_id="ch-1", content="b")
    assert created["n"] == 1  # 只建了一条连接


async def test_client_rest_retries_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 瞬时失败按退避重试，最后成功；重试耗尽按环境问题抛。"""
    client = KookClient(KookOptions(token="abc", rest_max_retries=2, rest_min_interval=0.0))
    monkeypatch.setattr("nacho.platforms.kook.client._rest_backoff", lambda attempt: 0.0)  # 别真等退避
    count: dict[str, int] = {"n": 0}

    def script(n: int):
        if n == 1:
            return 429, {"code": 40004, "message": "rate limited"}
        return 200, {"code": 0, "message": "success", "data": {"msg_id": "m-1"}}

    def fake_conn(host: str, *, timeout: float) -> _FakeConnection:
        return _FakeConnection(host, timeout=timeout, counter=count, script=script)

    monkeypatch.setattr("http.client.HTTPSConnection", fake_conn)

    response = await client.call("send_channel_msg", target_id="ch-1", content="hi")
    assert response.ok is True
    assert count["n"] == 2  # 第一次 429，第二次成功

    # 全 429：1 次原始 + rest_max_retries 次重试全失败
    def all_429(n: int):
        return 429, {"code": 40004, "message": "rate limited"}

    count["n"] = 0
    monkeypatch.setattr(
        "http.client.HTTPSConnection",
        lambda host, *, timeout: _FakeConnection(
            host, timeout=timeout, counter=count, script=all_429
        ),
    )
    client2 = KookClient(KookOptions(token="abc", rest_max_retries=2, rest_min_interval=0.0))
    with pytest.raises(ConnectionError, match="多次失败"):
        await client2.call("send_channel_msg", target_id="ch-1", content="hi")
    assert count["n"] == 3  # 原始 1 次 + 重试 2 次


async def test_client_rest_rate_limits_burst(monkeypatch: pytest.MonkeyPatch) -> None:
    """连发多个 REST 请求时按最小间隔限流（两次请求至少隔 rest_min_interval）。"""
    client = KookClient(KookOptions(token="abc", rest_min_interval=0.3))
    count: dict[str, int] = {"n": 0}

    def fake_conn(host: str, *, timeout: float) -> _FakeConnection:
        return _FakeConnection(
            host,
            timeout=timeout,
            counter=count,
            script=lambda n: (200, {"code": 0, "message": "success", "data": {"msg_id": f"m-{n}"}}),
        )

    monkeypatch.setattr("http.client.HTTPSConnection", fake_conn)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    results = await asyncio.gather(
        *[client.call("send_channel_msg", target_id="ch-1", content="hi") for _ in range(3)]
    )
    elapsed = loop.time() - t0
    assert all(r.ok for r in results)
    assert count["n"] == 3
    assert elapsed >= 0.6  # 3 次请求至少隔 2 个最小间隔（0.3s * 2）


# --------------------------------------------------------------------------- 事件 sn / 续传
def test_with_resume_params() -> None:
    """resume 拼参：有 sn/session_id 才拼；都没有原样返回；已有 query 是追加不是覆盖。"""
    # 有 sn + session_id：追加 resume=1&sn=&session_id=
    url = _with_resume("wss://gw/kook?token=abc&compress=0", 5, "sess-1")
    assert "resume=1" in url and "sn=5" in url and "session_id=sess-1" in url
    # sn=0 且无 session_id：全新连接，原样返回
    assert _with_resume("wss://gw/kook?token=abc", 0, "") == "wss://gw/kook?token=abc"
    # 只有 session_id（没收到过事件）：拼 resume + session_id，不带 sn
    url2 = _with_resume("wss://gw/kook?token=abc", 0, "sess-2")
    assert "resume=1" in url2 and "session_id=sess-2" in url2 and "sn=" not in url2


async def test_client_sn_advances_and_drops_replayed_event() -> None:
    """事件 sn 在顶层：收到后推进 self._sn；重放（sn <= 已处理）被丢弃，不重复投递。"""
    received: list[KookEvent] = []

    async def on_event(event: KookEvent) -> None:
        received.append(event)

    event = {
        "type": EVENT_TEXT,
        "channel_type": "GROUP",
        "target_id": "ch-1",
        "author_id": "u-1",
        "content": "hi",
    }
    # sn 101、101（重放）、102：只有 101 和 102 两条进 handler
    async with fake_gateway(
        [{**event, "sn": 101}, {**event, "sn": 101}, {**event, "sn": 102}]
    ) as port:
        client = KookClient(
            KookOptions(gateway=f"ws://127.0.0.1:{port}", token="abc"),
            handler=on_event,
        )
        await client.start()
        try:
            assert await wait_until(lambda: len(received) == 2, timeout=3.0)
            assert client._sn == 102  # 事件 sn 已推进到最大
        finally:
            await client.stop()


async def test_client_heartbeat_pings_latest_sn() -> None:
    """心跳 ping 带上已处理的最大事件 sn（Kook 靠它确认送达，不推进就会反复重传）。"""
    port = free_port()
    pings: list[dict[str, object]] = []

    async def handler(ws) -> None:  # type: ignore[no-untyped-def]
        await ws.send(json.dumps({"s": 1, "d": {"code": 0, "session_id": "sess-1"}}))
        await ws.send(json.dumps({"s": 0, "d": {"type": EVENT_TEXT, "content": "hi"}, "sn": 42}))
        with suppress(Exception):
            async for raw in ws:
                msg = json.loads(cast("str", raw))
                if msg.get("s") == 2:
                    pings.append(msg)

    server = await serve(handler, "127.0.0.1", port)
    received: list[KookEvent] = []

    async def on_event(event: KookEvent) -> None:
        received.append(event)

    client = KookClient(
        KookOptions(
            gateway=f"ws://127.0.0.1:{port}",
            token="abc",
            heartbeat_interval=0.1,  # 别等默认 30 秒
        ),
        handler=on_event,
    )
    try:
        await client.start()
        assert await wait_until(lambda: len(received) == 1)
        # 事件之后至少有一轮心跳带 42（已处理的最大 sn）
        assert await wait_until(
            lambda: any(p.get("sn") == 42 for p in pings), timeout=1.0
        )
    finally:
        await client.stop()
        server.close()
        await server.wait_closed()


# --------------------------------------------------------------------------- 网关信令
class _FakeWs:
    """假的 WS 连接：记下发出去的报文与有没有被关（``closed_evt`` 让测试不用睡等）。"""

    def __init__(self) -> None:
        self.closed = False
        self.closed_evt = asyncio.Event()
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True
        self.closed_evt.set()


def test_reconnect_delay_matches_official_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认退避就是官方序列：2、4（重连）-> 8、16（resume）-> 32 -> 60 封顶。"""
    monkeypatch.setattr("nacho.platforms.kook.client._jitter", lambda: 1.0)  # 去掉抖动
    options = KookOptions()  # reconnect_interval=2.0 / reconnect_max_interval=60.0
    assert _reconnect_delay(options, 1) == 2.0
    assert _reconnect_delay(options, 2) == 4.0
    assert _reconnect_delay(options, 3) == 8.0
    assert _reconnect_delay(options, 4) == 16.0
    assert _reconnect_delay(options, 5) == 32.0
    assert _reconnect_delay(options, 6) == 60.0  # 封顶
    assert _reconnect_delay(options, 9) == 60.0


async def test_run_loop_rediscovers_gateway_after_resume_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """四拍（2、4、8、16）都失败后回到第 1 步：清掉网关地址，下次重新 discover。"""
    monkeypatch.setattr(
        "nacho.platforms.kook.client._reconnect_delay", lambda options, attempts: 0.0
    )

    async def always_fail(self: KookClient) -> bool:
        raise ConnectionError("连不上")

    monkeypatch.setattr(KookClient, "_connect_once", always_fail)
    client = KookClient(KookOptions(token="abc"))
    client._gateway_url = "wss://gw.example/?token=x"  # noqa: SLF001

    await client.start()
    try:
        assert await wait_until(lambda: client._gateway_url == "", timeout=2.0)  # noqa: SLF001
    finally:
        await client.stop()


async def test_reconnect_signal_resets_resume_state_and_closes() -> None:
    """signal 5（reconnect）：清 sn / session_id / 网关地址 + 清队列 + 主动断开。

    官方口径：收到 reconnect 要重新获取 gateway、清空 sn 与消息队列，否则消息会错乱。
    """
    client = KookClient(KookOptions(token="abc"))
    ws = _FakeWs()
    client._ws = ws  # noqa: SLF001
    client._session_id = "sess-1"  # noqa: SLF001
    client._sn = 42  # noqa: SLF001
    client._gateway_url = "wss://gw.example/?token=x"  # noqa: SLF001
    await client._inbox.put(  # noqa: SLF001
        KookEvent(
            type=EVENT_TEXT,
            channel_type="GROUP",
            target_id="ch-1",
            author_id="u-1",
            content="hi",
        )
    )

    await client._handle_raw(  # noqa: SLF001
        json.dumps({"s": 5, "d": {"code": 41008, "err": "Missing params"}})
    )

    assert ws.closed is True  # 主动断开，让 _run_loop 重新走一遍（含重新获取网关）
    assert client._session_id == ""  # noqa: SLF001
    assert client._sn == 0  # noqa: SLF001
    assert client._gateway_url == ""  # 下次重新 discover，不再拿失效地址续传  # noqa: SLF001
    assert client._inbox.empty()  # 队列一并清掉  # noqa: SLF001


async def test_reconnect_signal_without_connection_is_safe() -> None:
    """没连着时收到 reconnect：只清状态，不炸。"""
    client = KookClient(KookOptions(token="abc"))
    client._session_id = "sess-1"  # noqa: SLF001
    client._sn = 7  # noqa: SLF001

    await client._handle_raw(json.dumps({"s": 5, "d": {"code": 40107}}))  # noqa: SLF001

    assert client._session_id == "" and client._sn == 0  # noqa: SLF001


def test_heartbeat_interval_jitters_within_official_window() -> None:
    """心跳间隔带抖动（官方 30 ± 5）：抖动为 0 时精确，间隔很小时抖动同步收敛。"""
    client = KookClient(KookOptions(heartbeat_interval=30.0, heartbeat_jitter=5.0))
    samples = [client._heartbeat_interval() for _ in range(50)]  # noqa: SLF001
    assert all(25.0 <= value <= 35.0 for value in samples)
    assert len(set(samples)) > 1  # 真的抖了，不是固定值

    fixed = KookClient(KookOptions(heartbeat_interval=30.0, heartbeat_jitter=0.0))
    assert fixed._heartbeat_interval() == 30.0  # noqa: SLF001

    short = KookClient(KookOptions(heartbeat_interval=0.1))  # 抖动收敛到 interval/2
    assert 0.05 <= short._heartbeat_interval() <= 0.15  # noqa: SLF001


async def test_pong_signal_releases_heartbeat() -> None:
    """signal 3（pong）：放行正在等它的那条心跳腿。"""
    client = KookClient(KookOptions(token="abc"))

    await client._handle_raw(json.dumps({"s": 3}))  # noqa: SLF001

    assert client._pong.is_set() is True  # noqa: SLF001


async def test_hello_failure_closes_connection() -> None:
    """hello code != 0（token 无效 / 续传被拒）：清续传状态并主动断开，不等网关掐线。"""
    client = KookClient(KookOptions(token="abc"))
    ws = _FakeWs()
    client._ws = ws  # noqa: SLF001
    client._gateway_url = "wss://gw.example/?token=x"  # noqa: SLF001

    await client._handle_raw(json.dumps({"s": 1, "d": {"code": 40103}}))  # noqa: SLF001

    assert ws.closed is True
    assert client._session_id == ""  # noqa: SLF001
    assert client._gateway_url == ""  # 下次重新 discover，全新连接  # noqa: SLF001


async def test_resume_ack_updates_session_id() -> None:
    """signal 6（resume ack）：续传成功后按服务端下发的 session_id 更新，并认下这条连接。"""
    client = KookClient(KookOptions(token="abc"))
    client._session_id = "sess-old"  # noqa: SLF001

    await client._handle_raw(  # noqa: SLF001
        json.dumps({"s": 6, "d": {"session_id": "sess-new"}})
    )

    assert client._session_id == "sess-new"  # noqa: SLF001
    assert client._greeted is True  # noqa: SLF001


async def test_hello_timeout_closes_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """连上后 6 秒（测试里调短）没收到 hello：主动断开，不干等网关掐线。"""
    monkeypatch.setattr("nacho.platforms.kook.client._HELLO_TIMEOUT", 0.05)
    port = free_port()
    closed: list[int] = []

    async def handler(ws) -> None:  # type: ignore[no-untyped-def]
        with suppress(Exception):
            await ws.wait_closed()  # 不发 hello，只挂着
        closed.append(1)

    server = await serve(handler, "127.0.0.1", port)
    client = KookClient(
        KookOptions(
            gateway=f"ws://127.0.0.1:{port}",
            token="abc",
            reconnect_interval=0.05,  # 别等默认 3 秒
        )
    )
    try:
        await client.start()
        assert await wait_until(lambda: len(closed) >= 1, timeout=2.0)
    finally:
        await client.stop()
        server.close()
        await server.wait_closed()


async def test_heartbeat_closes_connection_when_pong_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """心跳发出后没等到 pong（官方 6 秒，测试里调短）：主动断开，交给重连循环。"""
    monkeypatch.setattr("nacho.platforms.kook.client._PONG_TIMEOUT", 0.05)
    client = KookClient(KookOptions(token="abc", heartbeat_interval=0.01))
    ws = _FakeWs()
    client._ws = ws  # noqa: SLF001
    task = asyncio.create_task(client._heartbeat())  # noqa: SLF001
    try:
        await asyncio.wait_for(ws.closed_evt.wait(), timeout=2.0)
    finally:
        task.cancel()

    assert ws.closed is True  # 不再往死连接上发心跳
    assert json.loads(ws.sent[0]) == {"s": 2, "sn": 0}  # 先发了 ping（带最新 sn）


async def test_client_resume_rejected_falls_back_to_fresh_connection() -> None:
    """续传被拒（hello code != 0）后清掉续传状态：下次重连走全新连接，不死循环。

    复现线上问题：重连一直带着失效的 ``resume=1&sn=..&session_id=..`` 参数，网关连上就断、
    断了又连。修复后第二次（续传被拒）必须清状态，第三次是全新连接并正常收到事件。
    """
    port = free_port()
    paths: list[str] = []
    connection_no = 0

    async def handler(ws) -> None:  # type: ignore[no-untyped-def]
        nonlocal connection_no
        connection_no += 1
        paths.append(cast("str", ws.request.path))
        if connection_no == 1:
            # 第一次：正常握手 + 一条事件，然后服务端掐线
            await ws.send(json.dumps({"s": 1, "d": {"code": 0, "session_id": "sess-1"}}))
            await ws.send(
                json.dumps({"s": 0, "d": {"type": EVENT_TEXT, "content": "hi"}, "sn": 1})
            )
            await ws.close()
        elif connection_no == 2:
            # 第二次：网关拒绝续传（hello code != 0），然后掐线
            await ws.send(json.dumps({"s": 1, "d": {"code": 40100, "session_id": ""}}))
            await ws.close()
        else:
            # 第三次：全新连接成功，正常收事件
            await ws.send(json.dumps({"s": 1, "d": {"code": 0, "session_id": "sess-3"}}))
            await ws.send(
                json.dumps({"s": 0, "d": {"type": EVENT_TEXT, "content": "again"}, "sn": 2})
            )
            with suppress(Exception):
                async for raw in ws:
                    pass

    server = await serve(handler, "127.0.0.1", port)
    received: list[KookEvent] = []

    async def on_event(event: KookEvent) -> None:
        received.append(event)

    client = KookClient(
        KookOptions(
            gateway=f"ws://127.0.0.1:{port}",
            token="abc",
            reconnect_interval=0.05,  # 别等默认 3 秒
        ),
        handler=on_event,
    )
    try:
        await client.start()
        # 第三次连接（全新）收到事件，说明没死循环
        assert await wait_until(lambda: len(received) >= 2, timeout=3.0)
        assert connection_no >= 3
        # 第二次是续传尝试（带 resume），第三次是全新连接（不带）
        assert "resume=1" in paths[1]
        assert "resume=1" not in paths[2]
    finally:
        await client.stop()
        server.close()
        await server.wait_closed()


async def test_client_disconnect_before_hello_clears_resume_state() -> None:
    """连上但没完成握手就被掐（没收到 code==0 的 hello）：清续传状态，下次全新连接。"""
    port = free_port()
    paths: list[str] = []
    connection_no = 0

    async def handler(ws) -> None:  # type: ignore[no-untyped-def]
        nonlocal connection_no
        connection_no += 1
        paths.append(cast("str", ws.request.path))
        if connection_no == 1:
            # 第一次：正常握手 + 一条事件，然后服务端掐线
            await ws.send(json.dumps({"s": 1, "d": {"code": 0, "session_id": "sess-1"}}))
            await ws.send(
                json.dumps({"s": 0, "d": {"type": EVENT_TEXT, "content": "hi"}, "sn": 1})
            )
            await ws.close()
        elif connection_no == 2:
            # 第二次：不发 hello 直接掐线（网关直接拒 / 连接没建立起来）
            await ws.close()
        else:
            # 第三次：全新连接成功
            await ws.send(json.dumps({"s": 1, "d": {"code": 0, "session_id": "sess-3"}}))
            await ws.send(
                json.dumps({"s": 0, "d": {"type": EVENT_TEXT, "content": "again"}, "sn": 2})
            )
            with suppress(Exception):
                async for raw in ws:
                    pass

    server = await serve(handler, "127.0.0.1", port)
    received: list[KookEvent] = []

    async def on_event(event: KookEvent) -> None:
        received.append(event)

    client = KookClient(
        KookOptions(
            gateway=f"ws://127.0.0.1:{port}",
            token="abc",
            reconnect_interval=0.05,
        ),
        handler=on_event,
    )
    try:
        await client.start()
        assert await wait_until(lambda: len(received) >= 2, timeout=3.0)
        assert connection_no >= 3
        assert "resume=1" in paths[1]
        assert "resume=1" not in paths[2]
    finally:
        await client.stop()
        server.close()
        await server.wait_closed()

