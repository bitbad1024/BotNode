"""``nacho/api`` 的单元测试：登录流程、输入校验、错误出口、令牌与日志接入点。

请求不走真实网络：用 ``httpx.AsyncClient`` 挂 ``ASGITransport`` 直接打进 ASGI 应用，
于是请求与日志核心跑在**同一个事件循环**里（日志是同步入队的，换线程会丢），
「访问日志有没有落进文件」这类断言才站得住。

需要 ``fastapi`` / ``httpx``（``pip install "nacho[api]"`` + dev 依赖）；没装就整文件跳过。
"""
from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import ClassVar, override

import pytest

pytest.importorskip("fastapi", reason="接口层要装 fastapi：pip install \"nacho[api]\"")
pytest.importorskip("httpx", reason="接口层测试用 httpx 发请求：pip install \"nacho[dev]\"")

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402

from config import ConfigError, Settings  # noqa: E402
from nacho.api import (  # noqa: E402
    ACCESS_LOGGER_NAME,
    API_LOGGER_NAME,
    AccountAlreadyExistsError,
    ApiOptions,
    ApiResponse,
    AuthService,
    Credentials,
    ErrorCode,
    InvalidCredentialsError,
    LogData,
    LogPage,
    LoginData,
    LoginRequest,
    RegisterRequest,
    SESSION_COOKIE,
    ClientInfo,
    PasswordHasher,
    Pbkdf2PasswordHasher,
    SessionData,
    SessionService,
    SqlSessionStore,
    SqlUserStore,
    create_app,
    profile_of,
)
from nacho.core.logger import (  # noqa: E402
    BaseLogProcessor,
    ConsoleLogProcessor,
    DatabaseLogProcessor,
    LocalFileLogProcessor,
    LogCore,
    LogRecord,
    LogSearchResult,
    configure,
    get_logger,
    manager,
)
from nacho.db import SqlLogStore  # noqa: E402

#: 演示账号（见 nacho.api.services.user.demo.DEMO_USERS）
ADMIN = {"account": "admin", "password": "nacho-admin"}
#: 第二个账号：测「不是本人的令牌不复用」要用两个人
ROBOT = {"account": "robot", "password": "nacho-robot"}
LOGIN_PATH = "/api/auth/login"
REGISTER_PATH = "/api/auth/register"

#: 测试用的哈希迭代次数。默认 20 万次是**生产该有的值**（单次约 67ms，专门用来拖慢离线爆破），
#: 但这份钱不该让每个用例重付一遍 —— 这里降到 1000 次，爆破成本由 :class:`Pbkdf2PasswordHasher`
#: 自己的默认值负责，登录流程的断言与迭代次数无关（哈希串自带参数，验适用串里那份）。
TEST_ITERATIONS: int = 1_000

_TEST_HASHER = Pbkdf2PasswordHasher(iterations=TEST_ITERATIONS)


