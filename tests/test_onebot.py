"""OneBot 反向 WS 的测试：令牌定归属、在线列表、踢人与吊销，以及管理用的 HTTP 接口。

跑在 127.0.0.1 的空闲端口上（每个用例自己挑一个），WS 客户端用 ``websockets``；
令牌注册表用内存版（快）+ sqlite 版（验证落库那一份也走得通）。需要 ``websockets``
（``pip install "nacho[onebot]"``），没装就整文件跳过。
"""
from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, cast

import pytest

pytest.importorskip("websockets", reason="OneBot 接入层要装 websockets：pip install \"nacho[onebot]\"")
pytest.importorskip("fastapi", reason="接口层要装 fastapi：pip install \"nacho[api]\"")
pytest.importorskip("httpx", reason="接口层测试用 httpx 发请求：pip install \"nacho[dev]\"")

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402
from websockets.exceptions import ConnectionClosed, InvalidStatus  # noqa: E402

from nacho.api import (  # noqa: E402
    ApiOptions,
    ApiResponse,
    ClientData,
    InMemoryUserStore,
    IssuedTokenData,
    KickData,
    LoginData,
    Pbkdf2PasswordHasher,
    RevokeData,
    TokenData,
    create_app,
)
from nacho.onebot import (  # noqa: E402
    InMemoryTokenRegistry,
    OneBotOptions,
    OneBotServer,
    SqlTokenRegistry,
)
from nacho.onebot import tokens as tokens_module  # noqa: E402

#: 演示账号（见 nacho.api.services.user.demo.DEMO_USERS）
ADMIN = {"account": "admin", "password": "nacho-admin"}
#: 普通用户（roles 里只有 user）：用来验「只能管自己账号下那部分」
ROBOT = {"account": "robot", "password": "nacho-robot"}
#: 测试用的哈希迭代次数：默认 20 万次是生产该有的值，登录断言与迭代次数无关
TEST_ITERATIONS: int = 1_000
_TEST_HASHER = Pbkdf2PasswordHasher(iterations=TEST_ITERATIONS)

#: 一条私聊消息事件（够用来让连接学到 self_id）
PRIVATE_MESSAGE: dict[str, object] = {
    "post_type": "message",
    "message_type": "private",
    "time": 1_700_000_000,
    "self_id": 10001,
    "user_id": 20002,
    "raw_message": "你好",
    "message": "你好",
    "sender": {"nickname": "对方"},
}


def free_port() -> int:
    """挑一个当前空闲的端口（让内核分配，测完即释放）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        # getsockname 在 typeshed 里返回 Any，先 cast 成地址形状再取端口
        return cast("tuple[str, int]", sock.getsockname())[1]


def ws_url(port: int, token: str = "") -> str:
    """反向 WS 地址；给了令牌就按查询串带上（OneBot 实现常这么配）。"""
    return f"ws://127.0.0.1:{port}/" + (f"?access_token={token}" if token else "")


async def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    """轮询等一个条件成立（服务端那条腿是异步跑的，得给它一点时间）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


@asynccontextmanager
async def opened_server(
    registry: InMemoryTokenRegistry | None = None,
) -> AsyncGenerator[OneBotServer]:
    """起一个临时服务端（随机端口）；退出时停服并断开所有客户端。"""
    server = OneBotServer(
        OneBotOptions(host="127.0.0.1", port=free_port()), tokens=registry
    )
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def port_of(server: OneBotServer) -> int:
    """服务端实际监听的端口。"""
    return server.options.port


def api_app(server: OneBotServer | None) -> FastAPI:
    """接口层应用（内存演示账号），把 OneBot 服务挂上去给管理接口用。"""
    return create_app(
        ApiOptions(prefix="/api"),
        user_store=InMemoryUserStore.demo(_TEST_HASHER),
        hasher=_TEST_HASHER,
        onebot=server,
    )


async def login(client: httpx.AsyncClient, who: dict[str, str] | None = None) -> str:
    """登录拿令牌（管理接口都要带它）；``who`` 不传就是 admin。"""
    ok = await client.post("/api/auth/login", json=who or ADMIN)
    assert ok.status_code == 200, ok.text
    return ApiResponse[LoginData].model_validate(ok.json()).data.token


