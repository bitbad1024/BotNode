"""配置：读 TOML、用 pydantic 校验取值，产出一份 :class:`Settings`。

配置文件是 TOML（用 ``#`` 写注释），模板见 ``config.toml.example``，复制成
``config.toml`` 才生效；后者已进 ``.gitignore`` 不入库。

容错策略是「能跑就跑」：文件不存在、某一节某一项没写，都按各区域字段的默认值补齐；
只有**值写错**才抛 :class:`ConfigError` —— 级别名拼错、该填整数填了字符串、driver 不在
sqlite/mariadb 里、端口越界。这样配置拼错会在启动阶段就报出来，而不是静默用默认值。

校验交给 pydantic：类型、取值范围（端口 1-65535、driver 只能 sqlite/mariadb）、路径解析
都写在字段旁边 —— 不用再手写 ``isinstance`` 那一套；pydantic 的报错再由 :func:`_describe`
翻成统一格式的中文提示（带完整出处，如 ``logging.database.port``）。

代码按配置文件里的区域切块：一节一个模型，配置里写哪节就在代码里翻哪块::

    [app]                -> Settings.app                进程名、调试开关
    [database]           -> Settings.database           整项目共用的数据库连接
    [logging]            -> Settings.logging            级别、控制台、队列
    [logging.file]       -> ...logging.file             本地文件出口
    [logging.database]   -> ...logging.database         数据库出口
    [logging.queue]      -> ...logging.queue            异步队列与分发器
    [cache]              -> Settings.cache              缓存总控：用哪个后端
    [cache.redis]        -> ...cache.redis              Redis 连接（backend=redis 时才用）
    [api]                -> Settings.api                接口层：监听地址、路由前缀、令牌有效期
    [onebot]             -> Settings.onebot             反向 WS 接入：监听地址、路径、令牌

数据库配置按「专用 > 公共 > 默认」三层逐项覆盖：``[database]`` 是整项目共用的数据库连接
（driver / path / host / port / user / password / database），``[logging.database]`` 是
日志出口的专用配置 —— 连接类项默认整项继承公共节，日志特有的 enabled / buffer_size /
flush_interval 只在这一节；要给日志单独连另一个库，就在这一节里覆盖同名项，
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
from pathlib import Path
from typing import Annotated, ClassVar, Literal, TypeAlias, TypeVar, cast, get_args

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)
from pydantic.fields import FieldInfo

from nacho.core.logger import LogLevel

#: 项目根目录：配置、日志、数据库文件的相对路径都相对它解析
BASE_DIR: Path = Path(__file__).resolve().parent
#: 默认配置文件（不入库）
CONFIG_PATH: Path = BASE_DIR / "config.toml"
#: 配置模板（入库）
TEMPLATE_PATH: Path = BASE_DIR / "config.toml.example"

#: 合法的日志级别名，报错提示用
_LEVEL_NAMES: str = "/".join(level.name for level in LogLevel)
#: 报错里「要 xxx」用的类型名
_LABELS: dict[object, str] = {
    str: "str",
    int: "int",
    float: "float",
    bool: "bool",
    Path: "字符串路径",
}


class ConfigError(ValueError):
    """配置文件写错了：TOML 语法有误，或某一项的取值 / 类型不合法。"""


# --------------------------------------------------------------------------- 通用工具
def _where_in(section_name: str) -> Callable[[str], str]:
    """报错定位用：``section_name`` 这一节里某项的完整出处（如 ``database.port``）。"""
    return lambda key: f"{section_name}.{key}"


def _section(data: dict[str, object], name: str) -> dict[str, object]:
    """取一节配置；没写或不是表格就当空节。"""
    section = data.get(name)
    return cast("dict[str, object]", section) if isinstance(section, dict) else {}


def _overlay(
    *layers: tuple[str, dict[str, object]],
) -> tuple[dict[str, object], dict[str, str]]:
    """按「靠后的层覆盖靠前的层」合并配置。

    返回 ``(合并后的映射, 每项来自哪一节)``；后者用来定位报错 —— 覆盖之后光看键名
    已经不知道值写在哪个节里了。
    """
    merged: dict[str, object] = {}
    origin: dict[str, str] = {}
    for section_name, section in layers:
        for key, value in section.items():
            merged[key] = value
            origin[key] = f"{section_name}.{key}"
    return merged, origin


def _requirement(field: FieldInfo) -> str:
    """这一项的要求，也就是报错里「要 xxx」的那一段。

    字段自己声明了要求（如端口范围）就用它，否则从类型推：``Literal`` 取候选值，
    ``LogLevel`` 取级别名，其余按类型名。
    """
    if field.description:
        return field.description
    for candidate in (field.annotation, *get_args(field.annotation)):
        if isinstance(candidate, type) and issubclass(candidate, LogLevel):
            return f"日志级别（{_LEVEL_NAMES}）"
    args: tuple[object, ...] = get_args(field.annotation)
    names: list[str] = [arg for arg in args if isinstance(arg, str)]
    if names:
        return "/".join(names)
    return _LABELS.get(field.annotation, str(field.annotation))


def _describe(
    model: type[BaseModel], exc: ValidationError, where: Callable[[str], str]
) -> str:
    """把 pydantic 的第一条错误翻成本项目的报错：完整出处 + 要求 + 收到的值。"""
    error = exc.errors()[0]
    key: str = str(error["loc"][0]) if error["loc"] else "?"
    if error["type"] == "value_error":  # 我们自己校验器说的话，原样带上
        return f"{where(key)}: {str(error['msg']).removeprefix('Value error, ')}"
    field: FieldInfo | None = model.model_fields.get(key)
    requirement: str = _requirement(field) if field else "合法取值"
    # 中文要求（「大于 0 的秒数」）前不留空格，类型名（str / int）前留一个
    gap: str = " " if requirement[0].isascii() else ""
    return f"{where(key)} 要{gap}{requirement}，收到 {error['input']!r}"


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _load(
    model: type[_ModelT], section: dict[str, object], where: Callable[[str], str]
) -> _ModelT:
    """校验一节配置：没写的项按字段默认值补，值写错就翻成 :class:`ConfigError`。

    ``where`` 给出一项的完整出处（如 ``logging.file.dir``），只在报错时用。
    旧版键（:attr:`_Region.legacy_keys`）先查一遍：静默忽略会变成「配了没生效」，比报错难查。
    """
    for key, replacement in model.legacy_keys.items():
        if key in section:
            raise ConfigError(f"{where(key)} 已不再使用：改用 {replacement}")
    try:
        return model.model_validate(section)
    except ValidationError as exc:
        raise ConfigError(_describe(model, exc, where)) from exc


# ----------------------------------------------------- 字段级校验器与区域公共底
def _parse_level(value: object) -> object:
    """级别名（``"info"``）/ 级别值都收；认不出来就原样交回，让 pydantic 报错。"""
    if isinstance(value, str):
        try:
            return LogLevel.parse(value)
        except ValueError:
            return value
    return value


def _parse_optional_level(value: object) -> object:
    """同 :func:`_parse_level`，但留空（或没写）表示「跟随 level」，用 None 表示。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _parse_level(value)