@asynccontextmanager
async def client_for(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    """一个直连 ASGI 应用的异步客户端（不发真实网络请求）。

    顺带把 app 的 lifespan 跑起来：建表在里面，而 httpx 的 ASGITransport 不会自己触发启动。
    ``create_app`` 不接库时会话挂在一块内存 sqlite 上，正好靠它建表。
    """
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client


#: 本文件用到的内存引擎：每个用例一份，用例结束由下面的 fixture 统一 dispose
_MEMORY_ENGINES: list[AsyncEngine] = []


async def _memory_engine() -> AsyncEngine:
    """一块内存 sqlite 引擎（记下来给 fixture 关）。"""
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    _MEMORY_ENGINES.append(engine)
    return engine


async def memory_user_store(hasher: PasswordHasher) -> SqlUserStore:
    """挂在内存 sqlite 上的用户存储（表已建好 + 演示账号已种）。"""
    store = SqlUserStore(await _memory_engine(), hasher=hasher)
    await store.ensure_schema()
    await store.seed_demo()
    return store


async def memory_session_store() -> SqlSessionStore:
    """挂在内存 sqlite 上的会话存储（表已建好）。"""
    store = SqlSessionStore(await _memory_engine())
    await store.ensure_schema()
    return store


@pytest.fixture(autouse=True)
async def _dispose_memory_engines() -> AsyncIterator[None]:
    """用例结束把上面那些内存引擎关掉（否则连接会跟着事件循环一起悬着）。"""
    yield
    while _MEMORY_ENGINES:
        await _MEMORY_ENGINES.pop().dispose()


def app_with(**overrides: object) -> FastAPI:
    """建一个应用：默认前缀 /api、访问令牌 1800 秒（测试要可重现）。

    不传用户 / 会话存储：``create_app`` 兜底挂一块内存 sqlite 并种好演示账号（建表在
    lifespan，``client_for`` 会替应用跑一遍）。令牌索引由 ``TokenIndex`` 自带的内存兜底
    顶上（测试里没人去 ``cache.start()``）。
    """
    options = ApiOptions(prefix="/api", token_ttl=1800.0)
    return create_app(replace(options, **overrides), hasher=_TEST_HASHER)


async def drain(core: LogCore) -> None:
    """等分发器把队列里的日志交给处理机，再刷缓冲。

    写日志只是入队，落盘由分发器异步做：不先等一轮就直接 ``flush``，刷的是空缓冲。
    """
    await asyncio.sleep(0.1)
    await core.flush()


def records_of(path: Path) -> list[dict[str, object]]:
    """读出文件日志里所有记录（每行一个 JSON）。"""
    lines: list[str] = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


@pytest.fixture
async def core(tmp_path: Path) -> AsyncIterator[LogCore]:
    """一个跑起来的日志核心（无控制台），用完停机并复位门面，免得影响别的测试。

    分发间隔取小值：测试里刚挂上的文件出口要等分发器下一轮才被拉起来。
    """
    manager.reset()
    started: LogCore = configure("nacho", level="DEBUG", console=False, dispatch_timeout=0.05)
    await started.start()
    try:
        yield started
    finally:
        await manager.stop()
        manager.reset()


# --------------------------------------------------------------------------- 登录
class TestLogin:
    """登录成功与各种失败：出口形状统一，失败口径不泄露账号是否存在。"""

    async def test_login_success_returns_token_and_profile(self) -> None:
        async with client_for(app_with()) as client:
            response = await client.post(LOGIN_PATH, json=ADMIN)
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["token_type"] == "bearer"
        assert body["data"]["expires_in"] == 1800
        assert body["data"]["user"] == {
            "id": "u-admin",
            "account": "admin",
            "nickname": "管理员",
            "roles": ["admin", "user"],
        }
        # 响应头与响应体里的编号是同一个，用户报一次就够查日志
        assert response.headers["X-Trace-Id"] == body["trace_id"]

    async def test_success_response_never_carries_password(self) -> None:
        """协议层兜底：请求里的密码（SecretStr）不会出现在任何响应里。"""
        async with client_for(app_with()) as client:
            body = (await client.post(LOGIN_PATH, json=ADMIN)).json()
        assert "nacho-admin" not in json.dumps(body, ensure_ascii=False)
        assert "password" not in json.dumps(body)

    async def test_wrong_password_is_401_invalid_credentials(self) -> None:
        async with client_for(app_with()) as client:
            response = await client.post(
                LOGIN_PATH, json={"account": "admin", "password": "wrong-password"}
            )
        assert response.status_code == 401
        error = response.json()["error"]
        assert error["code"] == ErrorCode.INVALID_CREDENTIALS  # 大写错误码
        assert response.headers["WWW-Authenticate"] == "Bearer"

    async def test_unknown_account_uses_same_code_as_wrong_password(self) -> None:
        """账号存不存在不给不同回执：否则等于把账号名单送出去。"""
        codes: list[str] = []
        async with client_for(app_with()) as client:
            for payload in (
                {"account": "nobody", "password": "wrong-password"},
                {"account": "admin", "password": "wrong-password"},
            ):
                codes.append((await client.post(LOGIN_PATH, json=payload)).json()["error"]["code"])
        assert codes == [ErrorCode.INVALID_CREDENTIALS, ErrorCode.INVALID_CREDENTIALS]

    async def test_disabled_account_is_403(self) -> None:
        async with client_for(app_with()) as client:
            response = await client.post(
                LOGIN_PATH, json={"account": "guest", "password": "nacho-guest"}
            )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == ErrorCode.ACCOUNT_DISABLED


# --------------------------------------------------------------------------- 注册
class TestRegister:
    """注册：新建账号（201 + 资料、不发令牌）/ 账号已被注册（409）/ 参数不合法（422）。"""

    #: 一个没被占用的账号（每个用例各自起一个内存库，互不打扰）
    NEW: ClassVar[dict[str, str]] = {
        "account": "newbie",
        "password": "nacho-newbie",
        "nickname": "新来的",
    }

    async def test_success_returns_profile_without_token(self) -> None:
        async with client_for(app_with()) as client:
            response = await client.post(REGISTER_PATH, json=self.NEW)
        assert response.status_code == 201
        body = response.json()
        assert body["success"] is True
        assert body["data"]["account"] == "newbie"
        assert body["data"]["nickname"] == "新来的"
        assert body["data"]["roles"] == []
        # 注册不发令牌、也不种 Cookie：令牌与会话只有登录一个出口
        assert "token" not in body["data"]
        assert SESSION_COOKIE not in response.headers.get("set-cookie", "")

    async def test_success_response_never_carries_password(self) -> None:
        async with client_for(app_with()) as client:
            body = (await client.post(REGISTER_PATH, json=self.NEW)).json()
        assert "nacho-newbie" not in json.dumps(body, ensure_ascii=False)
        assert "password" not in json.dumps(body)

    async def test_registered_account_can_login_right_away(self) -> None:
        """注册出来的账号立刻能登录：密码确实被哈希后存进去了。"""
        async with client_for(app_with()) as client:
            created = await client.post(REGISTER_PATH, json=self.NEW)
            response = await client.post(
                LOGIN_PATH, json={"account": "newbie", "password": "nacho-newbie"}
            )
        assert created.status_code == 201
        assert response.status_code == 200
        assert response.json()["data"]["user"]["account"] == "newbie"

    async def test_duplicate_account_is_409(self) -> None:
        """第二次注册同一个账号：409 + ACCOUNT_ALREADY_EXISTS（注册必须说清是哪种失败）。"""
        async with client_for(app_with()) as client:
            first = await client.post(REGISTER_PATH, json=self.NEW)
            second = await client.post(REGISTER_PATH, json=self.NEW)
        assert first.status_code == 201
        assert second.status_code == 409
        assert second.json()["error"]["code"] == ErrorCode.ACCOUNT_ALREADY_EXISTS

    async def test_demo_account_counts_as_taken(self) -> None:
        """演示账号（admin）已经在库里，注册它同样是 409。"""
        async with client_for(app_with()) as client:
            response = await client.post(
                REGISTER_PATH,
                json={
                    "account": "admin",
                    "password": "nacho-another",
                    "nickname": "冒牌管理员",
                },
            )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == ErrorCode.ACCOUNT_ALREADY_EXISTS

    async def test_bad_input_reports_every_field(self) -> None:
        """账号字符集 / 密码长度 / 昵称长度各自报自己的字段，客户端才好逐个改。"""
        async with client_for(app_with()) as client:
            response = await client.post(
                REGISTER_PATH,
                json={"account": "a b", "password": "short", "nickname": ""},
            )
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == ErrorCode.VALIDATION_ERROR
        assert sorted(detail["field"] for detail in error["details"]) == [
            "body.account",
            "body.nickname",
            "body.password",
        ]

    async def test_missing_nickname_is_422(self) -> None:
        """昵称必填（请求契约如此；前端表单也会先拦一道）。"""
        async with client_for(app_with()) as client:
            response = await client.post(
                REGISTER_PATH, json={"account": "newbie", "password": "nacho-newbie"}
            )
        assert response.status_code == 422
        assert [detail["field"] for detail in response.json()["error"]["details"]] == [
            "body.nickname"
        ]

    def test_register_request_keeps_password_secret(self) -> None:
        """密码是 ``SecretStr``：从请求体（JSON）解析出来也打印不出明文。"""
        request = RegisterRequest.model_validate(
            {"account": "newbie", "password": "nacho-newbie", "nickname": "新来的"}
        )
        assert "nacho-newbie" not in repr(request)
        assert request.nickname == "新来的"


# ----------------------------------------------------------------------- 输入校验
class TestInputValidation:
    """请求体没过校验：422 + 逐字段细节（field 指得准，客户端才好改）。"""

    async def test_short_account_is_rejected_with_field(self) -> None:
        async with client_for(app_with()) as client:
            response = await client.post(LOGIN_PATH, json={"account": "ab", "password": "nacho-admin"})
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == ErrorCode.VALIDATION_ERROR
        assert [detail["field"] for detail in error["details"]] == ["body.account"]

    async def test_illegal_account_characters_are_rejected(self) -> None:
        async with client_for(app_with()) as client:
            response = await client.post(
                LOGIN_PATH, json={"account": "admin 中文", "password": "nacho-admin"}
            )
        assert response.status_code == 422
        assert "账号只能包含" in response.json()["error"]["details"][0]["message"]

    async def test_short_password_is_rejected(self) -> None:
        async with client_for(app_with()) as client:
            response = await client.post(LOGIN_PATH, json={"account": "admin", "password": "123"})
        assert response.status_code == 422
        assert [detail["field"] for detail in response.json()["error"]["details"]] == ["body.password"]

    async def test_missing_fields_report_every_one(self) -> None:
        async with client_for(app_with()) as client:
            response = await client.post(LOGIN_PATH, json={})
        assert response.status_code == 422
        fields: list[str] = [detail["field"] for detail in response.json()["error"]["details"]]
        assert fields == ["body.account", "body.password"]

    async def test_wrong_type_is_rejected(self) -> None:
        async with client_for(app_with()) as client:
            response = await client.post(LOGIN_PATH, json={"account": "admin", "password": 12345678})
        assert response.status_code == 422

    async def test_unknown_route_keeps_error_shape(self) -> None:
        """404 也走统一结构（Starlette 的 HTTP 异常被接住了）。"""
        async with client_for(app_with()) as client:
            response = await client.get("/api/nope")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == ErrorCode.HTTP_ERROR


# --------------------------------------------------------------------------- 令牌
class TestToken:
    """``/api/auth/me``：令牌怎么用、过期与篡改怎么回。"""

    async def test_me_returns_current_user(self) -> None:
        async with client_for(app_with()) as client:
            token: str = (await client.post(LOGIN_PATH, json=ADMIN)).json()["data"]["token"]
            response = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.json()["data"]["account"] == "admin"

    async def test_me_without_token_is_401(self) -> None:
        async with client_for(app_with()) as client:
            response = await client.get("/api/auth/me")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == ErrorCode.UNAUTHORIZED

    async def test_tampered_token_is_401(self) -> None:
        """令牌是随机串，不是签名：改一个字就换不出会话，直接不认。"""
        async with client_for(app_with()) as client:
            token: str = (await client.post(LOGIN_PATH, json=ADMIN)).json()["data"]["token"]
            # 登录已经让客户端带上了会话 Cookie，不清掉的话请求靠 Cookie 就过了，
            # 这条要测的是"头里那个坏令牌"，所以先把 Cookie 拿掉
            client.cookies.clear()
            response = await client.get(
                "/api/auth/me", headers={"Authorization": f"Bearer {token[:-2]}xy"}
            )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == ErrorCode.UNAUTHORIZED

    async def test_login_sets_httponly_cookie(self) -> None:
        """令牌写进 HttpOnly Cookie：JS 读不到，XSS 也就偷不走。"""
        async with client_for(app_with()) as client:
            response = await client.post(LOGIN_PATH, json=ADMIN)
            cookie_header: str = response.headers["set-cookie"]
            token: str = response.json()["data"]["token"]

        assert SESSION_COOKIE in cookie_header
        assert "HttpOnly" in cookie_header
        assert "SameSite=lax" in cookie_header.lower() or "samesite=lax" in cookie_header.lower()
        assert "Max-Age" not in cookie_header  # 没勾「记住设备」= 会话 Cookie（关浏览器即丢）
        assert token in cookie_header

    async def test_remember_makes_the_cookie_persistent(self) -> None:
        """勾了「记住设备」：Cookie 带上 Max-Age（持久），服务端有效期也变长。"""
        async with client_for(app_with()) as client:
            response = await client.post(LOGIN_PATH, json={**ADMIN, "remember": True})
            cookie_header: str = response.headers["set-cookie"]
            data = ApiResponse[LoginData].model_validate(response.json()).data

        assert "Max-Age=" in cookie_header
        assert data.expires_in == int(ApiOptions().remember_ttl)

    async def test_login_reuses_the_cookie_token(self) -> None:
        """同一台设备再登录一次（Cookie 自动带上）：**复用**旧令牌，不新建会话。

        这正是浏览器的情形——令牌在 HttpOnly Cookie 里，JS 拿不到、塞不进请求体，所以那
        条路只能靠后端自己从 Cookie 里取。好处是设备列表不会堆出一串重复记录，手里的
        Cookie 也不会被新令牌顶掉。
        """
        async with client_for(app_with()) as client:
            first = ApiResponse[LoginData].model_validate(
                (await client.post(LOGIN_PATH, json=ADMIN)).json()
            ).data
            second = ApiResponse[LoginData].model_validate(
                (await client.post(LOGIN_PATH, json=ADMIN)).json()
            ).data
            listed = ApiResponse[list[SessionData]].model_validate(
                (await client.get("/api/auth/sessions")).json()
            ).data

        assert first.reused is False  # 第一次是正经开会话
        assert second.reused is True
        assert second.token == first.token  # 令牌原样发回，Cookie 不用换
        assert second.token_hash == first.token_hash
        assert len(listed) == 1  # 设备列表里没多出一条

    async def test_login_with_previous_token_in_the_body(self) -> None:
        """脚本客户端（手里没有 Cookie）：把旧令牌放请求体里，一样能复用。"""
        async with client_for(app_with()) as client:
            first = ApiResponse[LoginData].model_validate(
                (await client.post(LOGIN_PATH, json=ADMIN)).json()
            ).data
            client.cookies.clear()  # 模拟脚本：不自动带 Cookie，只把令牌放进请求体
            second = ApiResponse[LoginData].model_validate(
                (await client.post(LOGIN_PATH, json={**ADMIN, "previous_token": first.token})).json()
            ).data

        assert second.reused is True
        assert second.token == first.token
        assert second.token_hash == first.token_hash

    async def test_login_with_a_useless_previous_token_still_works(self) -> None:
        """旧令牌认不出来 → 不复用，照常发一个新的（**复用失败不该挡住登录**）。"""
        async with client_for(app_with()) as client:
            data = ApiResponse[LoginData].model_validate(
                (
                    await client.post(
                        LOGIN_PATH, json={**ADMIN, "previous_token": "nacho_乱编的"}
                    )
                ).json()
            ).data
            listed = ApiResponse[list[SessionData]].model_validate(
                (await client.get("/api/auth/sessions")).json()
            ).data

        assert data.reused is False
        assert data.token.startswith("nacho_")
        assert len(listed) == 1  # 这条是这次新开的

    async def test_reuse_is_keyed_on_the_token_not_the_device(self) -> None:
        """复用认的是**令牌**，不是设备：同一台设备不带旧令牌，照样开一条新会话。

        两次都用同一个 client，并且显式带上同样的 X-Device-Name —— 于是"设备身份"完全一致
        （同一个 ip、同一个 User-Agent、同一个自报设备名），差别只在于第二次有没有旧令牌。

        （设备名用 ASCII：HTTP 头里放不下中文，httpx 会直接拒绝这个请求。要自报中文名得先
        百分号编码，见 client_info_of 那边。）
        """
        async with client_for(app_with()) as client:
            headers = {"X-Device-Name": "My-MacBook"}
            first = ApiResponse[LoginData].model_validate(
                (await client.post(LOGIN_PATH, json=ADMIN, headers=headers)).json()
            ).data
            # 模拟"令牌丢了/换了一份、但设备没变"：清掉 Cookie，请求体里也不传旧令牌
            client.cookies.clear()
            second = ApiResponse[LoginData].model_validate(
                (await client.post(LOGIN_PATH, json=ADMIN, headers=headers)).json()
            ).data
            listed = ApiResponse[list[SessionData]].model_validate(
                (await client.get("/api/auth/sessions")).json()
            ).data

        assert first.reused is False
        assert second.reused is False  # 设备没变也没用：复用不看设备
        assert second.token != first.token
        assert len(listed) == 2  # 于是多出一条设备记录
        # 两条记录的设备名一模一样 —— 同设备并不构成复用条件
        assert {row.device_name for row in listed} == {"My-MacBook"}

    async def test_previous_token_in_the_body_wins_over_the_cookie(self) -> None:
        """请求体里的 previous_token **优先**于 Cookie：它非空时根本不去看 Cookie。

        注意是"优先"不是"回退"——body 里那个用不了，也不会退回去用 Cookie 里那个好的。
        """
        async with client_for(app_with()) as client:
            first = ApiResponse[LoginData].model_validate(
                (await client.post(LOGIN_PATH, json=ADMIN)).json()
            ).data
            # 此刻 Cookie 里有一份**有效**的令牌（上一次登录留下的），但请求体里塞了个无效的：
            # body 优先 → 不复用，又开了一条
            second = ApiResponse[LoginData].model_validate(
                (
                    await client.post(
                        LOGIN_PATH, json={**ADMIN, "previous_token": "nacho_乱编的"}
                    )
                ).json()
            ).data
            listed = ApiResponse[list[SessionData]].model_validate(
                (await client.get("/api/auth/sessions")).json()
            ).data

        assert second.reused is False
        assert second.token != first.token
        assert len(listed) == 2  # Cookie 里那份没被用上，所以确实多了一条

    async def test_cookie_of_another_user_is_not_reused(self) -> None:
        """Cookie 里是**别人**的令牌 → 不复用：不能因为"知道现在是谁在登录"就把别人的续了。"""
        async with client_for(app_with()) as client:
            robot = ApiResponse[LoginData].model_validate(
                (await client.post(LOGIN_PATH, json=ROBOT)).json()
            ).data
            # Cookie 里还是 robot 那份（不清掉），现在改用 admin 登录
            admin = ApiResponse[LoginData].model_validate(
                (await client.post(LOGIN_PATH, json=ADMIN)).json()
            ).data
            # 再把 Cookie 清掉，用 robot 自己的令牌查它那份列表：确认没被这次 admin 登录动过
            client.cookies.clear()
            robot_rows = ApiResponse[list[SessionData]].model_validate(
                (
                    await client.get(
                        "/api/auth/sessions",
                        headers={"Authorization": f"Bearer {robot.token}"},
                    )
                ).json()
            ).data

        assert admin.reused is False
        assert admin.token != robot.token
        assert len(robot_rows) == 1  # robot 那条会话原封不动
        assert robot_rows[0].token_hash == robot.token_hash

    async def test_forged_cookie_is_ignored(self) -> None:
        """Cookie 是伪造的（认不出来）→ 不复用，照常发新令牌。"""
        async with client_for(app_with()) as client:
            data = ApiResponse[LoginData].model_validate(
                (
                    await client.post(
                        LOGIN_PATH,
                        json=ADMIN,
                        headers={"Cookie": f"{SESSION_COOKIE}=nacho_forged"},
                    )
                ).json()
            ).data

        assert data.reused is False
        assert data.token.startswith("nacho_")

    async def test_revoked_session_token_is_401(self) -> None:
        """令牌**有状态**：会话一吊销，手里那个旧令牌立刻就不好使了。"""
        async with client_for(app_with()) as client:
            login = ApiResponse[LoginData].model_validate(
                (await client.post(LOGIN_PATH, json=ADMIN)).json()
            )
            data = login.data
            headers = {"Authorization": f"Bearer {data.token}"}
            assert (await client.get("/api/auth/me", headers=headers)).status_code == 200

            revoked = await client.delete(
                f"/api/auth/sessions/{data.token_hash}", headers=headers
            )
            assert revoked.status_code == 200, revoked.text
            response = await client.get("/api/auth/me", headers=headers)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == ErrorCode.UNAUTHORIZED


# ----------------------------------------------------------------------- 选项装配
class TestOptions:
    """选项与配置怎么接进来：路由前缀、令牌有效期、多余键忽略。"""

    def test_from_mapping_picks_known_keys_only(self) -> None:
        options = ApiOptions.from_mapping({"prefix": "/v1", "token_ttl": 60.0, "nope": 1})
        assert (options.prefix, options.token_ttl) == ("/v1", 60.0)
        assert not hasattr(options, "nope")

    def test_settings_api_feeds_options(self, tmp_path: Path) -> None:
        """配置系统 -> 选项：``Settings.api.model_dump()`` 能直接喂进来。"""
        path = tmp_path / "config.toml"
        path.write_text('[api]\nprefix = "/v2"\ntoken_ttl = 120.0\n', encoding="utf-8")
        options = ApiOptions.from_mapping(Settings.load(path).api.model_dump())
        assert (options.prefix, options.token_ttl) == ("/v2", 120.0)

    def test_defaults_match_config_defaults(self) -> None:
        """接口层选项的默认值 = 配置系统的默认值（改一边忘了另一边，这里会红）。

        路径字段（``avatar_dir``）例外：配置侧写的是「相对项目根目录」、由配置系统解析成
        绝对路径，接口层侧的默认值是「相对当前工作目录」的裸路径 —— 两边本来就是两种口径，
        真跑起来用的也是配置侧解析出来的那个（见 :func:`nacho.bootstrap.run`）。
        """
        from_config = ApiOptions.from_mapping(Settings().api.model_dump())
        from_code = ApiOptions()
        assert replace(from_config, avatar_dir=from_code.avatar_dir) == from_code

    def test_bad_prefix_is_a_config_error(self, tmp_path: Path) -> None:
        """前缀不以 / 开头：配置阶段就报错，不让应用带着坏路由起来。"""
        path = tmp_path / "config.toml"
        path.write_text('[api]\nprefix = "api"\n', encoding="utf-8")
        with pytest.raises(ConfigError):
            Settings.load(path)

    async def test_prefix_decides_where_routes_live(self) -> None:
        app = app_with(prefix="/v1")
        async with client_for(app) as client:
            assert (await client.post("/v1/auth/login", json=ADMIN)).status_code == 200
            assert (await client.post(LOGIN_PATH, json=ADMIN)).status_code == 404


# ----------------------------------------------------------------------- 默认实现
class TestSecurity:
    """密码哈希这份默认实现（不引第三方库）。

    令牌那部分原来是「HMAC 签名、可自证」的，现在是**有状态的不透明令牌**，测试在
    :mod:`tests.test_session` 里（那里连缓存与滑动续期一起测）。
    """

    def test_password_hash_roundtrip(self) -> None:
        hasher = Pbkdf2PasswordHasher(iterations=TEST_ITERATIONS)
        hashed = hasher.hash("nacho-admin")
        assert "nacho-admin" not in hashed  # 明文不进哈希串
        assert hasher.verify("nacho-admin", hashed)
        assert not hasher.verify("nacho-other", hashed)

    def test_broken_hash_verifies_false_instead_of_raising(self) -> None:
        hasher = Pbkdf2PasswordHasher(iterations=TEST_ITERATIONS)
        assert not hasher.verify("x", "not-a-hash")
        assert not hasher.verify("x", "other$1$c2FsdA==$ZGlnZXN0")  # 算法名不认

    async def test_profile_of_drops_password_hash(self) -> None:
        store = await memory_user_store(Pbkdf2PasswordHasher(iterations=TEST_ITERATIONS))
        record = await store.get_by_account("admin")
        assert record is not None
        profile = profile_of(record).model_dump()
        assert "password_hash" not in profile
        assert profile["account"] == "admin"

    async def test_service_raises_invalid_credentials_directly(self) -> None:
        """服务层不认识 HTTP：失败抛的是异常，状态码在异常里。"""
        service = AuthService(
            await memory_user_store(Pbkdf2PasswordHasher(iterations=TEST_ITERATIONS)),
            sessions=SessionService(await memory_session_store()),
        )
        with pytest.raises(InvalidCredentialsError):
            await service.login(
                Credentials(account="admin", password="wrong-password"),
                client=ClientInfo(),
            )

    async def test_service_register_raises_account_already_exists(self) -> None:
        """服务层同样只抛异常：重复注册是 409 那个，不是数据库的冲突错误。"""
        service = AuthService(
            await memory_user_store(Pbkdf2PasswordHasher(iterations=TEST_ITERATIONS)),
            sessions=SessionService(await memory_session_store()),
        )
        profile = await service.register(
            account="newbie", password="nacho-newbie", nickname="新来的"
        )
        assert profile.account == "newbie"
        with pytest.raises(AccountAlreadyExistsError):
            await service.register(
                account="newbie", password="nacho-newbie", nickname="新来的"
            )


# --------------------------------------------------------------------- 日志接入点
def attach_file_outlet(
    core: LogCore, directory: Path, *, prefix: str = "nacho"
) -> LocalFileLogProcessor:
    """挂一份文件出口（整进程一份、按天分片）；返回它，好去读落下来的片。"""
    outlet = LocalFileLogProcessor(
        directory, prefix=prefix, name="file", buffer_size=1, flush_interval=0
    )
    core.attach(outlet)
    return outlet


def shard_records(outlet: LocalFileLogProcessor) -> list[dict[str, object]]:
    """读某个文件出口**所有片**里的记录（一份日志可能不止一个片文件）。"""
    rows: list[dict[str, object]] = []
    for path in outlet.shards():
        rows.extend(records_of(path))
    return rows


class TestLogging:
    """日志接入：整进程一份文件出口，业务日志与访问日志都进那一份，靠 logger_name 分来源。"""

    async def test_api_and_access_logs_land_in_the_file_outlet(
        self, tmp_path: Path, core: LogCore
    ) -> None:
        outlet = attach_file_outlet(core, tmp_path)
        await asyncio.sleep(0.1)  # 等分发器把刚挂上的文件出口拉起来

        async with client_for(app_with()) as client:
            ok = await client.post(LOGIN_PATH, json=ADMIN)
            bad = await client.post(LOGIN_PATH, json={"account": "admin", "password": "wrong-password"})
        assert (ok.status_code, bad.status_code) == (200, 401)

        await drain(core)
        records = shard_records(outlet)
        messages: list[str] = [str(record["message"]) for record in records]
        names: set[str] = {str(record["logger_name"]) for record in records}
        assert "登录成功" in messages  # 业务日志（api）
        assert "登录失败" in messages  # 失败也留痕
        assert messages.count("请求完成") == 2  # 访问日志：两条请求各一条
        assert any(name.endswith(ACCESS_LOGGER_NAME) for name in names)
        assert outlet.current_shard is not None  # 落点就是当天那片

    async def test_access_log_can_be_turned_off(self, tmp_path: Path, core: LogCore) -> None:
        """``access_log = false``：访问日志一条不记，业务日志照旧。"""
        outlet = attach_file_outlet(core, tmp_path)
        await asyncio.sleep(0.1)  # 同上：等文件出口被拉起来
        async with client_for(
            create_app(
                replace(ApiOptions(prefix="/api"), access_log=False),
                hasher=_TEST_HASHER,
            )
        ) as client:
            assert (await client.post(LOGIN_PATH, json=ADMIN)).status_code == 200

        await drain(core)
        messages: list[str] = [str(record["message"]) for record in shard_records(outlet)]
        assert "登录成功" in messages
        assert "请求完成" not in messages

    async def test_access_log_carries_owner(self, tmp_path: Path, core: LogCore) -> None:
        """访问日志带**所有者**：认得出登录用户的记在他名下，没归属的是空串（公共）。

        归属由鉴权依赖认出用户后挂到 ``request.state``，中间件读它填 ``owner_id``；登录接口
        本身不走鉴权，所以它那一行没有归属。
        """
        outlet = attach_file_outlet(core, tmp_path)
        await asyncio.sleep(0.1)  # 同上：等文件出口被拉起来

        async with client_for(app_with()) as client:
            await client.post(LOGIN_PATH, json=ADMIN)  # 这一步还没有身份 -> 公共
            await client.get("/api/auth/me")  # 靠登录时发的 Cookie 认证 -> u-admin

        await drain(core)
        access = [row for row in shard_records(outlet) if str(row["message"]) == "请求完成"]
        assert [row["owner_id"] for row in access] == ["", "u-admin"]

    async def test_trace_id_ties_request_and_logs_together(self, tmp_path: Path, core: LogCore) -> None:
        """请求自带 X-Trace-Id 时沿用：日志里的编号与响应头、响应体一致。"""
        outlet = attach_file_outlet(core, tmp_path)
        await asyncio.sleep(0.1)  # 同上：等文件出口被拉起来
        async with client_for(app_with()) as client:
            response = await client.post(LOGIN_PATH, json=ADMIN, headers={"X-Trace-Id": "trace-1"})

        assert response.headers["X-Trace-Id"] == "trace-1"
        assert response.json()["trace_id"] == "trace-1"
        await drain(core)
        traced: list[dict[str, object]] = [
            record
            for record in shard_records(outlet)
            if str(record["message"]) in ("请求完成", "登录成功")
        ]
        assert traced
        assert all(
            record.get("extra", {}).get("trace_id") == "trace-1"  # type: ignore[union-attr]
            for record in traced
        )


# ----------------------------------------------------------------------- 日志检索接口
async def memory_log_processor() -> DatabaseLogProcessor:
    """挂在内存 sqlite 上的库出口（``logs`` 表已建好），供日志检索接口用。"""
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    _MEMORY_ENGINES.append(engine)
    store = SqlLogStore(engine)
    await store.ensure_schema()
    return DatabaseLogProcessor(store, buffer_size=5, flush_interval=0)


async def search_logs(
    client: httpx.AsyncClient, headers: dict[str, str], **params: str | int
) -> list[LogData]:
    """调一次 ``GET /api/logs`` 并返回 ``data.items`` 里的日志（断言都看它）。"""
    page = await search_log_page(client, headers, **params)
    return page.items


async def search_log_page(
    client: httpx.AsyncClient, headers: dict[str, str], **params: str | int
) -> LogPage:
    """调一次 ``GET /api/logs`` 并返回整页（``items`` + ``total``，翻页断言看它）。"""
    response = await client.get("/api/logs", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return ApiResponse[LogPage].model_validate(response.json()).data


async def token_of(client: httpx.AsyncClient, account: dict[str, str]) -> str:
    """登录换一个令牌（这几条用例都要先登录）。"""
    response = await client.post(LOGIN_PATH, json=account)
    return ApiResponse[LoginData].model_validate(response.json()).data.token


class RecordingProcessor(BaseLogProcessor):
    """把收到的日志原样留在内存里（用来验证「不写 processors 时只查落库那份」）。"""

    name: str = "recording"

    def __init__(self) -> None:
        super().__init__(buffer_size=5, flush_interval=0)
        self.received: list[LogRecord] = []

    @override
    async def write(self, records: list[LogRecord]) -> None:
        self.received.extend(records)

    @override
    async def search(self, **kwargs: object) -> LogSearchResult:
        return LogSearchResult(records=list(self.received), total=len(self.received))


class TestLogSearch:
    """``GET /api/logs``：查询条件原样透传给日志系统的 search（落库那份就是一条 SQL）。"""

    async def test_requires_login(self) -> None:
        """没登录一律 401：不往外说有什么日志。"""
        async with client_for(app_with()) as client:
            assert (await client.get("/api/logs")).status_code == 401

    async def test_filters_pass_through(self, core: LogCore) -> None:
        """条件透传：归属 / 关键字 / 出口各自把目标那几条挑出来。"""
        core.attach(await memory_log_processor())
        log = get_logger(API_LOGGER_NAME)
        log.info("管理员干的活", owner_id="u-admin")
        log.warning("机器人干的活", owner_id="u-robot")
        log.info("框架自己的活")  # 不填归属 = 公共所有者
        await drain(core)

        async with client_for(app_with()) as client:
            headers = {"Authorization": f"Bearer {await token_of(client, ADMIN)}"}

            robots = await search_logs(client, headers, owner_id="u-robot")
            assert [(row.message, row.owner_id, row.level) for row in robots] == [
                ("机器人干的活", "u-robot", "WARNING")
            ]

            found = await search_logs(client, headers, query="机器人")
            assert [row.message for row in found] == ["机器人干的活"]

            # 归属给空串 = 只看公共那份（框架自身、没归属的请求都算公共）
            public = await search_logs(client, headers, owner_id="")
            assert "框架自己的活" in [row.message for row in public]
            assert all(row.owner_id == "" for row in public)

    async def test_reports_total_for_paging(self, core: LogCore) -> None:
        """响应带命中总数：`limit` / `offset` 只决定本页 items，total 始终是命中总数。"""
        core.attach(await memory_log_processor())
        log = get_logger(API_LOGGER_NAME)
        for index in range(5):
            log.info(f"第 {index} 条", owner_id="u-pager")
        await drain(core)

        async with client_for(app_with()) as client:
            headers = {"Authorization": f"Bearer {await token_of(client, ADMIN)}"}

            first = await search_log_page(client, headers, owner_id="u-pager", limit=2, offset=0)
            assert first.total == 5  # 总数不受本页大小限制
            assert len(first.items) == 2
            # 本页按**自增序号**倒序，序号也随响应回来（5 条日志 → 最新的两条是 5、4）
            assert [row.seq for row in first.items] == [5, 4]

            second = await search_log_page(client, headers, owner_id="u-pager", limit=2, offset=2)
            assert second.total == 5
            assert len(second.items) == 2

            last = await search_log_page(client, headers, owner_id="u-pager", limit=2, offset=4)
            assert last.total == 5
            assert len(last.items) == 1  # 最后一页不满

    async def test_default_queries_only_the_database_outlet(self, core: LogCore) -> None:
        """不写 ``processors`` 时只查落库那份（SQL）；显式指定才查别的出口。"""
        recording = RecordingProcessor()
        # 直接塞一条「只有这个出口有」的日志：要测的是「查谁」，路由那套不参与
        recording.received.append(LogRecord(message="只有内存出口有这条", owner_id="u-admin"))
        core.attach(recording)
        core.attach(await memory_log_processor())
        log = get_logger(API_LOGGER_NAME)
        log.info("落库那份有这条", owner_id="u-admin")
        await drain(core)

        async with client_for(app_with()) as client:
            headers = {"Authorization": f"Bearer {await token_of(client, ADMIN)}"}

            default_rows = [row.message for row in await search_logs(client, headers)]
            assert "落库那份有这条" in default_rows  # 默认查的就是落库那份
            assert "只有内存出口有这条" not in default_rows  # 别的出口那份不在默认结果里

            chosen = [row.message for row in await search_logs(client, headers, processors="recording")]
            assert "只有内存出口有这条" in chosen  # 显式指定出口就查它
            assert "落库那份有这条" in chosen

    async def test_source_file_reads_the_file_outlets(self, core: LogCore, tmp_path: Path) -> None:
        """``source=file`` 按**类别**选出口：文件出口叫什么名字都认，不会因为名字对不上查空。

        回归：界面上的「本机文件」原来发的是写死的 ``processors=local``，而装配层给文件出口
        起名叫 ``file``；日志系统对不认识的出口名**直接忽略**，于是表现成「一条都没有」。
        这里故意把出口起成别的名字（``nacho.file``），证明认的是**类型**、不是名字。
        """
        core.attach(await memory_log_processor())
        outlet = LocalFileLogProcessor(
            tmp_path, prefix="nacho", name="nacho.file", buffer_size=1, flush_interval=0
        )
        core.attach(outlet)
        # 直接起它（不等分发器那一轮）：这样落点当场就定得下来，好往当前那片塞一条标记记录
        await outlet.start()
        assert outlet.current_shard is not None
        only_in_file = LogRecord(message="只有文件里有这条", owner_id="u-fileonly")
        outlet.current_shard.write_text(
            json.dumps(only_in_file.to_dict(), ensure_ascii=False) + "\n", encoding="utf-8"
        )

        async with client_for(app_with()) as client:
            headers = {"Authorization": f"Bearer {await token_of(client, ADMIN)}"}
            scope = {"owner_id": "u-fileonly"}  # 只看这条，别把登录那几条也捞进来

            assert [
                row.message
                for row in await search_logs(client, headers, source="file", **scope)
            ] == ["只有文件里有这条"]
            # 落库那份压根没见过它：同样的条件换 source=database 就是空 —— 两路确实是两路
            assert await search_logs(client, headers, source="database", **scope) == []
            # all = 不限出口，两路一起查
            assert [
                row.message
                for row in await search_logs(client, headers, source="all", **scope)
            ] == ["只有文件里有这条"]

    async def test_source_and_outlet_names_are_validated(self, core: LogCore) -> None:
        """来源写错、出口名写错、两个来源参数一起写：一律 422，别静默当成「没有日志」。"""
        core.attach(await memory_log_processor())
        await asyncio.sleep(0.1)

        async with client_for(app_with()) as client:
            headers = {"Authorization": f"Bearer {await token_of(client, ADMIN)}"}

            bad_source = await client.get("/api/logs", headers=headers, params={"source": "本地"})
            assert bad_source.status_code == 422
            assert "来源要" in bad_source.json()["error"]["message"]

            bad_outlet = await client.get(
                "/api/logs", headers=headers, params={"processors": "local"}
            )
            assert bad_outlet.status_code == 422
            message = bad_outlet.json()["error"]["message"]
            assert "local" in message  # 写错的是哪个
            assert DatabaseLogProcessor.name in message  # 现在挂着的有哪些

            both = await client.get(
                "/api/logs",
                headers=headers,
                params={"source": "file", "processors": DatabaseLogProcessor.name},
            )
            assert both.status_code == 422
            assert "二选一" in both.json()["error"]["message"]

    async def test_source_needs_admin_and_an_existing_outlet(self, core: LogCore) -> None:
        """来源只有管理员能选；选了但那一类出口没挂 -> 503 说清楚（不是「一条都没有」）。"""
        core.attach(await memory_log_processor())
        await asyncio.sleep(0.1)

        async with client_for(app_with()) as client:
            robot = {"Authorization": f"Bearer {await token_of(client, ROBOT)}"}
            forbidden = await client.get("/api/logs", headers=robot, params={"source": "file"})
            assert forbidden.status_code == 403

            admin = {"Authorization": f"Bearer {await token_of(client, ADMIN)}"}
            # 这一趟压根没挂文件出口（`core` 只有库那份）：说清楚「配哪儿才有」
            missing = await client.get("/api/logs", headers=admin, params={"source": "file"})
            assert missing.status_code == 503
            assert "logging.file" in missing.json()["error"]["message"]

    async def test_missing_database_outlet_is_503(self, core: LogCore) -> None:
        """库出口没开时回 503 并说清楚：不然只会静默返回空，比报错难查。"""
        assert core.get_processor(DatabaseLogProcessor.name) is None  # 这个核心里没挂库出口
        async with client_for(app_with()) as client:
            headers = {"Authorization": f"Bearer {await token_of(client, ADMIN)}"}
            response = await client.get("/api/logs", headers=headers)

        assert response.status_code == 503
        assert "落库" in response.json()["error"]["message"]

    async def test_console_hint_is_not_a_log(self, core: LogCore) -> None:
        """控制台那条「本出口不支持检索」的提示记录不是日志，不会混进结果。"""
        core.attach(ConsoleLogProcessor(stream=io.StringIO()))
        await asyncio.sleep(0.1)  # 等分发器把它拉起来

        async with client_for(app_with()) as client:
            headers = {"Authorization": f"Bearer {await token_of(client, ADMIN)}"}
            assert await search_logs(client, headers, processors="console") == []

    async def test_normal_user_only_sees_own(self, core: LogCore) -> None:
        """普通用户只看得到自己名下的；显式要别人的归属 -> 403。"""
        core.attach(await memory_log_processor())
        log = get_logger(API_LOGGER_NAME)
        log.info("管理员干的活", owner_id="u-admin")
        log.info("机器人干的活", owner_id="u-robot")
        await drain(core)

        async with client_for(app_with()) as client:
            headers = {"Authorization": f"Bearer {await token_of(client, ROBOT)}"}

            rows = await search_logs(client, headers)
            # 登录那几步（登录成功 / 会话已开启）本身就是这个用户自己的操作，算他名下的
            assert rows
            assert all(row.owner_id == "u-robot" for row in rows)
            assert "机器人干的活" in [row.message for row in rows]
            assert "管理员干的活" not in [row.message for row in rows]
            assert rows[0].owner_id == "u-robot"

            forbidden = await client.get(
                "/api/logs", headers=headers, params={"owner_id": "u-admin"}
            )
            assert forbidden.status_code == 403

    async def test_normal_user_cannot_choose_the_source(self, core: LogCore) -> None:
        """来源只由管理员定：非管理员指定别的出口 -> 403；写成默认那份不报错。"""
        core.attach(await memory_log_processor())

        async with client_for(app_with()) as client:
            headers = {"Authorization": f"Bearer {await token_of(client, ROBOT)}"}

            forbidden = await client.get(
                "/api/logs", headers=headers, params={"processors": "recording"}
            )
            assert forbidden.status_code == 403

            # 写成默认那份 = 跟不写一样，没必要为它挑刺
            default_named = await client.get(
                "/api/logs",
                headers=headers,
                params={"processors": DatabaseLogProcessor.name},
            )
            assert default_named.status_code == 200

    async def test_bad_params_are_422(self) -> None:
        """级别名 / 时间格式 / 越界的 limit 当场挡住：不然会在出口里被吞掉，只看到「一条都没有」。"""
        async with client_for(app_with()) as client:
            headers = {"Authorization": f"Bearer {await token_of(client, ADMIN)}"}
            bad_level = await client.get("/api/logs", headers=headers, params={"level": "LOUD"})
            bad_time = await client.get("/api/logs", headers=headers, params={"start": "不是时间"})
            bad_limit = await client.get("/api/logs", headers=headers, params={"limit": 0})

        assert bad_level.status_code == 422
        assert bad_level.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR
        assert bad_time.status_code == 422
        assert bad_time.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR
        assert bad_limit.status_code == 422  # 上限 / 下限由 Annotated 里的 Query 管
        assert bad_limit.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR


# --------------------------------------------------------------------------- 协议
class TestResponseModels:
    """数据协议本身：默认编号、成功 / 失败两头的形状。"""

    def test_api_response_defaults(self) -> None:
        response = ApiResponse[str](data="ok")
        assert response.model_dump() == {"success": True, "data": "ok", "trace_id": "-"}

    def test_login_request_keeps_password_secret(self) -> None:
        request = LoginRequest(account="admin", password="nacho-admin")
        assert "nacho-admin" not in str(request)
        assert request.password.get_secret_value() == "nacho-admin"

    def test_login_data_shape(self) -> None:
        data = LoginData(token="t", expires_in=60, user={"id": "u", "account": "a"})  # type: ignore[arg-type]
        assert data.token_type == "bearer"
        assert data.user.roles == []  # 没给角色就是空列表，不是 None
