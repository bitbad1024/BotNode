"""机器人凭证（bot_credentials）的测试：多实例签发、握手 resolve、停用 / 删除。

与旧 onebot_tokens 的关键差异：**一个用户多个机器人**（bot_id 主键，不是 owner_id 主键），
platform 区分底层适配器。这里只测存储层（模型 + SqlBotStore），接口 / 前端留 P5-2 / P5-3。
"""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nacho.bots import BotCredential, SqlBotStore, hash_token


@pytest.fixture
async def store() -> SqlBotStore:
    """一份挂在内存 sqlite 上的存储（表已建好）。"""
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = SqlBotStore(engine)
    await store.ensure_schema()
    yield store
    await engine.dispose()


async def test_issue_creates_multiple_bots_per_owner(store: SqlBotStore) -> None:
    """一个用户多个机器人：每次签发新 bot_id，不再「一个归属一条换钥匙」。"""
    first = await store.issue("u-admin", account="机器人一号")
    second = await store.issue("u-admin", account="机器人二号")

    assert first.record.bot_id != second.record.bot_id  # 各自独立的行
    assert first.record.owner_id == "u-admin"
    assert first.record.platform == "onebot"  # 缺省 onebot
    assert first.record.token_hash == hash_token(first.token)  # 只存摘要
    assert first.token != second.token  # 令牌也不同

    rows = await store.list_records(owner_id="u-admin")
    assert len(rows) == 2  # 同一个用户现在有两条


async def test_issue_platform_tagged(store: SqlBotStore) -> None:
    """platform 区分底层适配器：onebot 缺省，kook 显式。"""
    ob = await store.issue("u-admin", platform="onebot")
    kk = await store.issue("u-admin", platform="kook", token="kook-bot-token-xxx")
    assert ob.record.platform == "onebot"
    assert kk.record.platform == "kook"
    assert kk.record.token_hash == hash_token("kook-bot-token-xxx")


async def test_resolve_by_token(store: SqlBotStore) -> None:
    """握手入口：按令牌明文找到记录；令牌错 / 停用后认不出来。"""
    issued = await store.issue("u-admin", account="主号")
    found = await store.resolve(issued.token)
    assert found is not None
    assert found.bot_id == issued.record.bot_id
    assert found.owner_id == "u-admin"
    assert await store.resolve("nbo_不存在") is None

    # 停用后 resolve 认不出来（握手 401）
    await store.set_enabled(issued.record.bot_id, False)
    assert await store.resolve(issued.token) is None


async def test_remove_and_set_enabled(store: SqlBotStore) -> None:
    """删 / 停用 / 启用；id 不存在返回 False。"""
    issued = await store.issue("u-admin")
    assert await store.set_enabled(issued.record.bot_id, False) is True
    assert (await store.get_by_id(issued.record.bot_id)).enabled is False  # type: ignore[union-attr]
    assert await store.set_enabled("不存在", True) is False

    assert await store.remove_by_id(issued.record.bot_id) is True
    assert await store.get_by_id(issued.record.bot_id) is None
    assert await store.remove_by_id(issued.record.bot_id) is False


async def test_issue_rejects_empty_token(store: SqlBotStore) -> None:
    """显式传空 token（Kook 场景用户没填）是错误，当场抛。"""
    with pytest.raises(ValueError, match="空令牌"):
        await store.issue("u-admin", platform="kook", token="")
