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
    """platform 区分底层适配器：onebot 缺省，kook 显式（kook 需 secret_key）。"""
    ob = await store.issue("u-admin", platform="onebot")
    kk = await store.issue(
        "u-admin", platform="kook", token="kook-bot-token-xxx", secret_key="test-key"
    )
    assert ob.record.platform == "onebot"
    assert kk.record.platform == "kook"
    assert kk.record.token_hash == hash_token("kook-bot-token-xxx")
    assert kk.record.token_secret != ""  # kook 有密文


async def test_kook_token_encrypted_and_decryptable(store: SqlBotStore) -> None:
    """Kook 的 Bot Token 落库是密文（可逆），能解回明文；OneBot 无密文列。"""
    kk = await store.issue(
        "u-admin", platform="kook", token="kook-secret-token", secret_key="my-secret-key"
    )
    assert kk.record.token_secret != "kook-secret-token"  # 不落明文
    assert kk.record.token_secret.startswith("v1.")  # 版本前缀

    decrypted = await store.decrypt_token(kk.record.bot_id, "my-secret-key")
    assert decrypted == "kook-secret-token"

    # 密钥不对 / 非 kook / 不存在 -> None
    assert await store.decrypt_token(kk.record.bot_id, "wrong-key") is None
    ob = await store.issue("u-admin", platform="onebot")
    assert await store.decrypt_token(ob.record.bot_id, "my-secret-key") is None
    assert await store.decrypt_token("不存在", "my-secret-key") is None


async def test_kook_issue_requires_secret_key(store: SqlBotStore) -> None:
    """Kook 平台不给 secret_key 当场抛（Bot Token 没法加密落库）。"""
    with pytest.raises(ValueError, match="secret_key"):
        await store.issue("u-admin", platform="kook", token="kook-bot-token-xxx")


async def test_list_platform_filters_enabled(store: SqlBotStore) -> None:
    """按平台列凭证行，只取开着开关的（Kook 装配用）。"""
    await store.issue("u-admin", platform="kook", token="t1", secret_key="k")
    kk2 = await store.issue("u-admin", platform="kook", token="t2", secret_key="k")
    await store.issue("u-admin", platform="onebot")  # 别的平台，不该出现
    await store.set_enabled(kk2.record.bot_id, False)  # 停用的不该出现

    rows = await store.list_platform("kook", enabled_only=True)
    assert [r.token_hash for r in rows] == [hash_token("t1")]


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


async def test_old_bot_table_gets_token_secret_column() -> None:
    """老库（P5 建的 bot_credentials，没有 token_secret）在 ensure_schema 时补上密文列。

    P6-1 给 kook 加的可逆密文列对老库是增量；不补的话 ``select(BotCredentialTable)`` 会取
    ``token_secret`` 这一列而报 Unknown column（``create_all`` 不会改已有表）。
    """
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        # 造一张「老表」：只有 P5 那几列，没有 token_secret
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "CREATE TABLE bot_credentials ("
                "bot_id VARCHAR(64) PRIMARY KEY, platform VARCHAR(16),"
                " owner_id VARCHAR(64), token_hash VARCHAR(64), account VARCHAR(64),"
                " enabled BOOLEAN, remark VARCHAR(255), created_at FLOAT)"
            )
            await conn.exec_driver_sql(
                "INSERT INTO bot_credentials (bot_id, platform, owner_id, token_hash,"
                " account, enabled, remark, created_at)"
                " VALUES ('old-bot', 'onebot', 'u-admin', 'deadbeef', '老机器人', 1, '老数据', 1.0)"
            )

        store = SqlBotStore(engine)
        await store.ensure_schema()  # 建表跳过（已存在）+ 补 token_secret

        rows = await store.list_records()
        assert len(rows) == 1
        assert rows[0].bot_id == "old-bot"
        assert rows[0].token_secret == ""  # 老行没有密文，补列默认空串
    finally:
        await engine.dispose()
