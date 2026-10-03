"""BotManager 测试：凭证落库 + 平台生命周期按平台派发（评审第 2 条收敛点）。

只测协调器这一层：不 import 平台包，用假的适配器（OneBot / Kook 各一份）验证
``issue / set_enabled / remove_by_id / online_clients`` 会派发到对应平台的生命周期，
而不是在 manager 里 if/else。真实平台的行为分别由 test_bridge_onebot / test_bridge_kook
负责。适配器没接（None）时退到「只落库」的兜底，与接入前的行为一致。
"""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tickneko.bots import SqlBotStore
from tickneko.platforms.bridge.manager import BotManager
from tickneko.platforms.bridge.models import BotClient


@pytest.fixture
async def store() -> SqlBotStore:
    """一份挂在内存 sqlite 上的存储（表已建好）。"""
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlBotStore(engine)
    await store.ensure_schema()
    yield store
    await engine.dispose()


class _FakeKookAdapter:
    """假 Kook 适配器：记下登记 / 起停 / 删除 / 在线，行为与真适配器同一形状。"""

    def __init__(self) -> None:
        self.registered: dict[str, str] = {}  # bot_id -> owner_id
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.removed: list[str] = []
        self.online: dict[str, tuple[BotClient, ...]] = {}

    @property
    def platform(self) -> str:
        return "kook"

    def has_bot(self, bot_id: str) -> bool:
        return bot_id in self.registered

    def add_bot(self, bot_id: str, token: str, *, owner_id: str = "") -> None:
        self.registered[bot_id] = owner_id

    async def start_bot(self, bot_id: str) -> None:
        self.started.append(bot_id)

    async def stop_bot(self, bot_id: str) -> None:
        self.stopped.append(bot_id)

    async def remove_bot(self, bot_id: str) -> None:
        self.removed.append(bot_id)
        self.registered.pop(bot_id, None)

    def clients(self, *, owner_id: str | None = None) -> tuple[BotClient, ...]:
        return self.online.get(owner_id or "", ())


class _FakeOneBotAdapter:
    """假 OneBot 适配器：启停 / 删走服务端（透传记录 id），在线按归属取。"""

    def __init__(self) -> None:
        self.enabled_calls: list[tuple[str, bool]] = []
        self.revoked: list[str] = []
        self.online: dict[str, tuple[BotClient, ...]] = {}

    @property
    def platform(self) -> str:
        return "onebot"

    async def set_token_enabled(self, token_id: str, enabled: bool) -> bool:
        self.enabled_calls.append((token_id, enabled))
        return True

    async def revoke_by_id(self, token_id: str) -> bool:
        self.revoked.append(token_id)
        return True

    def clients(self, *, owner_id: str | None = None) -> tuple[BotClient, ...]:
        return self.online.get(owner_id or "", ())


async def test_kook_issue_registers_and_starts_client(store: SqlBotStore) -> None:
    """platform=kook：登记客户端带归属、立刻拉起连接（生命周期在 adapter 里做）。"""
    kook = _FakeKookAdapter()
    manager = BotManager(store, kook=kook, secret_key="test-secret")

    issued = await manager.issue("u-admin", platform="kook", token="kook-token-1")

    assert issued.token == "kook-token-1"
    assert kook.registered == {issued.record.bot_id: "u-admin"}  # 归属（owner_id）带对了
    assert kook.started == [issued.record.bot_id]
    assert await manager.get_by_id(issued.record.bot_id) is not None


async def test_kook_set_enabled_and_remove(store: SqlBotStore) -> None:
    """platform=kook：停用停客户端、启用再拉起；删除注销客户端。"""
    kook = _FakeKookAdapter()
    manager = BotManager(store, kook=kook, secret_key="test-secret")
    issued = await manager.issue("u-admin", platform="kook", token="kook-token-1")
    bot_id = issued.record.bot_id

    assert await manager.set_enabled(bot_id, False) is True
    assert kook.stopped == [bot_id]
    assert await manager.set_enabled(bot_id, True) is True
    assert kook.started == [bot_id, bot_id]

    assert await manager.remove_by_id(bot_id) is True
    assert kook.removed == [bot_id]
    assert kook.registered == {}
    assert await manager.get_by_id(bot_id) is None


async def test_onebot_lifecycle_delegates_to_server(store: SqlBotStore) -> None:
    """platform=onebot：令牌随机签发（不填 token）；启停 / 删走服务端透传。"""
    onebot = _FakeOneBotAdapter()
    manager = BotManager(store, onebot=onebot)

    issued = await manager.issue("u-admin", platform="onebot", token="不应被采用")

    assert issued.token.startswith("nbo_")  # 系统随机签发，忽略用户填的
    bot_id = issued.record.bot_id

    assert await manager.set_enabled(bot_id, False) is True
    assert onebot.enabled_calls == [(bot_id, False)]
    assert await manager.remove_by_id(bot_id) is True
    assert onebot.revoked == [bot_id]


async def test_online_clients_by_platform(store: SqlBotStore) -> None:
    """在线列表口径交给平台：OneBot 按归属（owner_id）取，Kook 按 bot_id 取。"""
    kook = _FakeKookAdapter()
    onebot = _FakeOneBotAdapter()
    manager = BotManager(store, onebot=onebot, kook=kook, secret_key="test-secret")

    kook_bot = await manager.issue("u-admin", platform="kook", token="k")
    onebot_bot = await manager.issue("u-admin", platform="onebot")

    row = BotClient(client_id="c-1", owner_id="u-admin", self_id="bot")
    kook.online[kook_bot.record.bot_id] = (row,)
    onebot.online["u-admin"] = (row,)

    kook_record = await manager.get_by_id(kook_bot.record.bot_id)
    onebot_record = await manager.get_by_id(onebot_bot.record.bot_id)
    assert manager.online_clients(kook_record) == (row,)  # kook 按 bot_id
    assert manager.online_clients(onebot_record) == (row,)  # onebot 按 owner_id


async def test_no_adapter_falls_back_to_store_only(store: SqlBotStore) -> None:
    """适配器没接：只落库开关、不起客户端、在线恒空（与接入前行为一致）。"""
    manager = BotManager(store, secret_key="test-secret")

    issued = await manager.issue("u-admin", platform="kook", token="kook-token-1")

    assert issued.token == "kook-token-1"
    assert await manager.set_enabled(issued.record.bot_id, False) is True
    assert await manager.remove_by_id(issued.record.bot_id) is True
    record = await manager.get_by_id(issued.record.bot_id)
    assert record is None
    assert manager.online_clients(
        # 造一条临时记录验证在线恒空（删掉后 get_by_id 是 None，用签发记录不可用）
        _temp_record(issued.record.bot_id)
    ) == ()


def _temp_record(bot_id: str) -> Any:
    """造一条内存里的假记录：online_clients 只读 platform，不落库。"""
    from tickneko.bots import BotCredential

    return BotCredential(platform="kook", owner_id="u-admin", bot_id=bot_id)