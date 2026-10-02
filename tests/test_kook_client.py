"""Kook 客户端（平台层）测试：网关信令的处理口径。

正向 WS 要真连网关，测不了；这里只验「收到某条信令后客户端该做什么」：reconnect 清续传
状态并主动断开、pong 超时断开、hello 超时断开等。不连网、不起事件循环。
"""
from __future__ import annotations

import asyncio
import json

from nacho.core.logger import default_core
from nacho.platforms.kook import KookClient, KookEvent, KookOptions


class _FakeWs:
    """假的 WS 连接：只记有没有被关。"""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


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
