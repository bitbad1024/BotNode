"""``nacho/api`` 的单元测试：登录流程、输入校验、错误出口、令牌与日志接入点。

请求不走真实网络：用 ``httpx.AsyncClient`` 挂 ``ASGITransport`` 直接打进 ASGI 应用，
于是请求与日志核心跑在**同一个事件循环**里（日志是同步入队的，换线程会丢），
「访问日志有没有落进文件」这类断言才站得住。

需要 ``fastapi`` / ``httpx``（``pip install "nacho[api]"`` + dev 依赖）；没装就整文件跳过。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import AsyncIterator

import pytest

pytest.importorskip("fastapi", reason="接口层要装 fastapi：pip install \"nacho[api]\"")
pytest.importorskip("httpx", reason="接口层测试用 httpx 发请求：pip install \"nacho[dev]\"")

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from config import ConfigError, Settings  # noqa: E402
from nacho.api import (  # noqa: E402
    ACCESS_LOGGER_NAME,
    API_LOGGER_NAME,
    ApiOptions,
    ApiResponse,
    AuthService,
    Credentials,
    ErrorCode,
    HmacTokenService,
    InMemoryUserStore,
    InvalidCredentialsError,
    LoginData,
    LoginRequest,
    Pbkdf2PasswordHasher,
    TokenClaims,
    TokenExpiredError,
    TokenInvalidError,
    attach_api_logging,
    create_app,
    profile_of,
)
from nacho.core.logger import LogCore, configure, manager  # noqa: E402

#: 演示账号（见 InMemoryUserStore.demo）
ADMIN = {"account": "admin", "password": "nacho-admin"}
LOGIN_PATH = "/api/auth/login"

#: 测试用的哈希迭代次数。默认 20 万次是**生产该有的值**（单次约 67ms，专门用来拖慢离线爆破），
#: 但这份钱不该让每个用例重付一遍 —— 这里降到 1000 次，爆破成本由 :class:`Pbkdf2PasswordHasher`
#: 自己的默认值负责，登录流程的断言与迭代次数无关（哈希串自带参数，验适用串里那份）。
TEST_ITERATIONS: int = 1_000

_TEST_HASHER = Pbkdf2PasswordHasher(iterations=TEST_ITERATIONS)
_CACHE: dict[str, InMemoryUserStore] = {}


def demo_store() -> InMemoryUserStore:
    """全模块共用一份演示存储，第一次用时才造。

    ``InMemoryUserStore.demo`` 要按三个账号各哈希一遍（20 万次迭代下约 210ms），
    而它只有查询接口、没有增删改，登录也不会改动记录，所以共用一份不会被前一个用例污染。
    """
    if "demo" not in _CACHE:
        _CACHE["demo"] = InMemoryUserStore.demo(_TEST_HASHER)
    return _CACHE["demo"]


def client_for(app: FastAPI) -> httpx.AsyncClient:
    """一个直连 ASGI 应用的异步客户端（不发真实网络请求）。"""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def app_with(**overrides: object) -> FastAPI:
    """建一个应用：默认前缀 /api、令牌 1800 秒、固定签名密钥（测试要可重现）。

    用户存储传 :func:`demo_store` 那一份共享的：否则每个用例重建应用都要把演示账号
    重新哈希一遍，单点计时就先看得出这里贵。
    """
    options = ApiOptions(prefix="/api", token_ttl=1800.0, secret="test-secret")
    return create_app(replace(options, **overrides), user_store=demo_store(), hasher=_TEST_HASHER)


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
            "id": "u-0001",
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

    async def test_tampered_token_is_401_invalid(self) -> None:
        async with client_for(app_with()) as client:
            token: str = (await client.post(LOGIN_PATH, json=ADMIN)).json()["data"]["token"]
            response = await client.get(
                "/api/auth/me", headers={"Authorization": f"Bearer {token[:-2]}xy"}
            )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == ErrorCode.TOKEN_INVALID

    async def test_expired_token_is_401_expired(self) -> None:
        """过期令牌：客户端拿 TOKEN_EXPIRED 决定去重新登录。"""
        expired: str = HmacTokenService("test-secret", ttl=-1.0).issue("u-0001")
        async with client_for(app_with()) as client:
            response = await client.get(
                "/api/auth/me", headers={"Authorization": f"Bearer {expired}"}
            )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == ErrorCode.TOKEN_EXPIRED


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
        assert ApiOptions.from_mapping(Settings().api.model_dump()) == ApiOptions()

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
    """密码哈希与令牌这两份默认实现（都不引第三方库）。"""

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

    def test_token_roundtrip_and_expiry(self) -> None:
        tokens = HmacTokenService("secret", ttl=60.0)
        claims: TokenClaims = tokens.parse(tokens.issue("u-0001"))
        assert claims.subject == "u-0001"
        assert 0 < claims.expires_in <= 60
        assert tokens.parse(tokens.issue("u-0002")).token_id  # 每次一个新编号

    def test_expired_token_raises(self) -> None:
        tokens = HmacTokenService("secret", ttl=-1.0)
        with pytest.raises(TokenExpiredError) as excinfo:
            tokens.parse(tokens.issue("u-0001"))
        assert excinfo.value.code == ErrorCode.TOKEN_EXPIRED

    def test_secret_mismatch_raises_invalid(self) -> None:
        """换一把密钥验签：签名对不上，报 TOKEN_INVALID（不是过期）。"""
        token = HmacTokenService("one", ttl=60.0).issue("u-0001")
        with pytest.raises(TokenInvalidError) as excinfo:
            HmacTokenService("two", ttl=60.0).parse(token)
        assert excinfo.value.code == ErrorCode.TOKEN_INVALID

    async def test_profile_of_drops_password_hash(self) -> None:
        store = InMemoryUserStore.demo(Pbkdf2PasswordHasher(iterations=TEST_ITERATIONS))
        record = await store.get_by_account("admin")
        assert record is not None
        profile = profile_of(record).model_dump()
        assert "password_hash" not in profile
        assert profile["account"] == "admin"

    async def test_service_raises_invalid_credentials_directly(self) -> None:
        """服务层不认识 HTTP：失败抛的是异常，状态码在异常里。"""
        service = AuthService(InMemoryUserStore.demo(Pbkdf2PasswordHasher(iterations=TEST_ITERATIONS)))
        with pytest.raises(InvalidCredentialsError):
            await service.login(Credentials(account="admin", password="wrong-password"))


# --------------------------------------------------------------------- 日志接入点
class TestLogging:
    """日志接入点：挂上文件出口之后，业务日志与访问日志都进同一个文件。"""

    async def test_api_and_access_logs_land_in_mounted_file(self, tmp_path: Path, core: LogCore) -> None:
        log_path: Path = tmp_path / "api.log"
        logger = attach_api_logging(log_path)
        assert logger.name.endswith(API_LOGGER_NAME)
        await asyncio.sleep(0.1)  # 等分发器把刚挂上的文件出口拉起来

        async with client_for(app_with()) as client:
            ok = await client.post(LOGIN_PATH, json=ADMIN)
            bad = await client.post(LOGIN_PATH, json={"account": "admin", "password": "wrong-password"})
        assert (ok.status_code, bad.status_code) == (200, 401)

        await drain(core)
        records = records_of(log_path)
        messages: list[str] = [str(record["message"]) for record in records]
        names: set[str] = {str(record["logger_name"]) for record in records}
        assert "登录成功" in messages  # 业务日志（api）
        assert "登录失败" in messages  # 失败也留痕
        assert messages.count("请求完成") == 2  # 访问日志：两条请求各一条
        assert any(name.endswith(ACCESS_LOGGER_NAME) for name in names)

    async def test_access_log_can_be_turned_off(self, tmp_path: Path, core: LogCore) -> None:
        """``access_log = false``：访问日志一条不记，业务日志照旧。"""
        log_path: Path = tmp_path / "api.log"
        attach_api_logging(log_path)
        await asyncio.sleep(0.1)  # 同上：等文件出口被拉起来
        async with client_for(
            create_app(
                replace(ApiOptions(prefix="/api", secret="s"), access_log=False),
                user_store=demo_store(),
                hasher=_TEST_HASHER,
            )
        ) as client:
            assert (await client.post(LOGIN_PATH, json=ADMIN)).status_code == 200

        await drain(core)
        messages: list[str] = [str(record["message"]) for record in records_of(log_path)]
        assert "登录成功" in messages
        assert "请求完成" not in messages

    async def test_trace_id_ties_request_and_logs_together(self, tmp_path: Path, core: LogCore) -> None:
        """请求自带 X-Trace-Id 时沿用：日志里的编号与响应头、响应体一致。"""
        log_path: Path = tmp_path / "api.log"
        attach_api_logging(log_path)
        await asyncio.sleep(0.1)  # 同上：等文件出口被拉起来
        async with client_for(app_with()) as client:
            response = await client.post(LOGIN_PATH, json=ADMIN, headers={"X-Trace-Id": "trace-1"})

        assert response.headers["X-Trace-Id"] == "trace-1"
        assert response.json()["trace_id"] == "trace-1"
        await drain(core)
        traced: list[dict[str, object]] = [
            record
            for record in records_of(log_path)
            if str(record["message"]) in ("请求完成", "登录成功")
        ]
        assert traced
        assert all(
            record.get("extra", {}).get("trace_id") == "trace-1"  # type: ignore[union-attr]
            for record in traced
        )


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
