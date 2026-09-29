"""OneBot 反向 WS 的测试：令牌定归属（谁的）、在线列表、踢人与吊销，以及管理用的 HTTP 接口。

跑在 127.0.0.1 的空闲端口上（每个用例自己挑一个），WS 客户端用 ``websockets``；
机器人凭证统一走 :class:`~nacho.bots.SqlBotStore`，每个用例挂在一块内存 sqlite
上（见 :func:`memory_registry`），不再单养一份内存实现。需要 ``websockets``
（``pip install "nacho[onebot]"``），没装就整文件跳过。
"""
from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
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
    IssuedTokenData,
    KickData,
    LoginData,
    Pbkdf2PasswordHasher,
    RevokeData,
    TokenData,
    create_app,
)
from nacho.bots import SqlBotStore  # noqa: E402
from nacho.onebot import (  # noqa: E402
    OneBotOptions,
    OneBotServer,
)

#: 演示账号（见 nacho.api.services.user.demo.DEMO_USERS）：id 就是 ``u-admin`` / ``u-robot``
ADMIN = {"account": "admin", "password": "nacho-admin"}
#: 普通用户（roles 里只有 user）：用来验「只能管自己名下那部分」
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


#: 本文件用到的内存引擎：每个用例一份，用例结束由下面的 fixture 统一 dispose
_MEMORY_ENGINES: list[AsyncEngine] = []


async def memory_registry() -> SqlBotStore:
    """一份挂在内存 sqlite 上的机器人凭证存储（表已建好）。

    多实例后统一走 :class:`SqlBotStore`（bot_credentials 表，一个用户多个机器人）。
    """
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    _MEMORY_ENGINES.append(engine)
    registry = SqlBotStore(engine)
    await registry.ensure_schema()
    return registry


@pytest.fixture(autouse=True)
async def _dispose_memory_engines() -> AsyncGenerator[None, None]:
    """用例结束把上面那些内存引擎关掉（否则连接会跟着事件循环一起悬着）。"""
    yield
    while _MEMORY_ENGINES:
        await _MEMORY_ENGINES.pop().dispose()