def _resolve(path: Path) -> Path:
    """相对路径相对项目根目录解析（配置里写的都是相对项目根目录）。"""
    return path if path.is_absolute() else BASE_DIR / path


#: 日志级别：写级别名（``"INFO"``）或级别值都行
Level: TypeAlias = Annotated[LogLevel, BeforeValidator(_parse_level)]
#: 可选日志级别：留空表示跟随上面的 level
OptionalLevel: TypeAlias = Annotated[LogLevel | None, BeforeValidator(_parse_optional_level)]
#: 路径：配置里写字符串，相对路径按项目根目录解析
ConfigPath: TypeAlias = Annotated[Path, AfterValidator(_resolve)]
#: 支持的数据库后端
Driver: TypeAlias = Literal["sqlite", "mariadb"]
#: 支持的缓存后端：memory = 进程内存（默认），redis = Redis 服务
CacheBackendName: TypeAlias = Literal["memory", "redis"]


class _Region(BaseModel):
    """一块配置区域的公共底：冻结（配置读出来就不该被改），缺项按字段默认值补。"""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)
    #: 已废弃的键 -> 现在的替代项；写到配置里直接报错（见 :func:`_load`），不静默忽略
    legacy_keys: ClassVar[dict[str, str]] = {}


# --------------------------------------------------------------------------- 区域：[app]
class AppSettings(_Region):
    """``[app]``：进程级设置，和日志、数据库无关。"""

    name: str = "nacho"  # 进程名，同时是日志核心名（子实例都以它为前缀）
    debug: bool = False  # 调试模式：True 时忽略 level 直接开到 DEBUG


