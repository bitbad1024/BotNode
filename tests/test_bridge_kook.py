"""Kook 适配器测试：事件翻译（KookEvent -> PlatformEvent）、clients / send 透传、投递口。

不 import ``nacho.kook`` 的底层 WS（正向连接要真连网关，测试里用 FakeClient 走翻译 +
透传），只验 :class:`nacho.bridge.kook.KookAdapter` 这层胶水。
"""
from __future__ import annotations

from typing import Any

import pytest

from nacho.bridge.kook import KookAdapter, _translate
from nacho.bridge.models import PlatformEvent
from nacho.kook import KookEvent, KookOptions


def test_translate_group_message() -> None:
    """频道消息：channel_type=GROUP -> chat=group，target_id -> chat_id，author_id -> user_id。"""
    event = KookEvent(
        type=1,
        channel_type="GROUP",
        target_id="ch-123",
        author_id="u-456",
        msg_id="m-789",
        content="你好",
        msg_timestamp=1_700_000_000_000,
        self_id="bot-1",
    )
    translated = _translate(event)

    assert translated.platform == "kook"
    assert translated.owner_id == "bot-1"  # 归属 = 机器人自身
    assert translated.self_id == "bot-1"
    assert translated.kind == "message"
    assert translated.chat == "group"
    assert translated.chat_id == "ch-123"
    assert translated.user_id == "u-456"
    assert translated.text == "你好"
    assert translated.message_id == "m-789"
    assert translated.time == 1_700_000_000.0  # 毫秒转秒
    assert translated.raw is event  # 翻译不了的字段从这里兜


def test_translate_private_message() -> None:
    """私聊：channel_type=PERSON -> chat=private。"""
    event = KookEvent(
        type=1,
        channel_type="PERSON",
        target_id="u-456",
        author_id="u-456",
        msg_id="m-1",
        content="悄悄话",
        self_id="bot-1",
    )
    translated = _translate(event)
    assert translated.chat == "private"
    assert translated.chat_id == "u-456"
    assert translated.user_id == "u-456"


def test_translate_unknown_channel_type_is_other() -> None:
    """认不出的 channel_type -> chat=other（不猜）。"""
    event = KookEvent(type=9, channel_type="BROADCAST", content="广播")
    translated = _translate(event)
    assert translated.chat == "other"
    assert translated.chat_id == ""


# --------------------------------------------------------------------------- 适配器胶水
class _FakeClient:
    """假的 Kook 客户端：记下动作调用，按给定回应回执。"""

    def __init__(self, *, self_id: str = "bot-1", ok: bool = True, connected: bool = True) -> None:
        self.self_id = self_id
        self._ok = ok
        self._connected = connected
        self.connected_at = 1_700_000_000.0 if connected else 0.0
        self.calls: list[tuple[str, dict[str, object]]] = []

    @property
    def connected(self) -> bool:
        return self._connected

    async def call(self, action: str, /, **params: object) -> Any:
        self.calls.append((action, dict(params)))
        from nacho.kook import KookActionResponse

        return KookActionResponse(code=0 if self._ok else 40001, message="success" if self._ok else "参数错误")

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _make_adapter(client: _FakeClient, publish=None) -> KookAdapter:  # type: ignore[no-untyped-def]
    """把 FakeClient 塞进适配器（绕过构造时的真客户端）。"""
    adapter = KookAdapter.__new__(KookAdapter)
    adapter._publish = publish  # noqa: SLF001
    adapter._log = __import__("nacho.core.logger", fromlist=["get_logger"]).get_logger("bridge")
    adapter._client = client
    return adapter


async def test_adapter_send_translates_receipt() -> None:
    """send 走 client.call，回执翻译成 ActionResult（成功 ok=True）。"""
    client = _FakeClient()
    adapter = _make_adapter(client)

    result = await adapter.send("bot-1", "send_channel_msg", target_id="ch-1", content="hi")

    assert client.calls == [("send_channel_msg", {"target_id": "ch-1", "content": "hi"})]
    assert result.ok is True
    assert result.raw is not None  # 原始回执留 raw


async def test_adapter_send_failure_receipt() -> None:
    """回执不成功（code 非 0）：ok=False，message 带错误说明。"""
    client = _FakeClient(ok=False)
    adapter = _make_adapter(client)
    result = await adapter.send("bot-1", "send_channel_msg", target_id="ch-1", content="hi")
    assert result.ok is False
    assert result.message == "参数错误"


async def test_adapter_publishes_translated_event() -> None:
    """事件钩子：收到 KookEvent 翻译成 PlatformEvent 投给 publish。"""
    client = _FakeClient()
    got: list[PlatformEvent] = []

    async def publish(event: PlatformEvent) -> None:
        got.append(event)

    adapter = _make_adapter(client, publish=publish)

    event = KookEvent(type=1, channel_type="GROUP", target_id="ch-1", author_id="u-1", content="hi", self_id="bot-1")
    await adapter._on_event(event)  # noqa: SLF001

    assert len(got) == 1
    assert got[0].platform == "kook"
    assert got[0].chat_id == "ch-1"
    assert got[0].text == "hi"


async def test_adapter_clients_reports_single_bot() -> None:
    """在线列表：连上就是一行（机器人自身），连接时刻也转述，不再硬编码 0。"""
    client = _FakeClient(self_id="bot-1")
    adapter = _make_adapter(client)
    rows = adapter.clients()
    assert len(rows) == 1
    assert rows[0].owner_id == "bot-1" and rows[0].self_id == "bot-1"
    assert rows[0].connected_at == 1_700_000_000.0

    # 连上了但还没学到 self_id：仍该列一行（client_id 兜底成 "kook"）
    fresh = _FakeClient(self_id="")
    adapter = _make_adapter(fresh)
    rows = adapter.clients()
    assert len(rows) == 1
    assert rows[0].self_id == "" and rows[0].client_id == "kook"


async def test_adapter_clients_empty_when_not_connected() -> None:
    """没连上（connected=False）：在线列表是空。"""
    client = _FakeClient(self_id="", connected=False)
    adapter = _make_adapter(client)
    assert adapter.clients() == ()
