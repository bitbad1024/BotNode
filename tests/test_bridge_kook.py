"""Kook 适配器测试：事件翻译（KookEvent -> PlatformEvent）、clients / send 透传、投递口。

不 import ``nacho.kook`` 的底层 WS（正向连接要真连网关，测试里用 FakeClient 走翻译 +
透传），只验 :class:`nacho.platforms.bridge.kook.KookAdapter` 这层胶水。
"""
from __future__ import annotations

from typing import Any

import pytest

from nacho.platforms.bridge.kook import KookAdapter, _translate
from nacho.platforms.bridge.models import PlatformEvent
from nacho.platforms.kook import KookEvent, KookOptions


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
    translated = _translate(event, owner_id="u-admin")

    assert translated.platform == "kook"
    assert translated.owner_id == "u-admin"  # 归属 = 框架用户（凭证行 owner_id）
    assert translated.self_id == "bot-1"  # 机器人平台账号
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
        from nacho.platforms.kook import KookActionResponse

        return KookActionResponse(code=0 if self._ok else 40001, message="success" if self._ok else "参数错误")

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _make_adapter(  # type: ignore[no-untyped-def]
    client: _FakeClient, publish=None, bot_id: str = "bot-1", owner_id: str = "u-admin"
) -> KookAdapter:
    """把 FakeClient 塞进适配器（绕过构造时的真客户端与多客户端字典）。"""
    adapter = KookAdapter.__new__(KookAdapter)
    adapter._publish = publish  # noqa: SLF001
    adapter._log = __import__("nacho.core.logger", fromlist=["default_core"]).default_core().child("bridge")
    adapter._clients = {bot_id: client}  # noqa: SLF001
    adapter._owners = {bot_id: owner_id}  # noqa: SLF001
    adapter._by_self = {}  # noqa: SLF001
    adapter._started: set[str] = set()  # noqa: SLF001
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
    await adapter._on_event("bot-1", event)  # noqa: SLF001

    assert len(got) == 1
    assert got[0].platform == "kook"
    assert got[0].owner_id == "u-admin"  # 归属 = 框架用户，不再是机器人自身
    assert got[0].self_id == "bot-1"
    assert got[0].chat_id == "ch-1"
    assert got[0].text == "hi"


async def test_adapter_clients_reports_single_bot() -> None:
    """在线列表：连上就是一行（机器人自身），连接时刻也转述，不再硬编码 0。"""
    client = _FakeClient(self_id="bot-1")
    adapter = _make_adapter(client)
    rows = adapter.clients()
    assert len(rows) == 1
    assert rows[0].owner_id == "u-admin" and rows[0].self_id == "bot-1"
    assert rows[0].connected_at == 1_700_000_000.0

    # 连上了但还没学到 self_id：仍该列一行（client_id 兜底成 bot_id）
    fresh = _FakeClient(self_id="")
    adapter = _make_adapter(fresh)
    rows = adapter.clients()
    assert len(rows) == 1
    assert rows[0].self_id == "" and rows[0].client_id == "bot-1"


async def test_adapter_clients_empty_when_not_connected() -> None:
    """没连上（connected=False）：在线列表是空。"""
    client = _FakeClient(self_id="", connected=False)
    adapter = _make_adapter(client)
    assert adapter.clients() == ()


async def test_adapter_add_and_remove_bot_idempotent() -> None:
    """多客户端登记：add_bot 幂等（不重复建），remove_bot 停并注销、再删无害。"""
    adapter = KookAdapter()
    adapter.add_bot("bot-a", "tok-a", owner_id="u-admin")
    adapter.add_bot("bot-a", "tok-a")  # 幂等：已经登记过就不动
    assert adapter.has_bot("bot-a") is True
    assert adapter.has_bot("bot-b") is False

    await adapter.remove_bot("bot-a")
    assert adapter.has_bot("bot-a") is False
    await adapter.remove_bot("bot-a")  # 再删一次：没有也安静通过