# ---------------------------------------------------------------------- 区域：[database]
class DatabaseSettings(_Region):
    """一份数据库连接参数：``[database]`` 公共节，以及覆盖它之后的日志出口连接。

    两种 driver 用的键不一样，没用到的那几个保持默认值即可（模板里也是这么注释的）：
    sqlite 只看 ``path``，mariadb 看 ``host`` / ``port`` / ``user`` / ``password`` /
    ``database``。
    """

    driver: Driver = "sqlite"  # sqlite / mariadb
    path: ConfigPath = BASE_DIR / "logs" / "nacho.db"  # sqlite 用
    # mariadb 用：服务地址与账号
    host: str = "127.0.0.1"
    port: int = Field(default=3306, ge=1, le=65535, description="1-65535 的端口")
    user: str = "nacho"
    password: str = ""  # config.toml 不入库，密码写这里不会进 git
    database: str = "nacho"  # 库名（要事先建好）


# ----------------------------------------------------------------------- 区域：[logging]
class FileLogSettings(_Region):
    """``[logging.file]``：本地文件出口 —— **一个目录、按天分片**。

    片名是 ``<前缀>-<YYYY-MM-DD>[.<序号>].log``（``nacho-2026-09-28.log``）：跨天换日期片，
    同一天里写满 ``rotate_minutes``（或顶到 ``max_bytes``）就加序号再开一片。模块的区分靠
    记录里的 ``logger_name`` 字段，不靠文件 —— 所以整进程只有这一份文件出口。

    旧版的 ``path`` / ``backup_count`` 由 ``dir`` / ``keep_days`` 取代，写在配置里会**报错**
    （见 :func:`_load`、:attr:`legacy_keys`）：静默忽略的代价是「配了没生效」—— 老的
    ``logs/nacho.log`` 不再被读、也不会被清理，页面上只会看到「本机文件」一片空白。
    """

    #: 旧键 -> 替代项：``path`` 那时是单个文件，现在是目录；``backup_count`` 是「留几份历史」，
    #: 现在按天数留
    legacy_keys: ClassVar[dict[str, str]] = {"path": "dir", "backup_count": "keep_days"}

    enabled: bool = True  # false 就只有控制台
    dir: ConfigPath = BASE_DIR / "logs"  # 放片的目录
    prefix: str = ""  # 片名前缀；留空 = 用 [app].name
    rotate_minutes: float = Field(
        default=60.0, ge=0, description="不小于 0 的分钟数（0 = 只按天分片）"
    )
    max_bytes: int = Field(
        default=20 * 1024 * 1024, ge=0, description="不小于 0 的整数（单片上限兜底，0 = 不限）"
    )
    keep_days: int = Field(default=14, ge=0, description="不小于 0 的天数（0 = 不清理）")
    search_days: int = Field(
        default=2, ge=0, description="不小于 0 的天数（检索默认往回找几天，0 = 不限）"
    )
    buffer_size: int = Field(default=200, ge=1, description="不小于 1 的整数")
    flush_interval: float = Field(default=2.0, gt=0, description="大于 0 的秒数")


class DatabaseLogSettings(_Region):
    """``[logging.database]``：把日志再落一份到数据库的出口。

    这里只放日志出口特有的项；连接项在 :attr:`connection`（覆盖之后才知道最终值）。
    表名不在这里配：日志表固定叫 ``logs``，和 ``users`` / ``auth_sessions`` 一样由代码声明。
    """

    enabled: bool = False  # 默认不落库：要多一路数据库出口就置 true
    buffer_size: int = Field(default=500, ge=1, description="不小于 1 的整数")
    flush_interval: float = Field(default=5.0, gt=0, description="大于 0 的秒数")
    #: 这个出口真正连的库：[logging.database] 的连接项逐项盖在 [database] 上之后的结果；
    #: 没单独配就是 [database] 那一份（不是配置项，是算出来的）
    connection: DatabaseSettings = Field(default_factory=DatabaseSettings)


