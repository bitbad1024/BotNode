"""个人设置的测试：图片嗅探 / 本地目录存储 / 服务层规则 / 接口层（改昵称 + 头像）。

接口层那组走「真跑一遍」的口径（直连 ASGI、不发网络）：登录拿令牌 -> 改昵称 / 传头像 ->
再取回来。头像一律落到用例自己的 ``tmp_path``，别往仓库里写东西。
"""
from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="接口层测试要 fastapi")
pytest.importorskip("httpx", reason="接口层测试要 httpx")
pytest.importorskip("sqlmodel", reason="用户存储要 sqlmodel")
pytest.importorskip("aiosqlite", reason="内存数据库要 aiosqlite")

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from config import BASE_DIR, Settings
from botnode.api import (
    ApiOptions,
    ApiResponse,
    AvatarTooLargeError,
    AvatarTypeUnsupportedError,
    ErrorCode,
    FileAvatarStore,
    LoginData,
    Pbkdf2PasswordHasher,
    ProfileService,
    SqlUserStore,
    ValidationError,
    create_app,
)
from botnode.api.services.profile import ALLOWED_IMAGE_TYPES, sniff_image_type

#: 演示账号只剩 admin（见 botnode.api.services.user.demo.DEMO_USERS）；id 就是 ``u-<账号>``。
#: robot 由 :func:`login` 顺手注册（昵称带上，与原来的演示账号一致）
ADMIN = {"account": "admin", "password": "botnode-admin"}
ROBOT = {"account": "robot", "password": "botnode-robot", "nickname": "巡检机器人"}
LOGIN_PATH = "/api/auth/login"
REGISTER_PATH = "/api/auth/register"
PROFILE_PATH = "/api/profile"
AVATAR_PATH = "/api/profile/avatar"

#: 一个「文件头是对的」PNG：嗅探只看开头那几个字节，这里不解码图像（那要 Pillow，见 images.py）
PNG_BYTES: bytes = b"\x89PNG\r\n\x1a\n" + bytes(64)
#: 冒充图片的 HTML：就算把 Content-Type 报成 image/png，也过不了文件头那一关
HTML_BYTES: bytes = b"<html><script>alert(1)</script></html>"

#: 测试用的哈希迭代次数（默认 20 万次是生产该有的值，见 tests/test_api.py 里的说明）
TEST_ITERATIONS: int = 1_000

_TEST_HASHER = Pbkdf2PasswordHasher(iterations=TEST_ITERATIONS)


# --------------------------------------------------------------------------- 测试脚手架
@asynccontextmanager
async def client_for(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient]:
    """直连 ASGI 的客户端；顺带把 lifespan 跑起来（建表 + 种演示账号在里面）。"""
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


#: 本文件用到的内存引擎：用例结束统一 dispose
_MEMORY_ENGINES: list[AsyncEngine] = []


async def _memory_engine() -> AsyncEngine:
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite:///:memory:")
    _MEMORY_ENGINES.append(engine)
    return engine


@pytest.fixture(autouse=True)
async def _dispose_memory_engines() -> AsyncIterator[None]:
    yield
    while _MEMORY_ENGINES:
        await _MEMORY_ENGINES.pop().dispose()


def app_with(**overrides: object) -> FastAPI:
    """建一个应用：头像目录由用例传 ``tmp_path``、令牌 1800 秒（可重现）。"""
    options = ApiOptions(prefix="/api", token_ttl=1800.0)
    return create_app(replace(options, **overrides), hasher=_TEST_HASHER)


async def memory_user_store() -> SqlUserStore:
    """挂在内存 sqlite 上的用户存储（表已建好 + 演示账号已种）。"""
    store = SqlUserStore(await _memory_engine(), hasher=_TEST_HASHER)
    await store.ensure_schema()
    await store.seed_demo()
    return store


async def login(client: httpx.AsyncClient, account: dict[str, str]) -> str:
    """登录换一个令牌。

    演示账号现在只剩 ``admin``（见 ``botnode.api.services.user.demo``）：非 admin 的账号
    由这里顺手注册一个（注册要昵称，就用账号名顶上），用例不必各自准备。
    """
    if account["account"] != ADMIN["account"]:
        await client.post(REGISTER_PATH, json={"nickname": account["account"], **account})
    response = await client.post(LOGIN_PATH, json=account)
    return ApiResponse[LoginData].model_validate(response.json()).data.token


