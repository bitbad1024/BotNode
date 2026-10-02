"""Kook 客户端（平台层）测试：网关信令的处理口径。

正向 WS 要真连网关，测不了；这里只验「收到某条信令后客户端该做什么」：reconnect 清续传
状态并主动断开、pong 超时断开、hello 超时断开等。不连网、不起事件循环。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from nacho.core.logger import default_core
from nacho.platforms.kook import KookClient, KookEvent, KookOptions
from nacho.platforms.kook import client as kook_client


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


def _client() -> KookClient:
    """建一个不连网关的客户端（日志走进程默认核心，避开未装配的便捷函数）。"""
    return KookClient(KookOptions(token="tok"), logger=default_core().child("test"))


def _event() -> KookEvent:
    """一条能进队列的事件（内容不重要，只看队列有没有被清）。"""
    return KookEvent(type=1, channel_type="GROUP", target_id="ch-1", author_id="u-1", content="hi")


async def test_reconnect_signal_resets_resume_state_and_closes() -> None:
    """signal 5（reconnect）：清 sn / session_id / 网关地址 + 清队列 + 主动断开。"""
    client = _client()
    ws = _FakeWs()
    client._ws = ws  # noqa: SLF001
    client._session_id = "sess-1"  # noqa: SLF001
    client._sn = 42  # noqa: SLF001
    client._gateway_url = "wss://gw.example/?token=x"  # noqa: SLF001
    await client._inbox.put(_event())  # noqa: SLF001

    await client._handle_raw(  # noqa: SLF001
        json.dumps({"s": 5, "d": {"code": 41008, "err": "Missing params"}})
    )

    assert ws.closed is True  # 主动断开，让 _run_loop 重新走一遍（含重新获取网关）
    assert client._session_id == ""  # noqa: SLF001
    assert client._sn == 0  # noqa: SLF001
    assert client._gateway_url == ""  # 下次重新 discover，不再拿失效地址续传  # noqa: SLF001
    assert client._inbox.empty()  # 队列一并清掉（官方：否则消息会错乱）  # noqa: SLF001


async def test_reconnect_signal_without_connection_is_safe() -> None:
    """没连着时收到 reconnect：只清状态，不炸。"""
    client = _client()
    client._session_id = "sess-1"  # noqa: SLF001
    client._sn = 7  # noqa: SLF001

    await client._handle_raw(json.dumps({"s": 5, "d": {"code": 40107}}))  # noqa: SLF001

    assert client._session_id == "" and client._sn == 0  # noqa: SLF001


async def test_pong_signal_releases_heartbeat() -> None:
    """signal 3（pong）：放行正在等它的那条心跳腿。"""
    client = _client()

    await client._handle_raw(json.dumps({"s": 3}))  # noqa: SLF001

    assert client._pong.is_set() is True  # noqa: SLF001


async def test_heartbeat_closes_connection_when_pong_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """心跳发出后没等到 pong（官方 6 秒，测试里调短）：主动断开，交给重连循环。"""
    monkeypatch.setattr(kook_client, "_PONG_TIMEOUT", 0.05)
    client = KookClient(
        KookOptions(token="tok", heartbeat_interval=0.01),
        logger=default_core().child("test"),
    )
    ws = _FakeWs()
    client._ws = ws  # noqa: SLF001
    task = asyncio.create_task(client._heartbeat())  # noqa: SLF001
    try:
        await asyncio.wait_for(ws.closed_evt.wait(), timeout=2.0)
    finally:
        task.cancel()

    assert ws.closed is True  # 不再往死连接上发心跳
    assert json.loads(ws.sent[0]) == {"s": 2, "sn": 0}  # 先发了 ping（带最新 sn）
