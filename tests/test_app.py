"""应用入口的启动辅助测试：数据库引擎拼接、启动阶段的连接探测。

入口本身（``python app.py``）不在这里跑（会真的连库/起服务）；只测拆分出来的纯函数：
``_engine_url``（URL 拼接、口令不进 target）与 ``_probe_database``（启动阶段主动连一次，
连不上当场抛带 target 的 RuntimeError，让入口打成 ``[初始化错误]`` 干净退出）。
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

import app
from config import DatabaseSettings, Settings


# --------------------------------------------------------------------------- _engine_url
def test_engine_url_sqlite_creates_parent_dir(tmp_path) -> None:
    """sqlite：URL 指向文件路径，且父目录会被顺手建出来（不依赖先手工建目录）。"""
    settings = DatabaseSettings(driver="sqlite", path=tmp_path / "nested" / "tickneko.db")
    url, target = app._engine_url(settings)

    assert url == f"sqlite+aiosqlite:///{settings.path.as_posix()}"
    assert target == settings.path.as_posix()
    assert (tmp_path / "nested").is_dir()


def test_engine_url_mariadb_keeps_password_out_of_target() -> None:
    """mariadb：URL 带口令（驱动要用），target 只留 host:port/db（口令不进日志）。"""
    settings = DatabaseSettings(
        driver="mariadb",
        host="192.168.1.200",
        port=3306,
        user="root",
        password="s3cret",
        database="tickneko",
    )
    url, target = app._engine_url(settings)

    assert "s3cret" in url  # 驱动要拿到口令
    assert "s3cret" not in target  # 给人看/落日志的摘录里没有
    assert target == "192.168.1.200:3306/tickneko"
    assert url.startswith("mysql+aiomysql://")


# --------------------------------------------------------------------------- _probe_database
class _FakeConn:
    """假的异步连接：``execute`` 按需抛 SQLAlchemy 错误（模拟连不上库）。"""

    def __init__(self, exc: BaseException | None) -> None:
        self._exc = exc

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def execute(self, *args: object, **kwargs: object) -> None:
        if self._exc is not None:
            raise self._exc


class _FakeEngine:
    """假的异步引擎：``connect()`` 返回上面那把假连接。"""

    def __init__(self, exc: BaseException | None = None) -> None:
        self._exc = exc

    def connect(self) -> _FakeConn:
        return _FakeConn(self._exc)


async def test_probe_database_ok_when_reachable() -> None:
    """连得上：SELECT 1 正常执行，探测不抛。"""
    await app._probe_database(_FakeEngine(), "192.168.1.200:3306/tickneko", "mariadb")  # 不抛


async def test_probe_database_raises_with_target_on_unreachable() -> None:
    """连不上：抛 RuntimeError，消息带 target 与 driver（入口好打成 [初始化错误] 提示）。"""
    engine = _FakeEngine(OperationalError("stmt", {}, "Can't connect to MySQL server"))
    with pytest.raises(RuntimeError, match="连不上数据库（mariadb @ 127.0.0.1:3306/tickneko）"):
        await app._probe_database(engine, "127.0.0.1:3306/tickneko", "mariadb")


async def test_probe_database_cleanup_on_failure() -> None:
    """连不上也要正常退出上下文（连接归还/释放）：__aexit__ 被走到、不吞异常。"""

    class _TrackingConn(_FakeConn):
        def __init__(self) -> None:
            super().__init__(OperationalError("stmt", {}, "boom"))
            self.closed = False

        async def __aexit__(self, *exc_info: object) -> bool:
            self.closed = True
            return False

    class _TrackingEngine(_FakeEngine):
        def __init__(self) -> None:
            super().__init__()
            self.conn = _TrackingConn()

        def connect(self) -> _TrackingConn:
            return self.conn

    engine = _TrackingEngine()
    with pytest.raises(RuntimeError, match="连不上数据库"):
        await app._probe_database(engine, "127.0.0.1:3306/tickneko", "mariadb")
    assert engine.conn.closed


def test_settings_example_is_loadable() -> None:
    """配置模板能正常加载（入口第一步就是 Settings.load）：跑偏了启动时第一个报错就不是连库。"""
    import config

    settings = Settings.load(config.TEMPLATE_PATH)
    assert settings.app.name