class QueueSettings(_Region):
    """``[logging.queue]``：日志异步队列与分发器。"""

    maxsize: int = Field(default=10_000, ge=1, description="不小于 1 的整数")
    dispatch_batch_size: int = Field(default=200, ge=1, description="不小于 1 的整数")
    dispatch_timeout: float = Field(default=0.2, gt=0, description="大于 0 的秒数")


class LoggingSettings(_Region):
    """``[logging]``：日志总控；下挂三条子区域，一项在哪块看名字就知道。"""

    level: Level = LogLevel.INFO  # DEBUG / INFO / WARNING / ERROR / CRITICAL
    console: bool = True  # 是否往控制台输出（服务端 / 无人值守时置 false）
    console_color: bool = True  # 控制台是否按级别上 ANSI 颜色
    console_level: OptionalLevel = None  # 控制台单独的级别；None = 跟随 level
    file: FileLogSettings = Field(default_factory=FileLogSettings)
    database: DatabaseLogSettings = Field(default_factory=DatabaseLogSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)


def _load_logging(section: dict[str, object], public_db: dict[str, object]) -> LoggingSettings:
    """读 ``[logging]``：本节开关 + file / database / queue 三条子区域。

    ``public_db`` 是 ``[database]`` 公共节；数据库出口的连接项按「专用 > 公共 > 默认」
    逐项覆盖，最后落到 ``logging.database.connection``。
    """
    # 连接项两层覆盖：[logging.database] 专用项 > [database] 公共项 > 字段默认值
    merged, origin = _overlay(
        ("database", public_db), ("logging.database", _section(section, "database"))
    )

    def where(key: str) -> str:
        """这一项最终写在哪个节里（覆盖之后光看键名看不出来）。"""
        return origin.get(key, f"logging.database.{key}")

    connection = _load(DatabaseSettings, merged, where)
    log_db = _load(DatabaseLogSettings, merged, where).model_copy(
        update={"connection": connection}
    )
    # 本节自己的项：子区域（值是表格）各自由 _load 用自己的出处校验，报错才指得准
    own: dict[str, object] = {k: v for k, v in section.items() if not isinstance(v, dict)}
    return _load(LoggingSettings, own, _where_in("logging")).model_copy(
        update={
            "file": _load(FileLogSettings, _section(section, "file"), _where_in("logging.file")),
            "database": log_db,
            "queue": _load(QueueSettings, _section(section, "queue"), _where_in("logging.queue")),
        }
    )


# ------------------------------------------------------------------------- 区域：[cache]
class RedisCacheSettings(_Region):
    """``[cache.redis]``：连一份 Redis 要的参数（只有 ``backend = "redis"`` 时才用）。"""

    host: str = "127.0.0.1"
    port: int = Field(default=6379, ge=1, le=65535, description="1-65535 的端口")
    db: int = Field(default=0, ge=0, description="不小于 0 的整数")
    username: str = ""  # Redis 6+ 的 ACL 用户名；空 = 默认用户
    password: str = ""  # config.toml 不入库，密码写这里不会进 git
    socket_timeout: float = Field(default=5.0, gt=0, description="大于 0 的秒数")
    # TCP 建连超时（秒）：不设的话走系统默认，连不上时实测要干等二十几秒
    socket_connect_timeout: float = Field(default=5.0, gt=0, description="大于 0 的秒数")
    # 建连失败后的重试次数：驱动默认 10 次指数退避，连不上时会白等很久；默认只补一次
    connect_retries: int = Field(default=1, ge=0, description="不小于 0 的整数")
    max_connections: int = Field(default=10, ge=1, description="不小于 1 的整数")


