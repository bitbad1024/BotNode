"""配置：读 TOML、校验取值，产出一份 :class:`Settings`。

配置文件是 TOML（用 ``#`` 写注释），模板见 ``config.toml.example``，复制成
``config.toml`` 才生效；后者已进 ``.gitignore`` 不入库。

容错策略是「能跑就跑」：文件不存在、某一节某一项没写，都按 :class:`Settings`
的字段默认值补齐；只有**值写错**才抛 :class:`ConfigError` —— 级别名拼错、该填
整数填了字符串、driver 不在 sqlite/mariadb 里、表名不合法、端口越界。这样配置
拼错会在启动阶段就报出来，而不是静默用默认值。

用法::

    from config import ConfigError, Settings

    try:
        settings = Settings.load("config.toml")
    except ConfigError as exc:
        ...  # 报给用户，别拿默认值糊过去
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar, cast

from nacho.core.logger import LogLevel
from nacho.db import require_table_name

_T = TypeVar("_T")

#: 项目根目录：配置、日志、数据库文件的相对路径都相对它解析
BASE_DIR: Path = Path(__file__).resolve().parent
#: 默认配置文件（不入库）
CONFIG_PATH: Path = BASE_DIR / "config.toml"
#: 配置模板（入库）
TEMPLATE_PATH: Path = BASE_DIR / "config.toml.example"

#: 合法的日志级别名，报错提示用
_LEVEL_NAMES: str = "/".join(level.name for level in LogLevel)
#: 支持的数据库后端（driver 的取值）
_DRIVERS: tuple[str, ...] = ("sqlite", "mariadb")


class ConfigError(ValueError):
    """配置文件写错了：TOML 语法有误，或某一项的取值 / 类型不合法。"""


def _parse_level(value: object, key: str) -> LogLevel:
    if isinstance(value, str):
        try:
            return LogLevel.parse(value)
        except ValueError:
            pass
    raise ConfigError(f"{key} 要日志级别（{_LEVEL_NAMES}），收到 {value!r}")


def _section(data: dict[str, object], name: str) -> dict[str, object]:
    """取一节配置；没写或不是表格就当空节。"""
    section = data.get(name)
    return cast("dict[str, object]", section) if isinstance(section, dict) else {}


def _pick(section: dict[str, object], key: str, default: _T) -> _T:
    """取一项：没写用默认值，类型对不上报 :class:`ConfigError`。"""
    value = section.get(key, default)
    if isinstance(default, bool):
        ok = isinstance(value, bool)
    elif isinstance(default, float):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif isinstance(default, int):
        ok = isinstance(value, int) and not isinstance(value, bool)
    else:
        ok = isinstance(value, type(default))
    if not ok:
        raise ConfigError(f"{key} 要 {type(default).__name__}，收到 {value!r}")
    return cast("_T", value)


def _resolve(path: str) -> Path:
    """文件和数据库的相对路径相对项目根目录解析。"""
    result = Path(path)
    return result if result.is_absolute() else BASE_DIR / result


@dataclass(frozen=True)
class Settings:
    """一份设置；每个字段的默认值 = 配置里没写这项时的取值（键名同 config.toml.example）。"""

    # [app]
    name: str = "nacho"  # 进程名，同时是日志核心名（子实例都以它为前缀）
    debug: bool = False  # 调试模式：True 时忽略 level 直接开到 DEBUG

    # [logging]
    level: LogLevel = LogLevel.INFO
    console: bool = True
    console_color: bool = True
    console_level: LogLevel | None = None  # None = 跟随 level

    # [logging.file]
    file_enabled: bool = True
    file_path: Path = BASE_DIR / "logs" / "nacho.log"
    buffer_size: int = 200
    flush_interval: float = 2.0
    max_bytes: int = 20 * 1024 * 1024  # 0 = 不切割
    backup_count: int = 3

    # [logging.database]
    db_enabled: bool = False  # 默认不落库：要多一路数据库出口就置 true
    db_driver: str = "sqlite"  # sqlite / mariadb
    # sqlite 用：数据库文件路径
    db_path: Path = BASE_DIR / "logs" / "nacho.db"
    # mariadb 用：连接信息
    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "nacho"
    db_password: str = ""
    db_database: str = "nacho"
    # 两个后端共用：表名（启动时自动建表）+ 批量参数
    db_table: str = "logs"
    db_buffer_size: int = 500
    db_flush_interval: float = 5.0

    # [logging.queue]
    queue_maxsize: int = 10_000
    dispatch_batch_size: int = 200
    dispatch_timeout: float = 0.2

    #: 配置来源；None = 没找到配置文件，用的全是默认值
    config_path: Path | None = None

    @classmethod
    def load(cls, path: Path | str = CONFIG_PATH) -> "Settings":
        """读配置文件；文件不存在就返回一份全默认值。

        :raises ConfigError: TOML 语法有误、读不了文件，或某一项取值不合法。
        """
        config_path = Path(path)
        if not config_path.is_absolute():
            config_path = BASE_DIR / config_path
        if not config_path.is_file():
            return cls()

        try:
            with config_path.open("rb") as fp:
                data: dict[str, object] = tomllib.load(fp)
        except (tomllib.TOMLDecodeError, OSError) as exc:
            raise ConfigError(f"配置文件 {config_path} 读不了：{exc}") from exc

        app = _section(data, "app")
        log = _section(data, "logging")
        file = _section(log, "file")
        database = _section(log, "database")
        queue = _section(log, "queue")
        default = cls()

        console_level = log.get("console_level")

        db_driver = _pick(database, "driver", default.db_driver)
        if db_driver not in _DRIVERS:
            raise ConfigError(
                f"logging.database.driver 要 {'/'.join(_DRIVERS)}，收到 {db_driver!r}"
            )
        db_port = _pick(database, "port", default.db_port)
        if not 1 <= db_port <= 65535:
            raise ConfigError(f"logging.database.port 要 1-65535 的端口，收到 {db_port}")
        db_table = _pick(database, "table", default.db_table)
        try:
            require_table_name(db_table)
        except ValueError as exc:
            raise ConfigError(f"logging.database.table: {exc}") from exc

        return cls(
            name=_pick(app, "name", default.name),
            debug=_pick(app, "debug", default.debug),
            level=_parse_level(log.get("level", default.level.name), "logging.level"),
            console=_pick(log, "console", default.console),
            console_color=_pick(log, "console_color", default.console_color),
            console_level=(
                None
                if not isinstance(console_level, str) or not console_level.strip()
                else _parse_level(console_level, "logging.console_level")
            ),
            file_enabled=_pick(file, "enabled", default.file_enabled),
            file_path=_resolve(_pick(file, "path", "logs/nacho.log")),
            buffer_size=_pick(file, "buffer_size", default.buffer_size),
            flush_interval=_pick(file, "flush_interval", default.flush_interval),
            max_bytes=_pick(file, "max_bytes", default.max_bytes),
            backup_count=_pick(file, "backup_count", default.backup_count),
            db_enabled=_pick(database, "enabled", default.db_enabled),
            db_driver=db_driver,
            db_path=_resolve(_pick(database, "path", "logs/nacho.db")),
            db_host=_pick(database, "host", default.db_host),
            db_port=db_port,
            db_user=_pick(database, "user", default.db_user),
            db_password=_pick(database, "password", default.db_password),
            db_database=_pick(database, "database", default.db_database),
            db_table=db_table,
            db_buffer_size=_pick(database, "buffer_size", default.db_buffer_size),
            db_flush_interval=_pick(database, "flush_interval", default.db_flush_interval),
            queue_maxsize=_pick(queue, "maxsize", default.queue_maxsize),
            dispatch_batch_size=_pick(queue, "dispatch_batch_size", default.dispatch_batch_size),
            dispatch_timeout=_pick(queue, "dispatch_timeout", default.dispatch_timeout),
            config_path=config_path,
        )