async def issue_via_api(
    client: httpx.AsyncClient, headers: dict[str, str], account: str
) -> IssuedTokenData:
    """走管理接口签一个令牌（顺带断言能签出来），返回签发结果。"""
    made = await client.post(
        "/api/onebot/tokens", headers=headers, json={"account": account, "remark": account}
    )
    assert made.status_code == 200, made.text
    return ApiResponse[IssuedTokenData].model_validate(made.json()).data


# --------------------------------------------------------------------------- 令牌注册表
async def test_registry_issue_resolve_and_revoke() -> None:
    """签发 -> 认领 -> 列出 -> 吊销：令牌不存在 / 吊销后都认不出来。"""
    registry = InMemoryTokenRegistry()
    issued = await registry.issue("alice", remark="主号")

    found = await registry.resolve(issued.token)
    assert found is not None and found.account == "alice"
    assert found.id == issued.record.id and found.remark == "主号"
    assert await registry.resolve("nbo_不存在") is None
    assert len(await registry.list_records()) == 1

    assert await registry.remove_by_id(issued.record.id) is True
    assert await registry.resolve(issued.token) is None  # 吊销后认不出来
    assert await registry.remove_by_id(issued.record.id) is False  # 再删一次：没有了


async def test_sql_registry_roundtrip(tmp_path: Path) -> None:
    """落库那一份（``onebot_tokens`` 表）：建表 -> 签发 -> 认领 -> 吊销。"""
    engine: AsyncEngine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'onebot.db').as_posix()}"
    )
    try:
        registry = SqlTokenRegistry(engine)
        await registry.ensure_schema()
        await registry.ensure_schema()  # 幂等：再建一次不报错

        issued = await registry.issue("bob")
        found = await registry.resolve(issued.token)
        assert found is not None and found.account == "bob"
        assert await registry.resolve("nbo_不存在") is None
        assert len(await registry.list_records()) == 1
        assert await registry.remove_by_id(issued.record.id) is True
    finally:
        await engine.dispose()


def sql_engine(tmp_path: Path) -> AsyncEngine:
    """临时库上的异步引擎（签发重试那几个用例自己管 dispose）。"""
    return create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'collide.db').as_posix()}")


async def test_issue_retries_when_digest_collides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """摘要撞上已有记录：换一个令牌再来（把生成器钉死，碰撞就能确定性地造出来）。"""
    engine = sql_engine(tmp_path)
    try:
        registry = SqlTokenRegistry(engine)
        await registry.ensure_schema()
        first = await registry.issue("alice")

        # 第一次还生成 first.token（必撞），第二次给一个新的：应当重试后成功
        generated = iter([first.token, "nbo_fresh"])
        monkeypatch.setattr(tokens_module, "generate_token", lambda: next(generated))

        second = await registry.issue("alice")
        assert second.token == "nbo_fresh"
        assert await registry.resolve(first.token) is not None  # 原来那个还在
        assert await registry.resolve("nbo_fresh") is not None
    finally:
        await engine.dispose()