class CacheSettings(_Region):
    """``[cache]``：缓存总控 —— 用哪个后端、怎么处理连不上；连接参数在 ``[cache.redis]``。

    默认 ``backend = "memory"``（进程内存），所以**不装 Redis、不写这一节也能用**；
    换成 ``"redis"`` 之后连不上时默认**当场报错**（报错信息会提示去配置里改），只有显式
    打开 ``fallback_to_memory`` 才退回内存（记 warning）。两项都只影响后端怎么建，不影响
    上层看到的接口。
    """

    backend: CacheBackendName = "memory"  # memory（进程内存）/ redis
    namespace: str = "nacho"  # Redis 上的键前缀（共用实例时隔离）
    default_ttl: float = Field(default=0.0, ge=0, description="不小于 0 的秒数")
    fallback_to_memory: bool = False  # Redis 连不上时默认当场报错；true = 退回内存（记 warning）
    sweep_interval: float = Field(default=30.0, gt=0, description="大于 0 的秒数")
    redis: RedisCacheSettings = Field(default_factory=RedisCacheSettings)

    @field_validator("namespace")
    @classmethod
    def _check_namespace(cls, value: str) -> str:
        """命名空间要拼进键名（``<namespace>:key``），不能空着、也不能自带空格或冒号。"""
        if not value.strip() or " " in value or ":" in value:
            raise ValueError(f"命名空间要非空且不含空格与冒号，收到 {value!r}")
        return value


def _load_cache(section: dict[str, object]) -> CacheSettings:
    """读 ``[cache]``：本节各项 + ``[cache.redis]`` 连接子区域。

    和 ``[logging]`` 一样，子区域（值是表格）交回各自的出处校验，报错才指得准。
    """
    own: dict[str, object] = {k: v for k, v in section.items() if not isinstance(v, dict)}
    redis = _load(RedisCacheSettings, _section(section, "redis"), _where_in("cache.redis"))
    return _load(CacheSettings, own, _where_in("cache")).model_copy(
        update={"redis": redis}
    )


# --------------------------------------------------------------------------- 区域：[api]
class ApiSettings(_Region):
    """``[api]``：接口层对外怎么挂 —— 监听地址、路由前缀、令牌有效期、访问日志。

    监听地址（``host`` / ``port``）是接口层 HTTP 服务随主程序由 uvicorn 起时绑定的；
    其余几项只影响接口层自己：前缀决定登录接口挂在哪（``<prefix>/auth/login``），
    ``token_ttl`` 是**访问令牌**的有效期，``remember_ttl`` 是勾了「记住设备」才发的
    **长期令牌**有效期，``access_log`` 决定要不要逐条记访问日志，``trust_proxy``
    决定要不要认 ``X-Forwarded-For`` 里的客户端 ip（没挂在可信代理后面就别开——
    那个头客户端自己就能伪造）。

    头像那两项只影响个人设置的默认存储：``avatar_dir`` 是**存放目录**（默认实现的本地
    目录，相对路径按项目根目录解析；换成对象存储 / 落库时它就没用了），``avatar_max_bytes``
    是单个头像的字节上限（超了回 413）。

    ``token_ttl`` / ``remember_ttl`` 填 ``0`` 表示**永不过期**。注意 ``token_ttl``
    是**滑动**的：每次带令牌的请求都会把有效期往后延，所以它是"闲置多久算掉线"，
    而不是"登录后最多能用多久"。
    """

    host: str = "127.0.0.1"  # HTTP 服务监听地址
    port: int = Field(default=18080, ge=1, le=65535, description="1-65535 的端口")
    prefix: str = "/api"  # 路由前缀（要 / 开头；结尾的 / 会被去掉）
    token_ttl: float = Field(
        default=7200.0, ge=0, description="访问令牌滑动有效期（秒）；0 = 永不过期"
    )
    remember_ttl: float = Field(
        default=2_592_000.0, ge=0, description="长期令牌有效期（秒）；0 = 永不过期"
    )
    access_log: bool = True  # 逐条记访问日志（方法 / 路径 / 状态码 / 耗时）
    trust_proxy: bool = False  # 信任 X-Forwarded-For 里的客户端 ip（要挂在可信代理后面才开）
    # 头像存放目录（默认实现的本地目录；相对路径按项目根目录解析。换存储实现时它就没用了）
    avatar_dir: ConfigPath = BASE_DIR / "data" / "avatars"
    # 头像大小上限（字节）：超了接口层回 413。默认 2 MiB
    avatar_max_bytes: int = Field(
        default=2 * 1024 * 1024, ge=1, description="不小于 1 的字节数"
    )

    @field_validator("prefix")
    @classmethod
    def _check_prefix(cls, value: str) -> str:
        """前缀要拼进路由：得以 ``/`` 开头、不含空白；结尾的 ``/`` 去掉，免得拼出双斜杠。"""
        if not value.startswith("/") or any(char.isspace() for char in value):
            raise ValueError(f"路由前缀要以 / 开头且不含空白，收到 {value!r}")
        return value.rstrip("/") or "/"


