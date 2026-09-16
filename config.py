"""配置：读 TOML、校验取值，产出一份 :class:`Settings`。

配置文件是 TOML（用 ``#`` 写注释），模板见 ``config.toml.example``，复制成
``config.toml`` 才生效；后者已进 ``.gitignore`` 不入库。

容错策略是「能跑就跑」：文件不存在、某一节某一项没写，都按各区域字段的默认值补齐；
只有**值写错**才抛 :class:`ConfigError` —— 级别名拼错、该填整数填了字符串、driver 不在
sqlite/mariadb 里、表名不合法、端口越界。这样配置拼错会在启动阶段就报出来，而不是静默
用默认值。

代码按配置文件里的区域切块：一节一个 dataclass、一个解析函数，配置里写哪节就在代码里翻
哪块::

    [app]                -> Settings.app                进程名、调试开关
    [database]           -> Settings.database           整项目共用的数据库连接
    [logging]            -> Settings.logging            级别、控制台、队列
    [logging.file]       -> ...logging.file             本地文件出口
    [logging.database]   -> ...logging.database         数据库出口
    [logging.queue]      -> ...logging.queue            异步队列与分发器

数据库配置按「专用 > 公共 > 默认」三层逐项覆盖：``[database]`` 是整项目共用的数据库连接
（driver / path / host / port / user / password / database），``[logging.database]`` 是
日志出口的专用配置 —— 连接类项默认整项继承公共节，日志特有的 enabled / table /
buffer_size / flush_interval 只在这一节；要给日志单独连另一个库，就在这一节里覆盖同名项，
覆盖粒度是**逐项**的（没写的那几项继续继承）。两者合并的最终结果放在
``Settings.logging.database.connection``。

用法::

    from config import ConfigError, Settings

    try:
        settings = Settings.load("config.toml")
    except ConfigError as exc:
        ...  # 报给用户，别拿默认值糊过去
"""
from __future__ import annotations

import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar, cast

from nacho.core.logger import LogLevel
from nacho.db import require_table_name

_T = TypeVar("_T")

#: 项目根目录：配置、日志、数据库文件的相对路径都相对它解析
BASE_DIR: Path = Path(__file__).resolve().parent
#: 默认配置文件（不入库）
CONFIG_PATH: Path = BASE_DIR / "config.toml"

#: 合法的日志级别名，报错提示用
_LEVEL_NAMES: str = "/".join(level.name for level in LogLevel)
#: 支持的数据库后端（driver 的取值）
_DRIVERS: tuple[str, ...] = ("sqlite", "mariadb")


class ConfigError(ValueError):
    """配置文件写错了：TOML 语法有误，或某一项的取值 / 类型不合法。"""


# --------------------------------------------------------------------------- 通用工具
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


def _where_in(section_name: str) -> Callable[[str], str]:
    """报错定位用：``section_name`` 这一节里某项的完整出处（如 ``database.port``）。"""
    return lambda key: f"{section_name}.{key}"


def _pick(section: dict[str, object], key: str, default: _T, where: str = "") -> _T:
    """取一项：没写用默认值，类型对不上报 :class:`ConfigError`。

    ``where`` 是这项的完整出处（如 ``database.driver``），只在报错时用 —— 多层
    覆盖之后同一个键可能来自不同的节，报错得指对地方。
    """
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
        raise ConfigError(f"{where or key} 要 {type(default).__name__}，收到 {value!r}")
    return cast("_T", value)


def _overlay(
    *layers: tuple[str, dict[str, object]],
) -> tuple[dict[str, object], dict[str, str]]:
    """按「靠后的层覆盖靠前的层」合并配置。

    返回 ``(合并后的映射, 每项来自哪一节)``；后者给 :func:`_pick` 的 ``where`` 用
    —— 覆盖之后光看键名已经不知道值写在哪个节里了。
    """
    merged: dict[str, object] = {}
    origin: dict[str, str] = {}
    for section_name, section in layers:
        for key, value in section.items():
            merged[key] = value
            origin[key] = f"{section_name}.{key}"
    return merged, origin


def _resolve(path: str | Path) -> Path:
    """文件和数据库的相对路径相对项目根目录解析。"""
    result = Path(path)
    return result if result.is_absolute() else BASE_DIR / result


def _pick_path(section: dict[str, object], key: str, default: Path, where: str) -> Path:
    """取一项路径：配置里写的是字符串，相对路径按项目根目录解析。"""
    return _resolve(_pick(section, key, str(default), where))


# --------------------------------------------------------------------------- 区域：[app]
@dataclass(frozen=True)
class AppSettings:
    """``[app]``：进程级设置，和日志、数据库无关。"""

    name: str = "nacho"  # 进程名，同时是日志核心名（子实例都以它为前缀）
    debug: bool = False  # 调试模式：True 时忽略 level 直接开到 DEBUG


def _load_app(section: dict[str, object]) -> AppSettings:
    """读 ``[app]``。"""
    default = AppSettings()
    return AppSettings(
        name=_pick(section, "name", default.name, "app.name"),
        debug=_pick(section, "debug", default.debug, "app.debug"),
    )