async def test_issue_gives_up_after_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一直撞：试满上限就把错抛出来——绝不退回一个跟别人一样的令牌。"""
    engine = sql_engine(tmp_path)
    try:
        registry = SqlTokenRegistry(engine)
        await registry.ensure_schema()
        first = await registry.issue("alice")
        monkeypatch.setattr(tokens_module, "generate_token", lambda: first.token)

        with pytest.raises(IntegrityError):
            await registry.issue("alice")
        assert len(await registry.list_records()) == 1  # 没留下半截记录
    finally:
        await engine.dispose()


async def test_memory_issue_skips_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    """内存版没有唯一索引兜底，自己看一眼摘要：撞了就换一个。"""
    registry = InMemoryTokenRegistry()
    first = await registry.issue("alice")

    generated = iter([first.token, "nbo_fresh"])
    monkeypatch.setattr(tokens_module, "generate_token", lambda: next(generated))

    second = await registry.issue("alice")
    assert second.token == "nbo_fresh"
    assert await registry.resolve(first.token) is not None


# --------------------------------------------------------------------------- 握手：令牌定归属
async def test_handshake_binds_account() -> None:
    """带对令牌连进来：归属账号绑在连接上，机器人号收到第一条事件后才学到。"""
    registry = InMemoryTokenRegistry()
    alice = await registry.issue("alice")
    bob = await registry.issue("bob")

    async with opened_server(registry) as server:
        port = port_of(server)
        async with connect(ws_url(port, alice.token)) as ws, connect(ws_url(port, bob.token)):
            await ws.send(json.dumps(PRIVATE_MESSAGE))
            assert await wait_until(lambda: len(server.roster()) == 2)

            assert {item.account for item in server.roster()} == {"alice", "bob"}
            # 列表是快照，要重新取才能看到刚学到的机器人号
            assert await wait_until(
                lambda: any(
                    item.account == "alice" and item.self_id == 10001 for item in server.roster()
                )
            )
            bob_entry = next(item for item in server.roster() if item.account == "bob")
            assert bob_entry.self_id is None  # 没发过事件，还不知道是哪个机器人

            # 只看某个账号下的
            assert [item.account for item in server.roster(account="alice")] == ["alice"]


async def test_handshake_rejects_bad_token() -> None:
    """令牌不对 / 没带令牌：握手就 401，连不进来。"""
    registry = InMemoryTokenRegistry()
    issued = await registry.issue("alice")

    async with opened_server(registry) as server:
        port = port_of(server)
        with pytest.raises(InvalidStatus):  # 令牌不对
            async with connect(ws_url(port, "nbo_随便写的")):
                pass
        with pytest.raises(InvalidStatus):  # 压根没带
            async with connect(ws_url(port)):
                pass
        assert server.roster() == ()

        # 对的那一个照样能连
        async with connect(ws_url(port, issued.token)):
            assert await wait_until(lambda: len(server.roster()) == 1)


async def test_without_registry_accepts_anonymous() -> None:
    """不配注册表 = 不校验（谁都能连），归属记成匿名——不接数据库也能跑起来。"""
    async with opened_server(None) as server:
        async with connect(ws_url(port_of(server))):
            assert await wait_until(lambda: len(server.roster()) == 1)
            assert server.roster()[0].account == ""


# --------------------------------------------------------------------------- 在线列表 / 踢人 / 吊销
async def test_kick_disconnects_client() -> None:
    """踢下线：客户端那条连接被断开，在线列表里也消失。"""
    registry = InMemoryTokenRegistry()
    issued = await registry.issue("alice")

    async with opened_server(registry) as server:
        async with connect(ws_url(port_of(server), issued.token)) as ws:
            await ws.send(json.dumps(PRIVATE_MESSAGE))
            assert await wait_until(lambda: len(server.roster()) == 1)

            assert await server.kick("不存在的连接") is False  # 没有这条连接
            assert await server.kick(server.roster()[0].id) is True

            with pytest.raises(ConnectionClosed):
                await ws.recv()
            assert await wait_until(lambda: server.roster() == ())


async def test_kick_with_revoke_blocks_reconnect() -> None:
    """踢 + 吊销：断开之后重连也被拒（只踢不断令牌的话，客户端会自己重连回来）。"""
    registry = InMemoryTokenRegistry()
    issued = await registry.issue("alice")

    async with opened_server(registry) as server:
        port = port_of(server)
        async with connect(ws_url(port, issued.token)):
            assert await wait_until(lambda: len(server.roster()) == 1)
            assert await server.kick(server.roster()[0].id, revoke=True) is True
            assert await wait_until(lambda: server.roster() == ())

        with pytest.raises(InvalidStatus):  # 令牌已吊销：重连 401
            async with connect(ws_url(port, issued.token)):
                pass


async def test_kick_without_revoke_keeps_token() -> None:
    """只踢不吊销：令牌还在，拿同一个令牌还能连上（这就是要 revoke 的原因）。"""
    registry = InMemoryTokenRegistry()
    issued = await registry.issue("alice")

    async with opened_server(registry) as server:
        port = port_of(server)
        async with connect(ws_url(port, issued.token)):
            assert await wait_until(lambda: len(server.roster()) == 1)
            assert await server.kick(server.roster()[0].id) is True
            assert await wait_until(lambda: server.roster() == ())

        async with connect(ws_url(port, issued.token)):  # 重连成功
            assert await wait_until(lambda: len(server.roster()) == 1)


async def test_revoke_by_id_disconnects_client() -> None:
    """按令牌 id 吊销：记录删掉，正用它连着的客户端也断开（从列表里消失）。"""
    registry = InMemoryTokenRegistry()
    issued = await registry.issue("alice")

    async with opened_server(registry) as server:
        async with connect(ws_url(port_of(server), issued.token)) as ws:
            await ws.send(json.dumps(PRIVATE_MESSAGE))
            assert await wait_until(lambda: len(server.roster()) == 1)

            assert await server.revoke_by_id(issued.record.id) is True
            with pytest.raises(ConnectionClosed):
                await ws.recv()
            assert await wait_until(lambda: server.roster() == ())
            assert await registry.resolve(issued.token) is None

        assert await server.revoke_by_id(issued.record.id) is False  # 已经没有了


async def test_set_enabled_disables_and_disconnects() -> None:
    """停用：不许再连（握手 401），正连着的客户端一并断开；启用回来又能连。"""
    registry = InMemoryTokenRegistry()
    issued = await registry.issue("alice")

    async with opened_server(registry) as server:
        port = port_of(server)
        async with connect(ws_url(port, issued.token)) as ws:
            await ws.send(json.dumps(PRIVATE_MESSAGE))
            assert await wait_until(lambda: len(server.roster()) == 1)

            assert await server.set_token_enabled(issued.record.id, False) is True
            with pytest.raises(ConnectionClosed):
                await ws.recv()
            assert await wait_until(lambda: server.roster() == ())
            assert await registry.resolve(issued.token) is None  # 停用后认不出来

        with pytest.raises(InvalidStatus):  # 重连被拒
            async with connect(ws_url(port, issued.token)):
                pass

        # 和吊销不同：记录还在，启用回来照样能用
        assert len(await registry.list_records()) == 1
        assert await server.set_token_enabled(issued.record.id, True) is True
        async with connect(ws_url(port, issued.token)):
            assert await wait_until(lambda: len(server.roster()) == 1)
            assert server.roster()[0].account == "alice"


async def test_set_enabled_unknown_id() -> None:
    """id 不存在：改不动，返回 False。"""
    async with opened_server(InMemoryTokenRegistry()) as server:
        assert await server.set_token_enabled("t-不存在", True) is False


# --------------------------------------------------------------------------- 管理用的 HTTP 接口
async def test_management_requires_login() -> None:
    """管理接口都要登录：没带令牌一律 401（先鉴权，再看服务接没接）。"""
    async with opened_server(InMemoryTokenRegistry()) as server:
        app = api_app(server)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/api/onebot/clients")).status_code == 401
            assert (await client.get("/api/onebot/tokens")).status_code == 401
            assert (await client.delete("/api/onebot/clients/whatever")).status_code == 401


async def test_management_reports_not_configured() -> None:
    """主程序没传 OneBot 服务：接口回 503 说「没接入」，而不是 500。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app(None)), base_url="http://test"
    ) as client:
        token = await login(client)
        response = await client.get(
            "/api/onebot/clients", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 503


async def test_management_lists_clients_and_manages_tokens() -> None:
    """登录后：看在线列表 -> 签一个令牌 -> 列出来 -> 吊销掉。"""
    registry = InMemoryTokenRegistry()
    async with opened_server(registry) as server:
        app = api_app(server)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            token = await login(client)
            headers = {"Authorization": f"Bearer {token}"}

            # 1. 还没有客户端
            listed = await client.get("/api/onebot/clients", headers=headers)
            assert listed.status_code == 200, listed.text
            assert ApiResponse[list[ClientData]].model_validate(listed.json()).data == []

            # 2. 签一个令牌
            issued = await client.post(
                "/api/onebot/tokens",
                headers=headers,
                json={"account": "alice", "remark": "主号"},
            )
            assert issued.status_code == 200, issued.text
            # 用项目自己的响应壳解析：类型化的 data，顺带断言了响应协议形状
            issued_data = ApiResponse[IssuedTokenData].model_validate(issued.json()).data
            assert issued_data.record.account == "alice"
            assert issued_data.token.startswith("nbo_")  # 明文只在这一次露出来

            # 3. 令牌列表里有它（且**不含明文**：响应模型里根本没有 token 字段）
            tokens = await client.get("/api/onebot/tokens", headers=headers)
            assert tokens.status_code == 200, tokens.text
            records = ApiResponse[list[TokenData]].model_validate(tokens.json()).data
            assert [item.account for item in records] == ["alice"]
            assert "token" not in TokenData.model_fields

            # 4. 拿这个令牌连进来，在线列表里能看到它属于 alice
            async with connect(ws_url(port_of(server), issued_data.token)) as ws:
                await ws.send(json.dumps(PRIVATE_MESSAGE))
                assert await wait_until(lambda: len(server.roster()) == 1)
                assert await wait_until(
                    lambda: server.roster()[0].self_id == 10001  # 等它学到机器人号
                )

                clients = await client.get(
                    "/api/onebot/clients", headers=headers, params={"account": "alice"}
                )
                assert clients.status_code == 200, clients.text
                rows = ApiResponse[list[ClientData]].model_validate(clients.json()).data
                assert len(rows) == 1 and rows[0].account == "alice"
                assert rows[0].self_id == 10001

                # 5. 踢下线（不吊销）
                kicked = await client.delete(
                    f"/api/onebot/clients/{rows[0].id}", headers=headers
                )
                assert kicked.status_code == 200, kicked.text
                assert ApiResponse[KickData].model_validate(kicked.json()).data.revoked is False

            # 6. 吊销令牌
            revoked = await client.delete(
                f"/api/onebot/tokens/{issued_data.record.id}", headers=headers
            )
            assert revoked.status_code == 200, revoked.text
            assert ApiResponse[RevokeData].model_validate(revoked.json()).data.removed is True
            assert await registry.resolve(issued_data.token) is None

            # 7. 再删一次：404
            again = await client.delete(
                f"/api/onebot/tokens/{issued_data.record.id}", headers=headers
            )
            assert again.status_code == 404


async def test_management_toggles_token_enabled() -> None:
    """PATCH /onebot/tokens/{id}：停用 -> 状态变了 -> 启用回来；id 不存在 404。"""
    registry = InMemoryTokenRegistry()
    async with opened_server(registry) as server:
        app = api_app(server)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Authorization": f"Bearer {await login(client)}"}
            issued = await registry.issue("alice")

            off = await client.patch(
                f"/api/onebot/tokens/{issued.record.id}",
                headers=headers,
                json={"enabled": False},
            )
            assert off.status_code == 200, off.text
            assert ApiResponse[TokenData].model_validate(off.json()).data.enabled is False

            on = await client.patch(
                f"/api/onebot/tokens/{issued.record.id}",
                headers=headers,
                json={"enabled": True},
            )
            assert on.status_code == 200, on.text
            assert ApiResponse[TokenData].model_validate(on.json()).data.enabled is True

            missing = await client.patch(
                "/api/onebot/tokens/t-不存在", headers=headers, json={"enabled": True}
            )
            assert missing.status_code == 404


async def test_management_kick_with_revoke() -> None:
    """``DELETE /clients/{id}?revoke=true``：断开 + 吊销，之后重连也被拒。"""
    registry = InMemoryTokenRegistry()
    issued = await registry.issue("alice")

    async with opened_server(registry) as server:
        app = api_app(server)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Authorization": f"Bearer {await login(client)}"}
            async with connect(ws_url(port_of(server), issued.token)) as ws:
                await ws.send(json.dumps(PRIVATE_MESSAGE))
                assert await wait_until(lambda: len(server.roster()) == 1)
                client_id = server.roster()[0].id

                kicked = await client.delete(
                    f"/api/onebot/clients/{client_id}",
                    headers=headers,
                    params={"revoke": "true"},
                )
                assert kicked.status_code == 200, kicked.text
                assert ApiResponse[KickData].model_validate(kicked.json()).data.revoked is True

                with pytest.raises(ConnectionClosed):
                    await ws.recv()

            with pytest.raises(InvalidStatus):  # 令牌没了：重连被拒
                async with connect(ws_url(port_of(server), issued.token)):
                    pass


# --------------------------------------------------------------------- 权限范围（授权）
async def test_scope_admin_sees_every_account() -> None:
    """admin 的范围不限：所有账号的令牌都看得到、都能管。"""
    registry = InMemoryTokenRegistry()
    async with opened_server(registry) as server:
        app = api_app(server)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Authorization": f"Bearer {await login(client)}"}
            await issue_via_api(client, headers, "alice")
            await issue_via_api(client, headers, "bob")

            listed = await client.get("/api/onebot/tokens", headers=headers)
            assert listed.status_code == 200, listed.text
            rows = ApiResponse[list[TokenData]].model_validate(listed.json()).data

    assert {row.account for row in rows} == {"alice", "bob"}


async def test_scope_normal_user_only_sees_own_tokens() -> None:
    """普通用户（robot）只看得到自己账号下的令牌 —— 这就是原来漏掉的那道隔离。"""
    registry = InMemoryTokenRegistry()
    async with opened_server(registry) as server:
        app = api_app(server)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            admin_headers = {"Authorization": f"Bearer {await login(client)}"}
            await issue_via_api(client, admin_headers, "alice")
            await issue_via_api(client, admin_headers, "robot")

            robot_headers = {"Authorization": f"Bearer {await login(client, ROBOT)}"}
            listed = await client.get("/api/onebot/tokens", headers=robot_headers)
            assert listed.status_code == 200, listed.text
            rows = ApiResponse[list[TokenData]].model_validate(listed.json()).data

        assert [row.account for row in rows] == ["robot"]  # alice 那条看不到


async def test_scope_normal_user_cannot_issue_for_another_account() -> None:
    """普通用户给别人的账号签令牌 → 403，而且**真的没签出来**。

    这条原来能得手：``account`` 只是请求体里随便填的字符串，于是普通用户能给自己造一个
    admin 名下的接入身份 —— 那是提权，不是"看到"。
    """
    registry = InMemoryTokenRegistry()
    async with opened_server(registry) as server:
        app = api_app(server)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            admin_headers = {"Authorization": f"Bearer {await login(client)}"}
            robot_headers = {"Authorization": f"Bearer {await login(client, ROBOT)}"}

            denied = await client.post(
                "/api/onebot/tokens",
                headers=robot_headers,
                json={"account": "admin", "remark": "冒名"},
            )
            assert denied.status_code == 403, denied.text

            # 自己账号那条照样能签 —— 不是"普通用户不许签令牌"，是"只能签自己的"
            assert (await issue_via_api(client, robot_headers, "robot")).record.account == "robot"

            listed = await client.get("/api/onebot/tokens", headers=admin_headers)
            rows = ApiResponse[list[TokenData]].model_validate(listed.json()).data

        assert [row.account for row in rows] == ["robot"]  # 冒名那条没进库


async def test_scope_normal_user_cannot_touch_another_accounts_token() -> None:
    """普通用户停用 / 吊销别人的令牌 → 404（按 id 找东西一律 404，不告诉它这条 id 存在）。"""
    registry = InMemoryTokenRegistry()
    async with opened_server(registry) as server:
        app = api_app(server)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            admin_headers = {"Authorization": f"Bearer {await login(client)}"}
            alice = await issue_via_api(client, admin_headers, "alice")
            robot_headers = {"Authorization": f"Bearer {await login(client, ROBOT)}"}

            off = await client.patch(
                f"/api/onebot/tokens/{alice.record.id}",
                headers=robot_headers,
                json={"enabled": False},
            )
            assert off.status_code == 404, off.text
            gone = await client.delete(
                f"/api/onebot/tokens/{alice.record.id}", headers=robot_headers
            )
            assert gone.status_code == 404, gone.text

            listed = await client.get("/api/onebot/tokens", headers=admin_headers)
            rows = ApiResponse[list[TokenData]].model_validate(listed.json()).data

        # **真没动过**：还在、还是启用、令牌也还能认
        assert [(row.account, row.enabled) for row in rows] == [("alice", True)]
        assert await registry.resolve(alice.token) is not None


async def test_scope_client_list_is_scoped() -> None:
    """客户端列表：普通用户不带 ``?account=`` 只看自己账号的；显式要别人的 → 403。"""
    registry = InMemoryTokenRegistry()
    alice_token = (await registry.issue("alice")).token
    robot_token = (await registry.issue("robot")).token

    async with opened_server(registry) as server:
        app = api_app(server)
        # 两个账号各连一条：连上就够，归属在握手时就定了，不必再发事件
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
            connect(ws_url(port_of(server), alice_token)),
            connect(ws_url(port_of(server), robot_token)),
        ):
            assert await wait_until(lambda: len(server.roster()) == 2)
            admin_headers = {"Authorization": f"Bearer {await login(client)}"}
            robot_headers = {"Authorization": f"Bearer {await login(client, ROBOT)}"}

            mine = await client.get("/api/onebot/clients", headers=robot_headers)
            assert mine.status_code == 200, mine.text
            rows = ApiResponse[list[ClientData]].model_validate(mine.json()).data
            assert [row.account for row in rows] == ["robot"]  # 不写参数就按自己的范围收窄

            denied = await client.get(
                "/api/onebot/clients", headers=robot_headers, params={"account": "alice"}
            )
            assert denied.status_code == 403, denied.text

            every = await client.get("/api/onebot/clients", headers=admin_headers)
            assert {row.account for row in ApiResponse[list[ClientData]].model_validate(
                every.json()
            ).data} == {"alice", "robot"}