@asynccontextmanager
async def opened_server(
    registry: SqlBotStore | None = None,
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
    """接口层应用（演示账号由 ``create_app`` 兜底种好），把 OneBot 服务挂上去给管理接口用。"""
    return create_app(
        ApiOptions(prefix="/api"),
        hasher=_TEST_HASHER,
        onebot=server,
    )


@asynccontextmanager
async def api_client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    """直连 ASGI 的客户端，并跑一遍 app 的 lifespan（会话表在 lifespan 里建）。

    ``create_app`` 不接库时会话挂在一块内存 sqlite 上、建表在 lifespan，而 httpx 的
    ASGITransport 不会自己触发启动，所以这里替它跑一遍。
    """
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client


async def login(client: httpx.AsyncClient, who: dict[str, str] | None = None) -> str:
    """登录拿令牌（管理接口都要带它）；``who`` 不传就是 admin。"""
    ok = await client.post("/api/auth/login", json=who or ADMIN)
    assert ok.status_code == 200, ok.text
    return ApiResponse[LoginData].model_validate(ok.json()).data.token


async def issue_via_api(
    client: httpx.AsyncClient, headers: dict[str, str], account: str = ""
) -> IssuedTokenData:
    """走管理接口签一个令牌（顺带断言能签出来），返回签发结果。

    ``account`` 是「接入 WS 的机器人账号」，只用来展示；归属永远是当前登录用户。
    """
    made = await client.post(
        "/api/onebot/tokens", headers=headers, json={"account": account, "remark": account}
    )
    assert made.status_code == 200, made.text
    return ApiResponse[IssuedTokenData].model_validate(made.json()).data


# --------------------------------------------------------------------------- 机器人凭证（多实例）
async def test_registry_issue_resolve_and_revoke() -> None:
    """签发 -> 认领 -> 列出 -> 吊销：多实例后 id 是 bot_id、归属另看 owner_id。"""
    registry = await memory_registry()
    await registry.ensure_schema()  # 幂等：再建一次不报错
    issued = await registry.issue("alice", account="机器人一号", remark="主号")

    found = await registry.resolve(issued.token)
    assert found is not None and found.owner_id == "alice"  # 归属是签进去的那个 owner_id
    assert found.id == issued.record.id  # 主键是 bot_id（随机），不再等于归属
    assert found.account == "机器人一号" and found.remark == "主号"
    assert await registry.resolve("nbo_不存在") is None
    assert len(await registry.list_records()) == 1

    assert await registry.remove_by_id(issued.record.id) is True
    assert await registry.resolve(issued.token) is None  # 吊销后认不出来
    assert await registry.remove_by_id(issued.record.id) is False  # 再删一次：没有了


async def test_registry_reissue_creates_another_bot() -> None:
    """多实例：同一个归属再签 = 一个新机器人（新 bot_id），旧令牌仍然有效。"""
    registry = await memory_registry()
    first = await registry.issue("alice", account="旧的", remark="旧备注")

    second = await registry.issue("alice", account="新的")

    assert await registry.resolve(first.token) is not None  # 旧机器人还在（不再换钥匙）
    assert (await registry.resolve(second.token)) is not None
    rows = await registry.list_records(owner_id="alice")
    assert len(rows) == 2  # 现在是两个机器人
    assert first.record.id != second.record.id  # 各自独立的 bot_id


# --------------------------------------------------------------------------- 握手：令牌定归属
async def test_handshake_binds_owner() -> None:
    """带对令牌连进来：归属绑在连接上，机器人号收到第一条事件后才学到。"""
    registry = await memory_registry()
    alice = await registry.issue("alice")
    bob = await registry.issue("bob")

    async with opened_server(registry) as server:
        port = port_of(server)
        async with connect(ws_url(port, alice.token)) as ws, connect(ws_url(port, bob.token)):
            await ws.send(json.dumps(PRIVATE_MESSAGE))
            assert await wait_until(lambda: len(server.roster()) == 2)

            assert {item.id for item in server.roster()} == {"alice", "bob"}
            # 列表是快照，要重新取才能看到刚学到的机器人号
            assert await wait_until(
                lambda: any(
                    item.id == "alice" and item.self_id == 10001 for item in server.roster()
                )
            )
            bob_entry = next(item for item in server.roster() if item.id == "bob")
            assert bob_entry.self_id is None  # 没发过事件，还不知道是哪个机器人

            # 只看某个归属下的
            assert [item.id for item in server.roster(id="alice")] == ["alice"]


async def test_same_token_new_connection_replaces_old() -> None:
    """一个令牌（归属）同时只允许一条连接：第二个连进来把第一个顶掉。"""
    registry = await memory_registry()
    issued = await registry.issue("alice")

    async with opened_server(registry) as server:
        port = port_of(server)
        async with connect(ws_url(port, issued.token)) as first:
            assert await wait_until(lambda: len(server.roster()) == 1)
            old_client_id = server.roster()[0].client_id

            async with connect(ws_url(port, issued.token)) as second:
                # 旧连接被顶掉：roster 始终只有一条，且是新的那一条
                assert await wait_until(lambda: len(server.roster()) == 1)
                assert server.roster()[0].client_id != old_client_id
                with pytest.raises(ConnectionClosed):
                    await first.recv()


async def test_handshake_rejects_bad_token() -> None:
    """令牌不对 / 没带令牌：握手就 401，连不进来。"""
    registry = await memory_registry()
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
            assert server.roster()[0].id == ""


# --------------------------------------------------------------------------- 在线列表 / 踢人 / 吊销
async def test_kick_disconnects_client() -> None:
    """踢下线：客户端那条连接被断开，在线列表里也消失。"""
    registry = await memory_registry()
    issued = await registry.issue("alice")

    async with opened_server(registry) as server:
        async with connect(ws_url(port_of(server), issued.token)) as ws:
            await ws.send(json.dumps(PRIVATE_MESSAGE))
            assert await wait_until(lambda: len(server.roster()) == 1)

            assert await server.kick("不存在的连接") is False  # 没有这条连接
            assert await server.kick(server.roster()[0].client_id) is True

            with pytest.raises(ConnectionClosed):
                await ws.recv()
            assert await wait_until(lambda: server.roster() == ())


async def test_kick_with_revoke_blocks_reconnect() -> None:
    """踢 + 吊销：断开之后重连也被拒（只踢不断令牌的话，客户端会自己重连回来）。"""
    registry = await memory_registry()
    issued = await registry.issue("alice")

    async with opened_server(registry) as server:
        port = port_of(server)
        async with connect(ws_url(port, issued.token)):
            assert await wait_until(lambda: len(server.roster()) == 1)
            assert await server.kick(server.roster()[0].client_id, revoke=True) is True
            assert await wait_until(lambda: server.roster() == ())

        with pytest.raises(InvalidStatus):  # 令牌已吊销：重连 401
            async with connect(ws_url(port, issued.token)):
                pass


async def test_kick_without_revoke_keeps_token() -> None:
    """只踢不吊销：令牌还在，拿同一个令牌还能连上（这就是要 revoke 的原因）。"""
    registry = await memory_registry()
    issued = await registry.issue("alice")

    async with opened_server(registry) as server:
        port = port_of(server)
        async with connect(ws_url(port, issued.token)):
            assert await wait_until(lambda: len(server.roster()) == 1)
            assert await server.kick(server.roster()[0].client_id) is True
            assert await wait_until(lambda: server.roster() == ())

        async with connect(ws_url(port, issued.token)):  # 重连成功
            assert await wait_until(lambda: len(server.roster()) == 1)


async def test_revoke_by_id_disconnects_client() -> None:
    """按归属 id 吊销：记录删掉，正用它连着的客户端也断开（从列表里消失）。"""
    registry = await memory_registry()
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
    registry = await memory_registry()
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
            assert server.roster()[0].id == "alice"


async def test_set_enabled_unknown_id() -> None:
    """id 不存在：改不动，返回 False。"""
    async with opened_server(await memory_registry()) as server:
        assert await server.set_token_enabled("不存在", True) is False


# --------------------------------------------------------------------------- 管理用的 HTTP 接口
async def test_management_requires_login() -> None:
    """管理接口都要登录：没带令牌一律 401（先鉴权，再看服务接没接）。"""
    async with opened_server(await memory_registry()) as server:
        app = api_app(server)
        async with api_client(app) as client:
            assert (await client.get("/api/onebot/clients")).status_code == 401
            assert (await client.get("/api/onebot/tokens")).status_code == 401
            assert (await client.delete("/api/onebot/clients/whatever")).status_code == 401


async def test_management_reports_not_configured() -> None:
    """主程序没传 OneBot 服务：接口回 503 说「没接入」，而不是 500。"""
    async with api_client(api_app(None)) as client:
        token = await login(client)
        response = await client.get(
            "/api/onebot/clients", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 503


async def test_management_lists_clients_and_manages_tokens() -> None:
    """登录后：看在线列表 -> 签一个令牌 -> 列出来 -> 吊销掉。"""
    registry = await memory_registry()
    async with opened_server(registry) as server:
        app = api_app(server)
        async with api_client(app) as client:
            token = await login(client)
            headers = {"Authorization": f"Bearer {token}"}

            # 1. 还没有客户端
            listed = await client.get("/api/onebot/clients", headers=headers)
            assert listed.status_code == 200, listed.text
            assert ApiResponse[list[ClientData]].model_validate(listed.json()).data == []

            # 2. 签一个令牌：归属不用填（就是当前登录用户），account 是机器人账号
            issued = await client.post(
                "/api/onebot/tokens",
                headers=headers,
                json={"account": "机器人一号", "remark": "主号"},
            )
            assert issued.status_code == 200, issued.text
            # 用项目自己的响应壳解析：类型化的 data，顺带断言了响应协议形状
            issued_data = ApiResponse[IssuedTokenData].model_validate(issued.json()).data
            assert issued_data.record.owner_id == "u-admin"  # 归属 = 当前登录用户的 id
            assert issued_data.record.nickname == "管理员"  # 昵称按 owner_id 去用户表查
            assert issued_data.record.account == "机器人一号"
            assert issued_data.token.startswith("nbo_")  # 明文只在这一次露出来

            # 3. 令牌列表里有它（且**不含明文**：响应模型里根本没有 token 字段）
            tokens = await client.get("/api/onebot/tokens", headers=headers)
            assert tokens.status_code == 200, tokens.text
            records = ApiResponse[list[TokenData]].model_validate(tokens.json()).data
            assert [item.owner_id for item in records] == ["u-admin"]
            assert "token" not in TokenData.model_fields
            # 刚签发、还没连接：派生态 online 是 False（不落库，接口层实时聚合）
            assert records[0].online is False
            assert records[0].clients == []

            # 4. 拿这个令牌连进来，在线列表里能看到它属于 u-admin
            async with connect(ws_url(port_of(server), issued_data.token)) as ws:
                await ws.send(json.dumps(PRIVATE_MESSAGE))
                assert await wait_until(lambda: len(server.roster()) == 1)
                assert await wait_until(
                    lambda: server.roster()[0].self_id == 10001  # 等它学到机器人号
                )

                clients = await client.get(
                    "/api/onebot/clients", headers=headers, params={"id": "u-admin"}
                )
                assert clients.status_code == 200, clients.text
                rows = ApiResponse[list[ClientData]].model_validate(clients.json()).data
                assert len(rows) == 1 and rows[0].owner_id == "u-admin"
                assert rows[0].nickname == "管理员"  # 归属昵称：和令牌列表一个口径
                assert rows[0].account == "机器人一号"  # 令牌里带的机器人账号
                assert rows[0].self_id == 10001

                # 4.5 令牌列表把在线状态融合进来：online=True，且带连着它的连接详情
                tokens_live = await client.get("/api/onebot/tokens", headers=headers)
                assert tokens_live.status_code == 200, tokens_live.text
                live = ApiResponse[list[TokenData]].model_validate(tokens_live.json()).data
                assert live[0].online is True
                assert len(live[0].clients) == 1
                assert live[0].clients[0].owner_id == "u-admin"
                assert live[0].clients[0].self_id == 10001
                assert live[0].clients[0].remote  # 对端地址也带上了

                # 5. 踢下线（不吊销）
                kicked = await client.delete(
                    f"/api/onebot/clients/{rows[0].client_id}", headers=headers
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
    registry = await memory_registry()
    async with opened_server(registry) as server:
        app = api_app(server)
        async with api_client(app) as client:
            headers = {"Authorization": f"Bearer {await login(client)}"}
            issued = await registry.issue("u-admin")

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
                "/api/onebot/tokens/不存在", headers=headers, json={"enabled": True}
            )
            assert missing.status_code == 404


async def test_management_kick_with_revoke() -> None:
    """``DELETE /clients/{client_id}?revoke=true``：断开 + 吊销，之后重连也被拒。"""
    registry = await memory_registry()
    issued = await registry.issue("u-admin")

    async with opened_server(registry) as server:
        app = api_app(server)
        async with api_client(app) as client:
            headers = {"Authorization": f"Bearer {await login(client)}"}
            async with connect(ws_url(port_of(server), issued.token)) as ws:
                await ws.send(json.dumps(PRIVATE_MESSAGE))
                assert await wait_until(lambda: len(server.roster()) == 1)
                client_id = server.roster()[0].client_id

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
async def test_scope_admin_sees_every_owner() -> None:
    """admin 的范围不限：所有人的令牌都看得到。"""
    async with opened_server(await memory_registry()) as server:
        app = api_app(server)
        async with api_client(app) as client:
            admin_headers = {"Authorization": f"Bearer {await login(client)}"}
            robot_headers = {"Authorization": f"Bearer {await login(client, ROBOT)}"}
            await issue_via_api(client, admin_headers, "管理员的机器人")
            await issue_via_api(client, robot_headers, "巡检机器人")

            listed = await client.get("/api/onebot/tokens", headers=admin_headers)
            assert listed.status_code == 200, listed.text
            rows = ApiResponse[list[TokenData]].model_validate(listed.json()).data

    # 归属就是各自的 user id；account 是各自填的机器人账号
    assert {row.owner_id for row in rows} == {"u-admin", "u-robot"}
    assert {row.account for row in rows} == {"管理员的机器人", "巡检机器人"}


async def test_scope_normal_user_only_sees_own_token() -> None:
    """普通用户（robot）只看得到自己那条 —— 归属就是各自的 id。"""
    async with opened_server(await memory_registry()) as server:
        app = api_app(server)
        async with api_client(app) as client:
            admin_headers = {"Authorization": f"Bearer {await login(client)}"}
            robot_headers = {"Authorization": f"Bearer {await login(client, ROBOT)}"}
            await issue_via_api(client, admin_headers, "管理员的机器人")
            await issue_via_api(client, robot_headers, "巡检机器人")

            listed = await client.get("/api/onebot/tokens", headers=robot_headers)
            assert listed.status_code == 200, listed.text
            rows = ApiResponse[list[TokenData]].model_validate(listed.json()).data

        assert [row.owner_id for row in rows] == ["u-robot"]  # 管理员那条看不到
        assert [row.nickname for row in rows] == ["巡检机器人"]  # 昵称按 owner_id 查出来的


async def test_issue_always_belongs_to_the_caller() -> None:
    """签发**填不了归属**：``account`` 只是机器人账号，归属永远是当前登录用户。

    这条原来是个提权口子：那个字段是请求体里随便填的字符串，普通用户能给自己造一个
    admin 名下的接入身份。
    """
    async with opened_server(await memory_registry()) as server:
        app = api_app(server)
        async with api_client(app) as client:
            robot_headers = {"Authorization": f"Bearer {await login(client, ROBOT)}"}
            issued = await issue_via_api(client, robot_headers, "随便叫什么都行")
            assert issued.record.owner_id == "u-robot"  # 归属还是自己
            assert issued.record.account == "随便叫什么都行"  # 那个字段只是展示用的机器人账号

            admin_headers = {"Authorization": f"Bearer {await login(client)}"}
            listed = await client.get("/api/onebot/tokens", headers=admin_headers)
            rows = ApiResponse[list[TokenData]].model_validate(listed.json()).data

        assert [(row.owner_id, row.account) for row in rows] == [("u-robot", "随便叫什么都行")]


async def test_scope_normal_user_cannot_touch_another_owners_token() -> None:
    """普通用户停用 / 吊销别人的令牌 → 404（按 id 找东西一律 404，不告诉它这条 id 存在）。"""
    async with opened_server(await memory_registry()) as server:
        app = api_app(server)
        async with api_client(app) as client:
            admin_headers = {"Authorization": f"Bearer {await login(client)}"}
            admin_issued = await issue_via_api(client, admin_headers, "管理员的机器人")
            robot_headers = {"Authorization": f"Bearer {await login(client, ROBOT)}"}

            target = admin_issued.record.id  # = u-admin
            off = await client.patch(
                f"/api/onebot/tokens/{target}",
                headers=robot_headers,
                json={"enabled": False},
            )
            assert off.status_code == 404, off.text
            gone = await client.delete(f"/api/onebot/tokens/{target}", headers=robot_headers)
            assert gone.status_code == 404, gone.text

            listed = await client.get("/api/onebot/tokens", headers=admin_headers)
            rows = ApiResponse[list[TokenData]].model_validate(listed.json()).data

        # **真没动过**：还在、还是启用
        assert [(row.owner_id, row.enabled) for row in rows] == [("u-admin", True)]


async def test_scope_client_list_is_scoped() -> None:
    """客户端列表：普通用户不带 ``?id=`` 只看自己那条；显式要别人的 → 403。"""
    registry = await memory_registry()
    admin_token = (await registry.issue("u-admin")).token
    robot_token = (await registry.issue("u-robot")).token

    async with opened_server(registry) as server:
        app = api_app(server)
        # 两个归属各连一条：连上就够，归属在握手时就定了，不必再发事件
        async with (
            api_client(app) as client,
            connect(ws_url(port_of(server), admin_token)),
            connect(ws_url(port_of(server), robot_token)),
        ):
            assert await wait_until(lambda: len(server.roster()) == 2)
            admin_headers = {"Authorization": f"Bearer {await login(client)}"}
            robot_headers = {"Authorization": f"Bearer {await login(client, ROBOT)}"}

            mine = await client.get("/api/onebot/clients", headers=robot_headers)
            assert mine.status_code == 200, mine.text
            rows = ApiResponse[list[ClientData]].model_validate(mine.json()).data
            assert [row.owner_id for row in rows] == ["u-robot"]  # 不写参数就按自己的范围收窄

            denied = await client.get(
                "/api/onebot/clients", headers=robot_headers, params={"id": "u-admin"}
            )
            assert denied.status_code == 403, denied.text

            every = await client.get("/api/onebot/clients", headers=admin_headers)
            assert {
                row.owner_id
                for row in ApiResponse[list[ClientData]].model_validate(every.json()).data
            } == {"u-admin", "u-robot"}


# --------------------------------------------------------------------- 机器人接口（P5-2 泛化壳）
async def test_bots_list_and_add_onebot() -> None:
    """/api/bots：列表与添加；platform=onebot 走现有令牌签发，响应带 platform 字段。"""
    async with opened_server(await memory_registry()) as server:
        app = api_app(server)
        async with api_client(app) as client:
            headers = {"Authorization": f"Bearer {await login(client)}"}

            # 初始为空
            listed = await client.get("/api/bots", headers=headers)
            assert listed.status_code == 200, listed.text
            assert ApiResponse[list[dict]].model_validate(listed.json()).data == []

            # 添加一个 onebot 机器人
            added = await client.post(
                "/api/bots",
                headers=headers,
                json={"platform": "onebot", "account": "机器人一号", "remark": "主号"},
            )
            assert added.status_code == 200, added.text
            data = added.json()["data"]
            assert data["record"]["platform"] == "onebot"
            assert data["record"]["owner_id"] == "u-admin"  # 归属 = 当前登录用户
            assert data["token"].startswith("nbo_")  # 明文只露这一次

            # 列表里出现
            listed = await client.get("/api/bots", headers=headers)
            rows = listed.json()["data"]
            assert [row["platform"] for row in rows] == ["onebot"]
            assert rows[0]["account"] == "机器人一号"


async def test_bots_add_kook_not_implemented() -> None:
    """platform=kook 暂未接入：回 501。"""
    async with opened_server(await memory_registry()) as server:
        app = api_app(server)
        async with api_client(app) as client:
            headers = {"Authorization": f"Bearer {await login(client)}"}
            added = await client.post(
                "/api/bots",
                headers=headers,
                json={"platform": "kook", "account": "kook-bot"},
            )
            assert added.status_code == 501


async def test_bots_requires_login() -> None:
    """机器人接口都要登录：没带令牌 401。"""
    async with opened_server(await memory_registry()) as server:
        app = api_app(server)
        async with api_client(app) as client:
            assert (await client.get("/api/bots")).status_code == 401
            assert (await client.post("/api/bots", json={"platform": "onebot"})).status_code == 401