# ---------------------------------------------------------------------- 区域：[database]
@dataclass(frozen=True)
class DatabaseSettings:
    """一份数据库连接参数：``[database]`` 公共节，以及覆盖它之后的日志出口连接。

    两种 driver 用的键不一样，没用到的那几个保持默认值即可（模板里也是这么注释的）：
    sqlite 只看 ``path``，mariadb 看 ``host`` / ``port`` / ``user`` / ``password`` /
    ``database``。
    """

    driver: str = "sqlite"  # sqlite / mariadb
    # sqlite 用：数据库文件路径（相对路径相对项目根目录）
    path: Path = BASE_DIR / "logs" / "nacho.db"
    # mariadb 用：服务地址与账号
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "nacho"
    password: str = ""  # config.toml 不入库，密码写这里不会进 git
    database: str = "nacho"  # 库名（要事先建好）


def _load_connection(
    section: dict[str, object], where: Callable[[str], str]
) -> DatabaseSettings:
    """读一个连接节（``[database]``，或覆盖后的 ``[logging.database]``）并逐项校验。

    ``where`` 给出一项的出处（如 ``logging.database.port``）—— 覆盖之后同一个键可能
    来自不同的节，报错得指对地方。
    """
    default = DatabaseSettings()
    driver = _pick(section, "driver", default.driver, where("driver"))
    if driver not in _DRIVERS:
        raise ConfigError(f"{where('driver')} 要 {'/'.join(_DRIVERS)}，收到 {driver!r}")
    port = _pick(section, "port", default.port, where("port"))
    if not 1 <= port <= 65535:
        raise ConfigError(f"{where('port')} 要 1-65535 的端口，收到 {port}")
    return DatabaseSettings(
        driver=driver,
        path=_pick_path(section, "path", default.path, where("path")),
        host=_pick(section, "host", default.host, where("host")),
        port=port,
        user=_pick(section, "user", default.user, where("user")),
        password=_pick(section, "password", default.password, where("password")),
        database=_pick(section, "database", default.database, where("database")),
    )


# ----------------------------------------------------------------------- 区域：[logging]
@dataclass(frozen=True)
class FileLogSettings:
    """``[logging.file]``：本地文件出口。"""

    enabled: bool = True  # false 就只有控制台
    path: Path = BASE_DIR / "logs" / "nacho.log"  # 相对路径相对项目根目录
    buffer_size: int = 200  # 攒够多少条写一次盘（1 = 每条直写，最慢但最稳）
    flush_interval: float = 2.0  # 最多攒多久（秒）必须写一次盘
    max_bytes: int = 20 * 1024 * 1024  # 单个文件超过就切割（20MB）；0 = 不切割
    backup_count: int = 3  # 切割后保留几个历史文件


def _load_file_log(section: dict[str, object]) -> FileLogSettings:
    """读 ``[logging.file]``。"""
    default = FileLogSettings()
    where = _where_in("logging.file")
    return FileLogSettings(
        enabled=_pick(section, "enabled", default.enabled, where("enabled")),
        path=_pick_path(section, "path", default.path, where("path")),
        buffer_size=_pick(section, "buffer_size", default.buffer_size, where("buffer_size")),
        flush_interval=_pick(
            section, "flush_interval", default.flush_interval, where("flush_interval")
        ),
        max_bytes=_pick(section, "max_bytes", default.max_bytes, where("max_bytes")),
        backup_count=_pick(section, "backup_count", default.backup_count, where("backup_count")),
    )


@dataclass(frozen=True)
class DatabaseLogSettings:
    """``[logging.database]``：把日志再落一份到数据库的出口。

    这里只放日志出口特有的项；连接项在 :attr:`connection`（覆盖之后才知道最终值）。
    """

    enabled: bool = False  # 默认不落库：要多一路数据库出口就置 true
    table: str = "logs"  # 日志表名（启动时自动建表）
    buffer_size: int = 500  # 攒够多少条写一次库
    flush_interval: float = 5.0  # 最多攒多久（秒）必须写一次库
    #: 这个出口真正连的库：[logging.database] 的连接项逐项盖在 [database] 上之后的结果；
    #: 没单独配就是 [database] 那一份（不是配置项，是算出来的）
    connection: DatabaseSettings = field(default_factory=DatabaseSettings)


def _load_database_log(
    merged: dict[str, object],
    connection: DatabaseSettings,
    where: Callable[[str], str],
) -> DatabaseLogSettings:
    """读 ``[logging.database]`` 特有的项；``merged`` 是两层覆盖后的整份连接映射。"""
    default = DatabaseLogSettings()
    table = _pick(merged, "table", default.table, where("table"))
    try:
        require_table_name(table)
    except ValueError as exc:
        raise ConfigError(f"{where('table')}: {exc}") from exc
    return DatabaseLogSettings(
        enabled=_pick(merged, "enabled", default.enabled, where("enabled")),
        table=table,
        buffer_size=_pick(merged, "buffer_size", default.buffer_size, where("buffer_size")),
        flush_interval=_pick(
            merged, "flush_interval", default.flush_interval, where("flush_interval")
        ),
        connection=connection,
    )