def auth(token: str) -> dict[str, str]:
    """带上令牌的请求头。"""
    return {"Authorization": f"Bearer {token}"}


def image_headers(token: str, mime: str = "image/png") -> dict[str, str]:
    """上传头像的请求头（``Content-Type`` 只是提示，类型按文件头认）。"""
    return {**auth(token), "Content-Type": mime}


# --------------------------------------------------------------------------- 图片嗅探
class TestSniffImageType:
    """类型按文件头认：客户端报什么不作数。"""

    def test_allowlist_is_these_four(self) -> None:
        assert set(ALLOWED_IMAGE_TYPES) == {
            "image/png",
            "image/jpeg",
            "image/webp",
            "image/gif",
        }

    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            (b"\x89PNG\r\n\x1a\n", "image/png"),
            (b"\xff\xd8\xff\xe0", "image/jpeg"),
            (b"GIF87a", "image/gif"),
            (b"GIF89a", "image/gif"),
            (b"RIFF\x00\x00\x00\x00WEBP", "image/webp"),
        ],
    )
    def test_recognizes_the_allowed_types(self, header: bytes, expected: str) -> None:
        assert sniff_image_type(header + bytes(32)) == expected

    def test_rejects_everything_else(self) -> None:
        """HTML / SVG / 空 / 随机字节一律认不出来 —— SVG 是 XML，能带脚本，不收。"""
        assert sniff_image_type(HTML_BYTES) is None
        assert sniff_image_type(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>") is None
        assert sniff_image_type(b"") is None
        assert sniff_image_type(bytes(64)) is None


# --------------------------------------------------------------------------- 本地目录存储
class TestFileAvatarStore:
    """默认实现：一个目录、一个用户一个文件。"""

    async def test_round_trip(self, tmp_path: Path) -> None:
        store = FileAvatarStore(tmp_path / "avatars")
        info = await store.save("u-admin", PNG_BYTES, mime="image/png")
        assert (info.user_id, info.mime, info.size) == ("u-admin", "image/png", len(PNG_BYTES))

        found = await store.load("u-admin")
        assert found is not None
        loaded, data = found
        assert data == PNG_BYTES
        assert loaded.mime == "image/png"
        assert await store.info("u-admin") == loaded

        assert await store.remove("u-admin") is True
        assert await store.info("u-admin") is None
        assert await store.load("u-admin") is None

    async def test_changing_type_leaves_exactly_one_file(self, tmp_path: Path) -> None:
        """PNG 换成 JPEG：扩展名跟着换，旧那份不能留着（否则会读到过期的图）。"""
        directory = tmp_path / "avatars"
        store = FileAvatarStore(directory)
        await store.save("u-admin", PNG_BYTES, mime="image/png")
        await store.save("u-admin", b"\xff\xd8\xff" + bytes(32), mime="image/jpeg")
        assert sorted(path.name for path in directory.iterdir()) == ["u-admin.jpg"]
        found = await store.load("u-admin")
        assert found is not None and found[0].mime == "image/jpeg"

    async def test_missing_directory_reads_as_no_avatar(self, tmp_path: Path) -> None:
        """目录还没建（没人传过头像）时不该报错，只当没有头像。"""
        store = FileAvatarStore(tmp_path / "not-created-yet")
        assert await store.info("u-admin") is None
        assert await store.load("u-admin") is None
        assert await store.remove("u-admin") is False

    @pytest.mark.parametrize(
        "user_id", ["../evil", "a/b", ".", "..", ".hidden", "", "x" * 65]
    )
    async def test_rejects_ids_that_cannot_be_filenames(
        self, tmp_path: Path, user_id: str
    ) -> None:
        """能跳出目录 / 会被当成临时文件的名字一律拒绝（接口层还会先挡一道，见 router）。"""
        store = FileAvatarStore(tmp_path / "avatars")
        with pytest.raises(ValueError):
            await store.save(user_id, PNG_BYTES, mime="image/png")
        with pytest.raises(ValueError):
            await store.info(user_id)


# --------------------------------------------------------------------------- 服务层
class TestProfileService:
    """规则（空 / 大小 / 类型）只在服务层判一遍。"""

    async def test_set_nickname_lands_in_the_user_store(self, tmp_path: Path) -> None:
        users = await memory_user_store()
        service = ProfileService(users, FileAvatarStore(tmp_path / "avatars"))
        view = await service.set_nickname("u-admin", "改过的名字")
        assert view is not None
        assert view.profile.nickname == "改过的名字"

        stored = await users.get_by_id("u-admin")
        assert stored is not None
        assert stored.nickname == "改过的名字"

    async def test_unknown_user_is_none(self, tmp_path: Path) -> None:
        """不只是「读」返回 None：写操作也得让调用方能报 404。"""
        service = ProfileService(await memory_user_store(), FileAvatarStore(tmp_path / "a"))
        assert await service.get("u-nobody") is None
        assert await service.set_nickname("u-nobody", "谁") is None
        assert await service.put_avatar("u-nobody", PNG_BYTES) is None
        assert await service.remove_avatar("u-nobody") is None

    async def test_put_avatar_enforces_size_and_type(self, tmp_path: Path) -> None:
        service = ProfileService(
            await memory_user_store(),
            FileAvatarStore(tmp_path / "avatars"),
            max_avatar_bytes=len(PNG_BYTES),
        )
        with pytest.raises(ValidationError):
            await service.put_avatar("u-admin", b"")
        with pytest.raises(AvatarTypeUnsupportedError):
            await service.put_avatar("u-admin", HTML_BYTES)
        with pytest.raises(AvatarTooLargeError):
            await service.put_avatar("u-admin", PNG_BYTES + bytes(1))

        view = await service.put_avatar("u-admin", PNG_BYTES)
        assert view is not None and view.avatar is not None
        assert view.avatar.mime == "image/png"
        assert view.has_avatar is True

    def test_error_codes_carry_the_right_status(self) -> None:
        """413 / 415 是给客户端做分支用的：太大换小图，类型不对换格式，别混成一个码。"""
        assert AvatarTooLargeError(limit=1024).status_code == 413
        assert AvatarTooLargeError(limit=1024).code == ErrorCode.AVATAR_TOO_LARGE
        assert AvatarTypeUnsupportedError().status_code == 415
        assert AvatarTypeUnsupportedError().code == ErrorCode.AVATAR_TYPE_UNSUPPORTED

    async def test_remove_avatar_is_idempotent(self, tmp_path: Path) -> None:
        """连点两下删除不该报错：结果一样是「没有头像」。"""
        service = ProfileService(await memory_user_store(), FileAvatarStore(tmp_path / "avatars"))
        await service.put_avatar("u-admin", PNG_BYTES)
        first = await service.remove_avatar("u-admin")
        second = await service.remove_avatar("u-admin")
        assert first is not None and first.has_avatar is False
        assert second is not None and second.has_avatar is False

    async def test_avatar_changes_are_logged(self, tmp_path: Path) -> None:
        """换头像 / 删头像各留一条审计：谁的操作看 ``owner_id``，同一次请求看 ``trace_id``。

        这两条是「哪个人什么时候换了头像」的唯一线索（访问日志只记了方法与路径、还不入库），
        所以连字段口径一起钉住：消息、归属、trace_id 都要对得上。
        """
        import asyncio

        from botnode.core.logger import (
            BaseLogProcessor,
            LogCore,
            LogRecord,
            LogSearchResult,
        )

        collected: list[LogRecord] = []

        class _Collector(BaseLogProcessor):
            name: str = "collector"

            def __init__(self) -> None:
                super().__init__(buffer_size=1, flush_interval=0)  # 逐条直写，不用等攒批

            async def write(self, records: list[LogRecord]) -> None:
                collected.extend(records)

            async def search(
                self,
                *,
                query: str | None = None,
                level: object = None,
                start: object = None,
                end: object = None,
                logger_name: str | None = None,
                owner_id: str | None = None,
                limit: int = 100,
                offset: int = 0,
            ) -> LogSearchResult:
                return LogSearchResult()

        core = LogCore(console=False, dispatch_timeout=0.01)
        await core.start()
        core.mount(_Collector())
        try:
            service = ProfileService(
                await memory_user_store(),
                FileAvatarStore(tmp_path / "avatars"),
                logger=core,
            )
            assert await service.put_avatar("u-admin", PNG_BYTES, trace_id="t-1") is not None
            assert await service.remove_avatar("u-admin", trace_id="t-2") is not None

            for _ in range(100):  # 分发是异步的：等它落到出口
                if len(collected) >= 2:
                    break
                await asyncio.sleep(0.01)
            assert [(r.message, r.owner_id, r.extra["trace_id"]) for r in collected] == [
                ("头像已更新", "u-admin", "t-1"),
                ("头像已删除", "u-admin", "t-2"),
            ]
            # 换头像那条连类型与字节数一起记：只写「更新了」的话，事后没法判断换的是什么
            assert (collected[0].extra["mime"], collected[0].extra["size"]) == (
                "image/png",
                len(PNG_BYTES),
            )
        finally:
            await core.stop()


# --------------------------------------------------------------------------- 选项 / 配置
def test_options_take_avatar_settings() -> None:
    """配置里的头像目录 / 上限能进到选项里（路径收字符串也收 Path）。"""
    options = ApiOptions.from_mapping(
        {"avatar_dir": "/srv/avatars", "avatar_max_bytes": 1234}
    )
    assert options.avatar_dir == Path("/srv/avatars")
    assert options.avatar_max_bytes == 1234
    assert ApiOptions().avatar_max_bytes > 0  # 默认有个像样的上限


def test_config_resolves_avatar_dir_against_project_root(tmp_path: Path) -> None:
    """配置里的相对路径按项目根目录解析（与日志 / 数据库那几个路径同一个口径）。"""
    path = tmp_path / "config.toml"
    path.write_text('[api]\navatar_dir = "var/avatars"\n', encoding="utf-8")
    assert Settings.load(path).api.avatar_dir == BASE_DIR / "var" / "avatars"


# --------------------------------------------------------------------------- 接口层
class TestProfileApi:
    """改昵称 + 头像（传 / 取 / 删），头像落用例自己的 tmp_path。"""

    async def test_new_account_has_no_avatar(self, tmp_path: Path) -> None:
        async with client_for(app_with(avatar_dir=tmp_path / "avatars")) as client:
            token = await login(client, ADMIN)
            response = await client.get(PROFILE_PATH, headers=auth(token))
        assert response.status_code == 200
        data = response.json()["data"]
        assert (data["account"], data["nickname"], data["has_avatar"]) == ("admin", "管理员", False)
        assert data["avatar_url"] == ""

    async def test_patch_nickname_shows_up_in_me(self, tmp_path: Path) -> None:
        """改昵称改的是同一份资料：``/profile`` 与 ``/auth/me`` 看到的都是新名字。"""
        async with client_for(app_with(avatar_dir=tmp_path / "avatars")) as client:
            token = await login(client, ADMIN)
            patched = await client.patch(
                PROFILE_PATH, headers=auth(token), json={"nickname": "老张"}
            )
            me = await client.get("/api/auth/me", headers=auth(token))
        assert patched.status_code == 200
        assert patched.json()["data"]["nickname"] == "老张"
        assert me.json()["data"]["nickname"] == "老张"

    async def test_patch_rejects_blank_nickname(self, tmp_path: Path) -> None:
        """昵称规则与注册共用一份：去空白后为空 -> 422。"""
        async with client_for(app_with(avatar_dir=tmp_path / "avatars")) as client:
            token = await login(client, ADMIN)
            response = await client.patch(
                PROFILE_PATH, headers=auth(token), json={"nickname": "   "}
            )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR

    async def test_upload_then_fetch_avatar(self, tmp_path: Path) -> None:
        """传上去能取回来；带上 ``If-None-Match`` 再取就是 304。"""
        async with client_for(app_with(avatar_dir=tmp_path / "avatars")) as client:
            token = await login(client, ADMIN)
            uploaded = await client.put(
                AVATAR_PATH, headers=image_headers(token), content=PNG_BYTES
            )
            fetched = await client.get(AVATAR_PATH, headers=auth(token))
            cached = await client.get(
                AVATAR_PATH,
                headers={**auth(token), "If-None-Match": fetched.headers["etag"]},
            )
        assert uploaded.status_code == 200
        data = uploaded.json()["data"]
        assert (data["has_avatar"], data["avatar_mime"], data["avatar_size"]) == (
            True,
            "image/png",
            len(PNG_BYTES),
        )
        assert data["avatar_url"].startswith("/api/profile/avatar/u-admin?v=")
        assert fetched.status_code == 200
        assert fetched.headers["content-type"] == "image/png"
        assert fetched.headers["x-content-type-options"] == "nosniff"
        assert fetched.content == PNG_BYTES
        assert cached.status_code == 304

    async def test_avatars_land_in_the_configured_directory(self, tmp_path: Path) -> None:
        """存放目录可配置：``[api] avatar_dir`` 一路传到默认存储，文件就落那儿。"""
        target = tmp_path / "my-avatars"
        async with client_for(app_with(avatar_dir=target)) as client:
            token = await login(client, ADMIN)
            await client.put(AVATAR_PATH, headers=image_headers(token), content=PNG_BYTES)
        assert [path.name for path in target.iterdir()] == ["u-admin.png"]

    async def test_upload_rejects_non_image(self, tmp_path: Path) -> None:
        """HTML 冒充 PNG：``Content-Type`` 报得再对也没用，类型看文件头。"""
        async with client_for(app_with(avatar_dir=tmp_path / "avatars")) as client:
            token = await login(client, ADMIN)
            response = await client.put(
                AVATAR_PATH, headers=image_headers(token), content=HTML_BYTES
            )
        assert response.status_code == 415
        assert response.json()["error"]["code"] == ErrorCode.AVATAR_TYPE_UNSUPPORTED

    async def test_upload_rejects_oversized_body(self, tmp_path: Path) -> None:
        async with client_for(
            app_with(avatar_dir=tmp_path / "avatars", avatar_max_bytes=len(PNG_BYTES))
        ) as client:
            token = await login(client, ADMIN)
            response = await client.put(
                AVATAR_PATH,
                headers=image_headers(token),
                content=PNG_BYTES + bytes(64),
            )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == ErrorCode.AVATAR_TOO_LARGE

    async def test_delete_avatar_then_it_is_gone(self, tmp_path: Path) -> None:
        async with client_for(app_with(avatar_dir=tmp_path / "avatars")) as client:
            token = await login(client, ADMIN)
            await client.put(AVATAR_PATH, headers=image_headers(token), content=PNG_BYTES)
            deleted = await client.delete(AVATAR_PATH, headers=auth(token))
            missing = await client.get(AVATAR_PATH, headers=auth(token))
        assert deleted.status_code == 200
        assert deleted.json()["data"]["has_avatar"] is False
        assert missing.status_code == 404

    async def test_requires_login(self, tmp_path: Path) -> None:
        async with client_for(app_with(avatar_dir=tmp_path / "avatars")) as client:
            read = await client.get(PROFILE_PATH)
            avatar = await client.get(AVATAR_PATH)
        assert read.status_code == 401
        assert avatar.status_code == 401

    async def test_any_logged_in_user_can_look_at_someone_avatar(self, tmp_path: Path) -> None:
        """取别人的头像是展示用途（列表页要画），登录了就能看。"""
        async with client_for(app_with(avatar_dir=tmp_path / "avatars")) as client:
            admin_token = await login(client, ADMIN)
            robot_token = await login(client, ROBOT)
            await client.put(
                AVATAR_PATH, headers=image_headers(admin_token), content=PNG_BYTES
            )
            seen = await client.get(f"{AVATAR_PATH}/u-admin", headers=auth(robot_token))
        assert seen.status_code == 200
        assert seen.content == PNG_BYTES

    @pytest.mark.parametrize("user_id", [".hidden", "bad id", "x" * 65])
    async def test_bad_user_id_is_404(self, tmp_path: Path, user_id: str) -> None:
        """不合形状的 id 在入口就 404：别让它走到存储层（那儿会抛成 500）。"""
        async with client_for(app_with(avatar_dir=tmp_path / "avatars")) as client:
            token = await login(client, ADMIN)
            response = await client.get(f"{AVATAR_PATH}/{user_id}", headers=auth(token))
        assert response.status_code == 404
