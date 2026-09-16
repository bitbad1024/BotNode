"""config.py 的单元测试：TOML 解析、数据库三层覆盖（专用 > 公共 > 默认）、报错定位。"""
from __future__ import annotations

from dataclasses import replace
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
        assert replace(settings, config_path=None) == Settings()

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
        assert (log_db.table, log_db.buffer_size) == ("logs", 500)
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
                    table = "log_entries"
                    """
                ),
            )
        )
        conn = settings.logging.database.connection
        assert conn.driver == "sqlite"
        assert conn.path == BASE_DIR / "logs" / "only-log.db"
        assert settings.logging.database.table == "log_entries"
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


class TestErrors:
    @pytest.mark.parametrize(
        ("text", "prefix"),
        [
            ('[database]\ndriver = "pg"\n', "database.driver"),
            ('[logging.database]\ndriver = "pg"\n', "logging.database.driver"),
            ("[database]\nport = 70000\n", "database.port"),
            ('[database]\nhost = 1\n', "database.host"),
            ('[logging.database]\nport = "x"\n', "logging.database.port"),
            ('[logging.database]\ntable = "1bad"\n', "logging.database.table"),
            ('[logging]\nlevel = "LOUD"\n', "logging.level"),
        ],
    )
    def test_error_names_the_section(self, tmp_path: Path, text: str, prefix: str) -> None:
        """值写错要报在它真正所处的节上 —— 覆盖之后光看键名是看不出来的。"""
        with pytest.raises(ConfigError) as info:
            Settings.load(write(tmp_path, text))
        assert str(info.value).startswith(prefix)


class TestTemplate:
    def test_example_template_loads(self) -> None:
        """模板必须始终可加载：它既是文档，也是新环境的起点。"""
        settings = Settings.load(TEMPLATE_PATH)
        assert settings.config_path == TEMPLATE_PATH
        conn = settings.logging.database.connection
        assert (conn.driver, conn.host) == ("sqlite", "127.0.0.1")
        assert settings.logging.database.table == "logs"

    def test_template_agrees_with_defaults(self) -> None:
        """模板里的值要和代码默认值一致，否则「照抄模板启动」会悄悄改行为。"""
        assert replace(Settings.load(TEMPLATE_PATH), config_path=None) == Settings()
