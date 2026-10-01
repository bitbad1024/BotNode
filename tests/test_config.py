"""config.py 的单元测试：TOML 解析、数据库三层覆盖（专用 > 公共 > 默认）、报错定位。"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from config import BASE_DIR, TEMPLATE_PATH, ConfigError, DatabaseSettings, Settings


def write(tmp_path: Path, text: str) -> Path:
    """把一段 TOML 落盘，返回路径。"""
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


class TestDatabaseLayers:
    """日志库出口的取值链：[logging.database] > [database] > 字段默认值，逐项覆盖。"""

    def test_missing_file_falls_back_to_defaults(self, tmp_path: Path) -> None:
        settings = Settings.load(tmp_path / "nope.toml")
        assert settings == Settings()
        assert settings.config_path is None  # 记下「没找到文件」，好提示用户

    def test_empty_file_falls_back_to_defaults(self, tmp_path: Path) -> None:
        settings = Settings.load(write(tmp_path, ""))
        assert settings.model_copy(update={"config_path": None}) == Settings()

    def test_public_section_supplies_log_connection(self, tmp_path: Path) -> None:
        """只写公共节：日志出口的连接项整项继承它，日志特有的项还走默认值。"""
        settings = Settings.load(
            write(
                tmp_path,
                dedent(
                    """\
                    [database]
                    driver = "mariadb"
                    host = "10.0.0.5"
                    port = 3307
                    user = "sys"
                    password = "pw"
                    database = "appdb"
                    """
                ),
            )
        )
        conn = settings.logging.database.connection
        assert (conn.driver, conn.host, conn.port) == ("mariadb", "10.0.0.5", 3307)
        assert (conn.user, conn.password, conn.database) == ("sys", "pw", "appdb")
        log_db = settings.logging.database
        assert log_db.buffer_size == 500  # 日志特有的项没写 -> 默认值（表名不在这儿配）
        assert log_db.enabled is False  # 落不落库是日志出口自己的开关

    def test_public_section_reads_on_its_own(self, tmp_path: Path) -> None:
        """[database] 自己就是一份可读的连接（以后的数据层直接用），日志出口另算一份。"""
        settings = Settings.load(
            write(
                tmp_path,
                dedent(
                    """\
                    [database]
                    driver = "mariadb"
                    host = "10.0.0.5"

                    [logging.database]
                    path = "logs/only-log.db"
                    """
                ),
            )
        )
        assert settings.database == DatabaseSettings(driver="mariadb", host="10.0.0.5")
        conn = settings.logging.database.connection
        assert (conn.driver, conn.host) == ("mariadb", "10.0.0.5")  # 连接项继续继承公共节
        assert conn.path == BASE_DIR / "logs" / "only-log.db"  # 被专用项盖掉的这一项

    def test_log_section_overrides_item_by_item(self, tmp_path: Path) -> None:
        """专用节只盖自己写了的项，没写的继续继承公共节。"""
        settings = Settings.load(
            write(
                tmp_path,
                dedent(
                    """\
                    [database]
                    driver = "mariadb"
                    host = "10.0.0.5"
                    port = 3307
                    user = "sys"
                    database = "appdb"

                    [logging.database]
                    driver = "sqlite"
                    path = "logs/only-log.db"
                    flush_interval = 9.0
                    """
                ),
            )
        )
        conn = settings.logging.database.connection
        assert conn.driver == "sqlite"
        assert conn.path == BASE_DIR / "logs" / "only-log.db"
        assert settings.logging.database.flush_interval == 9.0
        assert (conn.host, conn.port, conn.user) == ("10.0.0.5", 3307, "sys")
        assert conn.database == "appdb"

    def test_log_section_alone_overrides_defaults(self, tmp_path: Path) -> None:
        """没有公共节时，专用节直接盖在字段默认值上。"""
        settings = Settings.load(
            write(
                tmp_path,
                dedent(
                    """\
                    [logging.database]
                    enabled = true
                    host = "192.168.1.9"
                    """
                ),
            )
        )
        assert settings.logging.database.enabled is True
        assert settings.logging.database.connection.host == "192.168.1.9"
        assert settings.logging.database.connection.driver == "sqlite"  # 两层都没写 -> 默认值兜底


class TestCacheRegion:
    """[cache]：默认走本地内存（不启用 Redis 也能用），换 Redis 只是改后端 + 连接参数。"""

    def test_defaults_to_memory_without_section(self, tmp_path: Path) -> None:
        settings = Settings.load(write(tmp_path, ""))
        cache = settings.cache
        assert cache.backend == "memory"
        assert cache.namespace == "nacho"
        assert (cache.default_ttl, cache.fallback_to_memory) == (0.0, False)  # 默认不降级
        assert cache.redis.port == 6379

    def test_redis_backend_reads_connection(self, tmp_path: Path) -> None:
        settings = Settings.load(
            write(
                tmp_path,
                dedent(
                    """\
                    [cache]
                    backend = "redis"
                    namespace = "app"
                    default_ttl = 30.0
                    fallback_to_memory = false

                    [cache.redis]
                    host = "10.0.0.9"
                    port = 6390
                    db = 3
                    password = "pw"
                    """
                ),
            )
        )
        cache = settings.cache
        assert (cache.backend, cache.namespace) == ("redis", "app")
        assert (cache.default_ttl, cache.fallback_to_memory) == (30.0, False)
        assert (cache.redis.host, cache.redis.port, cache.redis.db) == ("10.0.0.9", 6390, 3)
        assert cache.redis.password == "pw"

    def test_redis_section_alone_keeps_memory_backend(self, tmp_path: Path) -> None:
        """只写连接参数、没把 backend 改成 redis：仍然走本地内存（那份参数备而不用）。"""
        settings = Settings.load(write(tmp_path, '[cache.redis]\nhost = "10.0.0.9"\n'))
        assert settings.cache.backend == "memory"
        assert settings.cache.redis.host == "10.0.0.9"

    def test_empty_namespace_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="cache.namespace"):
            Settings.load(write(tmp_path, '[cache]\nnamespace = ""\n'))


class TestErrors:
    @pytest.mark.parametrize(
        ("text", "prefix"),
        [
            ('[database]\ndriver = "pg"\n', "database.driver"),
            ('[logging.database]\ndriver = "pg"\n', "logging.database.driver"),
            ("[database]\nport = 70000\n", "database.port"),
            ('[database]\nhost = 1\n', "database.host"),
            ('[logging.database]\nport = "x"\n', "logging.database.port"),
            ('[logging]\nlevel = "LOUD"\n', "logging.level"),
            ('[cache]\nbackend = "memcached"\n', "cache.backend"),
            ("[cache]\ndefault_ttl = -1\n", "cache.default_ttl"),
            ('[cache]\nnamespace = "a b"\n', "cache.namespace"),
            ("[cache.redis]\nport = 70000\n", "cache.redis.port"),
        ],
    )
    def test_error_names_the_section(self, tmp_path: Path, text: str, prefix: str) -> None:
        """值写错要报在它真正所处的节上 —— 覆盖之后光看键名是看不出来的。"""
        with pytest.raises(ConfigError) as info:
            Settings.load(write(tmp_path, text))
        assert str(info.value).startswith(prefix)


class TestLegacyKeys:
    """旧版键直接在报错里指路：静默忽略会变成「配了没生效」，比报错难查得多。"""

    @pytest.mark.parametrize(
        ("text", "prefix", "replacement"),
        [
            ('[logging.file]\npath = "logs/nacho.log"\n', "logging.file.path", "dir"),
            ("[logging.file]\nbackup_count = 3\n", "logging.file.backup_count", "keep_days"),
        ],
    )
    def test_file_outlet_rejects_legacy_keys(
        self, tmp_path: Path, text: str, prefix: str, replacement: str
    ) -> None:
        with pytest.raises(ConfigError) as info:
            Settings.load(write(tmp_path, text))
        message = str(info.value)
        assert message.startswith(prefix)  # 报在它真正所处的节上
        assert replacement in message  # 顺手告诉人该改成哪个键

    def test_file_outlet_reads_the_new_shape(self, tmp_path: Path) -> None:
        """新形状照常读：目录 + 前缀 + 保留天数 + 检索窗口。"""
        settings = Settings.load(
            write(
                tmp_path,
                dedent(
                    """\
                    [logging.file]
                    dir = "logs/here"
                    prefix = "nacho-web"
                    keep_days = 3
                    search_days = 0
                    """
                ),
            )
        )
        file_log = settings.logging.file
        assert file_log.dir == BASE_DIR / "logs" / "here"
        assert file_log.prefix == "nacho-web"
        assert (file_log.keep_days, file_log.search_days) == (3, 0)


class TestTemplate:
    def test_example_template_loads(self) -> None:
        """模板必须始终可加载：它既是文档，也是新环境的起点。"""
        settings = Settings.load(TEMPLATE_PATH)
        assert settings.config_path == TEMPLATE_PATH
        conn = settings.logging.database.connection
        assert (conn.driver, conn.host) == ("sqlite", "127.0.0.1")
        assert settings.logging.database.enabled is False

    def test_template_agrees_with_defaults(self) -> None:
        """模板里的值要和代码默认值一致，否则「照抄模板启动」会悄悄改行为。"""
        loaded = Settings.load(TEMPLATE_PATH)
        assert loaded.model_copy(update={"config_path": None}) == Settings()


class TestKookRegion:
    """[kook]：正向 WS 接入；token 留空 = 不接入。"""

    def test_defaults_without_section(self, tmp_path: Path) -> None:
        settings = Settings.load(write(tmp_path, ""))
        kook = settings.kook
        assert kook.gateway == ""  # 留空 = 连接前走 gateway/index 动态获取
        assert kook.token == ""  # 没配 = 不接入
        assert kook.heartbeat_interval == 30.0
        assert kook.reconnect_max_interval == 30.0
        assert kook.rest_min_interval == 0.2
        assert kook.rest_max_retries == 3

    def test_kook_section_reads_token_and_intervals(self, tmp_path: Path) -> None:
        settings = Settings.load(
            write(
                tmp_path,
                dedent(
                    """\
                    [kook]
                    token = "bot-token-xxx"
                    heartbeat_interval = 15.0
                    reconnect_interval = 5.0
                    reconnect_max_interval = 20.0
                    rest_min_interval = 0.5
                    rest_max_retries = 5
                    """
                ),
            )
        )
        kook = settings.kook
        assert kook.token == "bot-token-xxx"
        assert kook.heartbeat_interval == 15.0
        assert kook.reconnect_interval == 5.0
        assert kook.action_timeout == 30.0  # 没写的回默认
        assert kook.reconnect_max_interval == 20.0
        assert kook.rest_min_interval == 0.5
        assert kook.rest_max_retries == 5
