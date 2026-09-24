"""``SqlUserStore``（SQLModel 落库）的测试：建表、种数据、查询、唯一性，以及接入 ``create_app`` 后的登录链路。

跑在临时 sqlite 文件 + ``aiosqlite`` 上（文件由 ``tmp_path`` 管，用完即删）；需要 ``sqlmodel`` /
``aiosqlite``（``pip install "nacho[api]"``），没装就整文件跳过。
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("sqlmodel", reason="落库存储要装 sqlmodel：pip install \"nacho[api]\"")
pytest.importorskip("aiosqlite", reason="sqlite 异步驱动要装 aiosqlite：pip install \"nacho[api]\"")
pytest.importorskip("fastapi", reason="接口层要装 fastapi：pip install \"nacho[api]\"")
pytest.importorskip("httpx", reason="接口层测试用 httpx 发请求：pip install \"nacho[dev]\"")

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402
from sqlmodel import select  # noqa: E402
from sqlmodel.ext.asyncio.session import AsyncSession  # noqa: E402

from nacho.api import (  # noqa: E402
    AccountAlreadyExistsError,
    ApiOptions,
    ApiResponse,
    LoginData,
    Pbkdf2PasswordHasher,
    create_app,
)
from nacho.api.services.user.store_sql import SqlUserStore, UserTable  # noqa: E402

#: 演示账号（见 nacho.api.services.user.demo.DEMO_USERS）
ADMIN = {"account": "admin", "password": "nacho-admin"}
#: 测试用的哈希迭代次数。默认 20 万次是生产该有的值，但登录断言与迭代次数无关
#: （哈希串自带参数），降到 1000 次省掉每个用例重复付的那笔钱。
TEST_ITERATIONS: int = 1_000
_TEST_HASHER = Pbkdf2PasswordHasher(iterations=TEST_ITERATIONS)


def open_engine(tmp_path: Path) -> AsyncEngine:
    """在临时目录上开一个 sqlite 异步引擎（每个用例一个独立库文件）。"""
    return create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'users.db').as_posix()}")


@asynccontextmanager
async def opened_store(tmp_path: Path) -> AsyncGenerator[tuple[SqlUserStore, AsyncEngine]]:
    """临时库上的 :class:`SqlUserStore`；退出时 dispose 引擎。"""
    engine = open_engine(tmp_path)
    try:
        yield SqlUserStore(engine, hasher=_TEST_HASHER), engine
    finally:
        await engine.dispose()


async def rows_of(engine: AsyncEngine) -> list[UserTable]:
    """直接把 users 表整个读出来，用来断言"表里实际有几行"。"""
    async with AsyncSession(engine) as session:
        return list((await session.exec(select(UserTable))).all())


# --------------------------------------------------------------------------- 建表 / 种数据
async def test_ensure_schema_is_idempotent(tmp_path: Path) -> None:
    async with opened_store(tmp_path) as (store, engine):
        await store.ensure_schema()
        await store.ensure_schema()  # 再建一次不报错（CREATE TABLE IF NOT EXISTS 的效果）
        await store.seed_demo()
        assert len(await rows_of(engine)) == 3


async def test_seed_demo_is_idempotent(tmp_path: Path) -> None:
    async with opened_store(tmp_path) as (store, engine):
        await store.ensure_schema()
        await store.seed_demo()
        await store.seed_demo()  # 已有数据，第二次不该重复插
        assert len(await rows_of(engine)) == 3


async def test_seed_demo_skips_non_empty_table(tmp_path: Path) -> None:
    """表里已有人（比如真实账号）时不种演示账号，避免污染真实数据。"""
    async with opened_store(tmp_path) as (store, engine):
        await store.ensure_schema()
        async with AsyncSession(engine) as session:
            session.add(
                UserTable(
                    id="u-real",
                    account="real",
                    password_hash="h",
                    nickname="真实用户",
                    roles="[]",
                    disabled=False,
                )
            )
            await session.commit()
        await store.seed_demo()
        assert len(await rows_of(engine)) == 1


# --------------------------------------------------------------------------- 查询
async def test_get_by_account(tmp_path: Path) -> None:
    async with opened_store(tmp_path) as (store, _):
        await store.ensure_schema()
        await store.seed_demo()

        admin = await store.get_by_account("admin")
        assert admin is not None
        assert admin.account == "admin"
        assert admin.nickname == "管理员"
        assert admin.roles == ("admin", "user")  # JSON 列已还原成 tuple
        assert admin.disabled is False
        assert admin.password_hash.startswith("pbkdf2_sha256$")


async def test_get_by_id(tmp_path: Path) -> None:
    async with opened_store(tmp_path) as (store, _):
        await store.ensure_schema()
        await store.seed_demo()

        admin = await store.get_by_account("admin")
        assert admin is not None
        same = await store.get_by_id(admin.id)
        assert same is not None and same.account == "admin"


async def test_get_by_ids(tmp_path: Path) -> None:
    """一批 id **一次**查齐（列表页的昵称查询靠它，别一条一次）：重复的、不存在的都不出现。"""
    async with opened_store(tmp_path) as (store, _):
        await store.ensure_schema()
        await store.seed_demo()

        admin = await store.get_by_account("admin")
        robot = await store.get_by_account("robot")
        assert admin is not None and robot is not None

        got = await store.get_by_ids([admin.id, robot.id, "u-nobody", admin.id])

        assert set(got) == {admin.id, robot.id}
        assert got[admin.id].nickname == "管理员"
        assert got[robot.id].nickname == "巡检机器人"
        assert await store.get_by_ids([]) == {}  # 空的一批：不查库


async def test_disabled_account_and_missing(tmp_path: Path) -> None:
    async with opened_store(tmp_path) as (store, _):
        await store.ensure_schema()
        await store.seed_demo()

        guest = await store.get_by_account("guest")
        assert guest is not None and guest.disabled is True
        assert await store.get_by_account("nobody") is None
        assert await store.get_by_id("u-nobody") is None


async def test_broken_roles_json_is_tolerated(tmp_path: Path) -> None:
    """库里的 roles 是个坏 JSON 串时不抛异常，当成"没有角色"。"""
    async with opened_store(tmp_path) as (store, engine):
        await store.ensure_schema()
        async with AsyncSession(engine) as session:
            session.add(
                UserTable(
                    id="u-bad",
                    account="bad",
                    password_hash="h",
                    nickname="",
                    roles="{not json",
                    disabled=False,
                )
            )
            await session.commit()

        row = await store.get_by_account("bad")
        assert row is not None and row.roles == ()


# --------------------------------------------------------------------------- 约束
async def test_account_is_unique(tmp_path: Path) -> None:
    """account 上的唯一索引要真的拦住重复账号。"""
    async with opened_store(tmp_path) as (store, engine):
        await store.ensure_schema()
        await store.seed_demo()

        async with AsyncSession(engine) as session:
            session.add(
                UserTable(
                    id="u-dup",
                    account="admin",  # 与演示账号重名
                    password_hash="h",
                    nickname="",
                    roles="[]",
                    disabled=False,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()


async def test_add_inserts_row_and_rejects_duplicate(tmp_path: Path) -> None:
    """add 落一条新账号；同账号再 add 抛「已被注册」（唯一约束翻成 409 那个异常，不是 IntegrityError）。"""
    async with opened_store(tmp_path) as (store, engine):
        await store.ensure_schema()
        record = await store.add(account="newbie", password_hash="hash", nickname="新来的")
        assert (record.id, record.account, record.nickname) == ("u-newbie", "newbie", "新来的")
        assert record.roles == () and record.disabled is False
        assert await store.get_by_account("newbie") == record  # 确实落库了，不只是内存里的对象
        rows = await rows_of(engine)  # 表里真的多了一行（不是只在会话里挂着）
        assert [(row.id, row.account, row.nickname) for row in rows] == [
            ("u-newbie", "newbie", "新来的")
        ]

        with pytest.raises(AccountAlreadyExistsError):
            await store.add(account="newbie", password_hash="another-hash")


# --------------------------------------------------------------------------- 接入 create_app
async def test_create_app_with_db_serves_login(tmp_path: Path) -> None:
    """传了 db 就走落库存储：lifespan 建表 + 种账号，随后登录 / 取当前用户 / 停用账号被拒都正常。"""
    engine = open_engine(tmp_path)
    app: FastAPI = create_app(
        ApiOptions(prefix="/api", token_ttl=1800.0), db=engine, hasher=_TEST_HASHER
    )
    try:
        # starlette 的 lifespan 要在 ASGITransport 之外手动跑（httpx 不会自己触发 startup）
        async with app.router.lifespan_context(app):
            assert len(await rows_of(engine)) == 3  # lifespan 已在库里种好演示账号

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                ok = await client.post("/api/auth/login", json=ADMIN)
                assert ok.status_code == 200, ok.text
                # 用项目自己的响应壳解析，得到类型化的 data（顺带断言了响应协议形状）
                login = ApiResponse[LoginData].model_validate(ok.json())
                token = login.data.token

                me = await client.get(
                    "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
                )
                assert me.status_code == 200

                denied = await client.post(
                    "/api/auth/login", json={"account": "guest", "password": "nacho-guest"}
                )
                assert denied.status_code == 403  # 停用账号
    finally:
        await engine.dispose()