@dataclass(frozen=True)
class QueueSettings:
    """``[logging.queue]``：日志异步队列与分发器。"""

    maxsize: int = 10_000  # 队列容量，满了按「丢弃最旧」处理
    dispatch_batch_size: int = 200  # 分发器一轮最多取多少条
    dispatch_timeout: float = 0.2  # 分发器一轮最多等多久（秒）


def _load_queue(section: dict[str, object]) -> QueueSettings:
    """读 ``[logging.queue]``。"""
    default = QueueSettings()
    where = _where_in("logging.queue")
    return QueueSettings(
        maxsize=_pick(section, "maxsize", default.maxsize, where("maxsize")),
        dispatch_batch_size=_pick(
            section,
            "dispatch_batch_size",
            default.dispatch_batch_size,
            where("dispatch_batch_size"),
        ),
        dispatch_timeout=_pick(
            section, "dispatch_timeout", default.dispatch_timeout, where("dispatch_timeout")
        ),
    )


@dataclass(frozen=True)
class LoggingSettings:
    """``[logging]``：日志总控；下挂三条子区域，一项在哪块看名字就知道。"""

    level: LogLevel = LogLevel.INFO  # DEBUG / INFO / WARNING / ERROR / CRITICAL
    console: bool = True  # 是否往控制台输出（服务端 / 无人值守时置 false）
    console_color: bool = True  # 控制台是否按级别上 ANSI 颜色
    console_level: LogLevel | None = None  # 控制台单独的级别；None = 跟随 level
    file: FileLogSettings = field(default_factory=FileLogSettings)
    database: DatabaseLogSettings = field(default_factory=DatabaseLogSettings)
    queue: QueueSettings = field(default_factory=QueueSettings)


def _parse_console_level(section: dict[str, object]) -> LogLevel | None:
    """``console_level``：留空（或没写）表示跟随 ``level``，用 None 表示。"""
    value = section.get("console_level")
    if not isinstance(value, str) or not value.strip():
        return None
    return _parse_level(value, "logging.console_level")


def _load_logging(
    section: dict[str, object], public_db: dict[str, object]
) -> LoggingSettings:
    """读 ``[logging]``：本节开关 + file / database / queue 三条子区域。

    ``public_db`` 是 ``[database]`` 公共节；数据库出口的连接项按「专用 > 公共 > 默认」
    逐项覆盖，最后落到 ``logging.database.connection``。
    """
    default = LoggingSettings()
    # 连接项两层覆盖：[logging.database] 专用项 > [database] 公共项 > 字段默认值
    merged, origin = _overlay(
        ("database", public_db),
        ("logging.database", _section(section, "database")),
    )

    def where(key: str) -> str:
        """这一项最终写在哪个节里（覆盖之后光看键名看不出来）。"""
        return origin.get(key, f"logging.database.{key}")

    connection = _load_connection(merged, where)
    return LoggingSettings(
        level=_parse_level(section.get("level", default.level.name), "logging.level"),
        console=_pick(section, "console", default.console, "logging.console"),
        console_color=_pick(
            section, "console_color", default.console_color, "logging.console_color"
        ),
        console_level=_parse_console_level(section),
        file=_load_file_log(_section(section, "file")),
        database=_load_database_log(merged, connection, where),
        queue=_load_queue(_section(section, "queue")),
    )


# --------------------------------------------------------------------------- 整份设置
@dataclass(frozen=True)
class Settings:
    """一份设置：字段就是配置文件里的区域，一一对应；最后一项是元信息，不是配置项。

    每个字段的默认值 = 配置里没写这一节（项）时的取值，键名同 ``config.toml.example``。
    """

    app: AppSettings = field(default_factory=AppSettings)
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)

    #: 配置来源；None = 没找到配置文件，用的全是默认值
    config_path: Path | None = None

    @classmethod
    def load(cls, path: Path | str = CONFIG_PATH) -> Settings:
        """读配置文件；文件不存在就返回一份全默认值。

        :raises ConfigError: TOML 语法有误、读不了文件，或某一项取值不合法。
        """
        config_path = Path(path)
        config_path = config_path if config_path.is_absolute() else BASE_DIR / config_path
        if not config_path.is_file():
            return cls()

        try:
            with config_path.open("rb") as fp:
                data: dict[str, object] = tomllib.load(fp)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"配置文件 {config_path} 的 TOML 语法有误：{exc}") from exc
        except OSError as exc:
            raise ConfigError(f"配置文件 {config_path} 无法读取：{exc}") from exc

        # 一块区域一个解析函数：读哪一节、错了报哪一项，都在那一块里说清
        public_db = _section(data, "database")
        return cls(
            app=_load_app(_section(data, "app")),
            database=_load_connection(public_db, _where_in("database")),
            logging=_load_logging(_section(data, "logging"), public_db),
            config_path=config_path,
        )
