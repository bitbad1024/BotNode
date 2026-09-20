"""会话子系统的测试：设备信息解析、令牌（摘要即表主键）、滑动续期、双删吊销。

令牌是**有状态**的、且只有一种：明文给客户端，摘要既当表主键（列名 ``token_hash``）又当缓存键。
「会话还活着」这件事只记在缓存里，所以这里用一份真的内存缓存（``Cache`` 门面）来测；
落库那份（``auth_sessions``）用临时 sqlite 单独测。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

pytest.importorskip("sqlmodel", reason="落库会话要装 sqlmodel：pip install \"nacho[api]\"")
pytest.importorskip("aiosqlite", reason="sqlite 异步驱动要装 aiosqlite：pip install \"nacho[api]\"")

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402

from nacho.api import (  # noqa: E402
    ClientInfo,
    InMemorySessionStore,
    SessionService,
    SqlSessionStore,
    TokenHashCollisionError,
    describe_client,
)
from nacho.api.common.errors import UnauthorizedError  # noqa: E402
from nacho.api.services.session.tokens import (  # noqa: E402
    TOKEN_PREFIX,
    TokenIndex,
    hash_token,
)
from nacho.core.cache import Cache  # noqa: E402

#: 几个真实形状的 UA（截取了有辨识度的部分）
UA_CHROME_MAC: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
UA_SAFARI_IPHONE: str = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
UA_FIREFOX_WINDOWS: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0"
)

#: 测试用的两个有效期：短的那个别设太大，断言里要比较大小
ACCESS_TTL: float = 1800.0
REMEMBER_TTL: float = 2_592_000.0


@pytest.fixture
async def cache() -> AsyncIterator[Cache]:
    """一份跑起来的内存缓存（令牌映射就放它里面）。"""
    facade = Cache()
    await facade.start()
    try:
        yield facade
    finally:
        await facade.stop()


@pytest.fixture
def client() -> ClientInfo:
    """默认客户端：某台 Mac 上的 Chrome。"""
    return describe_client(ip="10.0.0.9", user_agent=UA_CHROME_MAC)


def make_service(
    cache: Cache,
    *,
    store: InMemorySessionStore | SqlSessionStore | None = None,
    access_ttl: float = ACCESS_TTL,
    remember_ttl: float = REMEMBER_TTL,
) -> SessionService:
    """建一个会话服务：内存存储 + 真缓存。"""
    return SessionService(
        store if store is not None else InMemorySessionStore(),
        index=TokenIndex(cache),
        access_ttl=access_ttl,
        remember_ttl=remember_ttl,
    )


def cache_key(token: str) -> str:
    """令牌对应的缓存键（测试里要看 TTL）。"""
    return "auth:token:" + hash_token(token)


# --------------------------------------------------------------------------- 设备信息
class TestClientInfo:
    """从 ip / 自报设备名 / UA 推出设备信息（纯函数，不碰 HTTP）。"""

    def test_desktop_chrome(self) -> None:
        info = describe_client(ip="1.2.3.4", user_agent=UA_CHROME_MAC)
        assert (info.device_type, info.browser, info.os) == ("desktop", "Chrome", "macOS")
        assert info.device_name == "Chrome · macOS"  # 没自报就按「浏览器 · 系统」拼
        assert info.ip == "1.2.3.4"

    def test_mobile_safari(self) -> None:
        info = describe_client(user_agent=UA_SAFARI_IPHONE)
        assert (info.device_type, info.browser, info.os) == ("mobile", "Safari", "iOS")

    def test_firefox_on_windows(self) -> None:
        info = describe_client(user_agent=UA_FIREFOX_WINDOWS)
        assert (info.device_type, info.browser, info.os) == ("desktop", "Firefox", "Windows")

    def test_self_reported_device_name_wins(self) -> None:
        """客户端自报的设备名优先——UA 里本来就没有「设备名称」这个东西。"""
        info = describe_client(user_agent=UA_SAFARI_IPHONE, device_name="  我的 iPhone  ")
        assert info.device_name == "我的 iPhone"  # 前后空白去掉
        assert info.browser == "Safari"  # 其余照样从 UA 推

    def test_unknown_agent_does_not_blow_up(self) -> None:
        info = describe_client(user_agent="")
        assert (info.device_type, info.browser, info.os) == ("unknown", "", "")
        assert info.device_name == "未知设备"


# --------------------------------------------------------------------------- 开会话 / 认令牌
class TestOpenAndAuthenticate:
    """登录开出来的会话：明文给客户端，摘要当表主键与缓存键。"""

    async def test_open_returns_token_and_records_device(
        self, cache: Cache, client: ClientInfo
    ) -> None:
        store = InMemorySessionStore()
        service = make_service(cache, store=store)
        issued = await service.open("u-0001", client=client)

        assert issued.token.startswith(TOKEN_PREFIX)
        assert issued.expires_in == int(ACCESS_TTL)  # 没勾「记住设备」用短的那档
        # 会话的 id **就是令牌摘要**（不是明文）：库里只留摘要
        assert issued.session.token_hash == hash_token(issued.token)
        assert issued.token not in issued.session.token_hash
        assert issued.session.device_name == "Chrome · macOS"
        assert issued.session.ip == "10.0.0.9"
        assert (await store.get(issued.session.token_hash)) == issued.session

        auth = await service.authenticate(issued.token)
        assert (auth.user_id, auth.token_hash) == ("u-0001", issued.session.token_hash)
        assert auth.expires_in == int(ACCESS_TTL)

    async def test_remember_uses_the_long_ttl(self, cache: Cache, client: ClientInfo) -> None:
        """勾「记住设备」只是把滑动有效期换成长的那档，没有第二种令牌。"""
        service = make_service(cache)
        issued = await service.open("u-0001", client=client, remember=True)
        assert issued.expires_in == int(REMEMBER_TTL)
        assert issued.session.remembered is True
        ttl = await cache.ttl(cache_key(issued.token))
        assert ttl is not None and ttl > ACCESS_TTL

    async def test_unknown_token_is_rejected(self, cache: Cache) -> None:
        service = make_service(cache)
        with pytest.raises(UnauthorizedError):
            await service.authenticate("nacho_随便编的")

    async def test_empty_token_is_rejected(self, cache: Cache) -> None:
        service = make_service(cache)
        with pytest.raises(UnauthorizedError):
            await service.authenticate("")

    async def test_session_row_alone_does_not_authenticate(
        self, cache: Cache, client: ClientInfo
    ) -> None:
        """库里那行是**设备记录**、不是凭据：缓存里没有这个键 = 会话不在了（闲置过期或吊销）。"""
        store = InMemorySessionStore()
        service = make_service(cache, store=store)
        issued = await service.open("u-0001", client=client)

        await cache.delete(cache_key(issued.token))  # 把它当"闲置到期被清掉"
        with pytest.raises(UnauthorizedError):
            await service.authenticate(issued.token)
        # 设备记录还在 —— 所以它还能在「登录设备」里看到、被吊销
        assert await store.get(issued.session.token_hash) is not None


# --------------------------------------------------------------------------- 复用旧令牌
class TestReuse:
    """登录复用旧令牌：认得出、且是同一个人 → 延长有效期，不新建会话。"""

    async def test_reuse_extends_ttl_and_keeps_the_token(
        self, cache: Cache, client: ClientInfo
    ) -> None:
        store = InMemorySessionStore()
        service = make_service(cache, store=store)
        issued = await service.open("u-0001", client=client)
        await cache.expire(cache_key(issued.token), 10.0)  # 人为拨到快过期

        again = await service.reuse(issued.token, user_id="u-0001")

        assert again is not None
        assert again.token == issued.token  # 还是那个令牌：客户端手里那份不用换
        assert again.session.token_hash == issued.session.token_hash
        assert again.expires_in == int(ACCESS_TTL)
        ttl = await cache.ttl(cache_key(issued.token))
        assert ttl is not None and ttl > 1000  # 有效期被拨回去了
        assert len(await service.list_for_user("u-0001")) == 1  # 没多出一条设备记录

    async def test_reuse_switches_to_the_remember_ttl(
        self, cache: Cache, client: ClientInfo
    ) -> None:
        """这次勾了「记住设备」→ 之后续期按长的那档（缓存的值里存的也是它）。"""
        store = InMemorySessionStore()
        service = make_service(cache, store=store)
        issued = await service.open("u-0001", client=client)  # 短档
        assert issued.session.remembered is False

        assert await service.reuse(issued.token, user_id="u-0001", remember=True) is not None

        # 再认一次：续期用的就是长档了（值里那个数字被一起改掉了）
        auth = await service.authenticate(issued.token)
        assert auth.expires_in == int(REMEMBER_TTL)
        # 设备记录里那一栏也跟着变，列表上不会还显示成「没记住」
        rows = await service.list_for_user("u-0001")
        assert len(rows) == 1
        assert rows[0].remembered is True

    async def test_reuse_refuses_another_users_token(
        self, cache: Cache, client: ClientInfo
    ) -> None:
        """拿别人的令牌来复用：不认——**不能因为"知道现在是谁在登录"就把别人的令牌续了**。"""
        service = make_service(cache)
        issued = await service.open("u-0001", client=client)
        assert await service.reuse(issued.token, user_id="u-0002") is None

    async def test_reuse_refuses_unknown_or_empty_token(self, cache: Cache) -> None:
        service = make_service(cache)
        assert await service.reuse("nacho_没有这条", user_id="u-0001") is None
        assert await service.reuse("", user_id="u-0001") is None

    async def test_reuse_refuses_when_the_record_is_gone(
        self, cache: Cache, client: ClientInfo
    ) -> None:
        """缓存还认得出、库里那行没了（正常到不了）→ 不复用，宁可另发一个。"""
        store = InMemorySessionStore()
        service = make_service(cache, store=store)
        issued = await service.open("u-0001", client=client)
        await store.remove(issued.session.token_hash)
        assert await service.reuse(issued.token, user_id="u-0001") is None


# --------------------------------------------------------------------------- 滑动续期
class TestSlidingExpiry:
    """有通讯就一直延期：每次认令牌都把缓存里的有效期往后拨。"""

    async def test_authenticate_extends_ttl(self, cache: Cache, client: ClientInfo) -> None:
        service = make_service(cache)
        issued = await service.open("u-0001", client=client)
        key = cache_key(issued.token)

        await cache.expire(key, 10.0)  # 人为拨到快过期
        assert (await cache.ttl(key) or 0) <= 10.0

        auth = await service.authenticate(issued.token)
        assert auth.expires_in == int(ACCESS_TTL)
        after = await cache.ttl(key)
        assert after is not None and after > 1000  # 又被延回去了

    async def test_remembered_session_slides_with_its_own_ttl(
        self, cache: Cache, client: ClientInfo
    ) -> None:
        """续期用的是这条会话**自己**那档有效期（长的那档不会被短档覆盖掉）。"""
        service = make_service(cache)
        issued = await service.open("u-0001", client=client, remember=True)
        await cache.expire(cache_key(issued.token), 10.0)

        auth = await service.authenticate(issued.token)
        assert auth.expires_in == int(REMEMBER_TTL)

    async def test_permanent_token_is_not_slid(self, client: ClientInfo) -> None:
        """``ttl = 0`` 表示永不过期：不延也不过期。"""
        facade = Cache()
        await facade.start()
        try:
            service = make_service(facade, access_ttl=0.0)
            issued = await service.open("u-0001", client=client)
            assert issued.expires_in == 0
            assert (await facade.ttl(cache_key(issued.token))) == float("inf")
            auth = await service.authenticate(issued.token)
            assert auth.expires_in == 0
            assert (await facade.ttl(cache_key(issued.token))) == float("inf")
        finally:
            await facade.stop()


# --------------------------------------------------------------------------- 设备列表 / 吊销
class TestRevoke:
    """双删吊销：先删缓存（令牌立刻失效），再删库里那行。"""

    async def test_list_shows_my_sessions(self, cache: Cache, client: ClientInfo) -> None:
        service = make_service(cache)
        phone = describe_client(user_agent=UA_SAFARI_IPHONE, device_name="我的 iPhone")
        await service.open("u-0001", client=client)
        await service.open("u-0001", client=phone)
        await service.open("u-0002", client=client)  # 别人的，不该出现

        rows = await service.list_for_user("u-0001")
        assert len(rows) == 2
        assert {row.device_name for row in rows} == {"Chrome · macOS", "我的 iPhone"}
        assert all(row.user_id == "u-0001" for row in rows)

    async def test_revoke_kills_token_and_row(self, cache: Cache, client: ClientInfo) -> None:
        store = InMemorySessionStore()
        service = make_service(cache, store=store)
        issued = await service.open("u-0001", client=client)
        assert (await service.authenticate(issued.token)).user_id == "u-0001"

        assert await service.revoke(issued.session.token_hash, user_id="u-0001") is True
        # 缓存这一半：令牌立刻不认
        assert await cache.get(cache_key(issued.token)) is None
        with pytest.raises(UnauthorizedError):
            await service.authenticate(issued.token)
        # 库里那一半：设备记录也没了
        assert await store.get(issued.session.token_hash) is None
        assert await service.list_for_user("u-0001") == ()

    async def test_cannot_revoke_someone_elses_session(
        self, cache: Cache, client: ClientInfo
    ) -> None:
        """拿别人的令牌摘要来吊销，按"没有这条"处理（不回"没权限"，免得能试探出存在性）。"""
        service = make_service(cache)
        issued = await service.open("u-0001", client=client)
        assert await service.revoke(issued.session.token_hash, user_id="u-0002") is False
        assert (await service.authenticate(issued.token)).user_id == "u-0001"

    async def test_revoke_missing_session_is_false(self, cache: Cache) -> None:
        service = make_service(cache)
        assert await service.revoke(hash_token("nacho_没有这条"), user_id="u-0001") is False

    async def test_revoke_all_logs_everything_out(
        self, cache: Cache, client: ClientInfo
    ) -> None:
        service = make_service(cache)
        phone = describe_client(user_agent=UA_SAFARI_IPHONE)
        first = await service.open("u-0001", client=client)
        second = await service.open("u-0001", client=phone)
        other = await service.open("u-0002", client=client)

        assert await service.revoke_all("u-0001") == 2
        assert await service.list_for_user("u-0001") == ()
        with pytest.raises(UnauthorizedError):
            await service.authenticate(first.token)
        with pytest.raises(UnauthorizedError):
            await service.authenticate(second.token)
        # 别人的不受影响
        assert (await service.authenticate(other.token)).user_id == "u-0002"


# --------------------------------------------------------------------------- 落库那份
class TestSqlSessionStore:
    """``auth_sessions`` 表：一行一次登录，**主键就是令牌摘要**。"""

    async def test_create_get_and_remove(self, tmp_path: Path, client: ClientInfo) -> None:
        engine: AsyncEngine = create_async_engine(
            f"sqlite+aiosqlite:///{(tmp_path / 'sessions.db').as_posix()}"
        )
        try:
            store = SqlSessionStore(engine)
            await store.ensure_schema()
            await store.ensure_schema()  # 幂等

            token_hash = hash_token("nacho_x")
            row = await store.create(token_hash, "u-0001", client=client, remembered=True)
            assert row.token_hash == token_hash
            assert row.device_name == "Chrome · macOS"
            assert (await store.get(token_hash)) == row

            assert await store.remove(token_hash) is True
            assert await store.get(token_hash) is None
            assert await store.remove(token_hash) is False
        finally:
            await engine.dispose()

    async def test_set_remembered(self, tmp_path: Path, client: ClientInfo) -> None:
        """改「记住设备」那一栏：落库版也要真写进去（登录复用时会走这条）。"""
        engine: AsyncEngine = create_async_engine(
            f"sqlite+aiosqlite:///{(tmp_path / 'sessions.db').as_posix()}"
        )
        try:
            store = SqlSessionStore(engine)
            await store.ensure_schema()
            token_hash = hash_token("nacho_x")
            await store.create(token_hash, "u-0001", client=client, remembered=False)

            updated = await store.set_remembered(token_hash, remembered=True)
            assert updated is not None
            assert updated.remembered is True
            assert (await store.get(token_hash)) == updated  # 落库了，不只是返回值的花样

            assert await store.set_remembered(hash_token("nacho_没有这条"), remembered=True) is None
        finally:
            await engine.dispose()

    async def test_remove_all_only_touches_that_user(
        self, tmp_path: Path, client: ClientInfo
    ) -> None:
        engine: AsyncEngine = create_async_engine(
            f"sqlite+aiosqlite:///{(tmp_path / 'sessions.db').as_posix()}"
        )
        try:
            store = SqlSessionStore(engine)
            await store.ensure_schema()
            await store.create(hash_token("a"), "u-0", client=client, remembered=False)
            await store.create(hash_token("b"), "u-1", client=client, remembered=False)

            assert await store.remove_all("u-0") == 1
            assert await store.list_for_user("u-0") == ()
            assert len(await store.list_for_user("u-1")) == 1  # 别人的没动
        finally:
            await engine.dispose()

    async def test_落库会话也能被服务端认得(
        self, tmp_path: Path, cache: Cache, client: ClientInfo
    ) -> None:
        """落库存储接上会话服务，走一遍开 -> 认 -> 吊销。"""
        engine: AsyncEngine = create_async_engine(
            f"sqlite+aiosqlite:///{(tmp_path / 'sessions.db').as_posix()}"
        )
        try:
            store = SqlSessionStore(engine)
            await store.ensure_schema()
            service = make_service(cache, store=store)

            issued = await service.open("u-0001", client=client)
            assert (await service.authenticate(issued.token)).user_id == "u-0001"
            assert len(await service.list_for_user("u-0001")) == 1

            assert await service.revoke(issued.session.token_hash, user_id="u-0001") is True
            assert await service.list_for_user("u-0001") == ()
        finally:
            await engine.dispose()


# --------------------------------------------------------------------------- 摘要冲突
class TestTokenHashCollision:
    """摘要撞上已有记录：**报错，绝不覆盖**。

    这该是撞不上的（256 位输出的生日界，见 :func:`hash_token`），所以这里不去"造"一个真
    碰撞——把同一个摘要交给两次 ``create``，效果等同真撞了。要验的就一件事：
    **原来那条记录分毫未动**（字典赋值本来会把它悄悄换掉）。
    """

    async def test_memory_store_refuses_to_overwrite(self, client: ClientInfo) -> None:
        store = InMemorySessionStore()
        token_hash = hash_token("nacho_x")
        first = await store.create(token_hash, "u-0001", client=client, remembered=False)

        with pytest.raises(TokenHashCollisionError):
            await store.create(token_hash, "u-0002", client=client, remembered=True)

        # 归属、设备、勾没勾「记住设备」——全都还是原来那份
        assert (await store.get(token_hash)) == first
        assert first.user_id == "u-0001"
        assert (await store.list_for_user("u-0002")) == ()

    async def test_sql_store_refuses_to_overwrite(
        self, tmp_path: Path, client: ClientInfo
    ) -> None:
        """落库版撞的是主键，翻成跟内存版**同一个**错（协议对两个实现的要求一致）。"""
        engine: AsyncEngine = create_async_engine(
            f"sqlite+aiosqlite:///{(tmp_path / 'sessions.db').as_posix()}"
        )
        try:
            store = SqlSessionStore(engine)
            await store.ensure_schema()
            token_hash = hash_token("nacho_x")
            first = await store.create(token_hash, "u-0001", client=client, remembered=False)

            with pytest.raises(TokenHashCollisionError):
                await store.create(token_hash, "u-0002", client=client, remembered=True)

            assert (await store.get(token_hash)) == first
            assert (await store.list_for_user("u-0002")) == ()
        finally:
            await engine.dispose()