# ------------------------------------------------------------------------ 区域：[onebot]
class OneBotSettings(_Region):
    """``[onebot]``：OneBot 反向 WS 接入 —— 框架当服务端，等 OneBot 实现连进来。

    监听地址（``host`` / ``port`` / ``path``）是 ``onebot.py`` 起的 WS 服务绑定的；OneBot 实现
    （go-cqhttp / NapCat / LLOneBot …）那边把「反向 WS」地址配成 ``ws://<host>:<port><path>``。
    ``access_token`` 留空表示不校验；非空时握手要带 ``Authorization: Bearer <token>`` 或
    ``?access_token=<token>``。``action_timeout`` 是发出一个动作后等回应的超时。
    """

    host: str = "127.0.0.1"  # WS 服务监听地址
    port: int = Field(default=6700, ge=1, le=65535, description="1-65535 的端口")
    path: str = "/"  # 只接受该路径的连接
    action_timeout: float = Field(default=30.0, gt=0, description="大于 0 的秒数")

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        """路径要拼进 WS 地址：得从 ``/`` 开始。"""
        if not value.startswith("/"):
            raise ValueError(f"WS 路径要以 / 开头，收到 {value!r}")
        return value


# ------------------------------------------------------------------------ 区域：[kook]
class KookSettings(_Region):
    """``[kook]``：Kook 正向 WS 接入 —— 框架当客户端，主动连 Kook 网关（用 Bot Token 鉴权）。

    与 OneBot 相反：这里配的是「连哪个网关 / 用什么凭证」，不是「监听哪个端口」。
    ``token`` 留空 = 没配，不接入 Kook（装配层据此跳过建适配器）。
    """

    gateway: str = ""  # 网关地址：留空 = 连接前走 gateway/index 动态获取（推荐）
    token: str = ""  # Bot Token（Kook 开放平台签发；留空 = 不接入）
    secret_key: str = ""  # Bot Token 落库加密的密钥（kook 凭证行加密用；留空则无法存 kook 凭证）
    heartbeat_interval: float = Field(default=30.0, gt=0, description="大于 0 的秒数")
    #: 心跳抖动（秒）：官方口径 30 秒 + rand(-5, +5)，别让所有机器人同一时刻打心跳
    heartbeat_jitter: float = Field(default=5.0, ge=0, description="不小于 0 的秒数")
    action_timeout: float = Field(default=30.0, gt=0, description="大于 0 的秒数")
    reconnect_interval: float = Field(default=2.0, gt=0, description="大于 0 的秒数")
    reconnect_max_interval: float = Field(default=60.0, gt=0, description="大于 0 的秒数")
    rest_min_interval: float = Field(default=0.2, gt=0, description="大于 0 的秒数")
    rest_max_retries: int = Field(default=3, ge=0, description="不小于 0 的次数")


# --------------------------------------------------------------------------- 整份设置
class Settings(_Region):
    """一份设置：字段就是配置文件里的区域，一一对应；最后一项是元信息，不是配置项。

    每个字段的默认值 = 配置里没写这一节（项）时的取值，键名同 ``config.toml.example``。
    """

    app: AppSettings = Field(default_factory=AppSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    onebot: OneBotSettings = Field(default_factory=OneBotSettings)
    kook: KookSettings = Field(default_factory=KookSettings)

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

        #(TODO)api数据库也要加入默认公共，可以被覆盖功能
        # 一块区域一次校验：读哪一节、错了报哪一项，都在那一块里说清
        public_db = _section(data, "database")
        return cls(
            app=_load(AppSettings, _section(data, "app"), _where_in("app")),
            api=_load(ApiSettings, _section(data, "api"), _where_in("api")),
            cache=_load_cache(_section(data, "cache")),
            database=_load(DatabaseSettings, public_db, _where_in("database")),
            logging=_load_logging(_section(data, "logging"), public_db),
            onebot=_load(OneBotSettings, _section(data, "onebot"), _where_in("onebot")),
            kook=_load(KookSettings, _section(data, "kook"), _where_in("kook")),
            config_path=config_path,
        )
