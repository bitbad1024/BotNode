"""日志数据模型：日志级别与日志记录。

日志记录（:class:`LogRecord`）是队列、日志系统基类与各日志处理机之间
传递的最小单元，必须是可序列化的（可转成 ``dict``），这样才能被推入
消息队列，也才能被数据库日志处理机直接落库。

例外只有一个：:attr:`LogRecord.targets`（这条记录要投给哪些出口）。它是**活对象引用**，
不属于日志内容，因此刻意不进 :meth:`LogRecord.to_dict` / :meth:`LogRecord.from_dict`。
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from typing import TYPE_CHECKING, Protocol, cast, TypeAlias

if TYPE_CHECKING:  # 只为类型标注：运行期引入会成环（processors.base 反过来 import 本模块）
    from .processors.base import BaseLogProcessor


class FilterLike(Protocol):
    """目标上那个过滤器的形状：只要有 ``match(record)`` 就算。

    刻意只认形状不认 :class:`~tickneko.core.logger.filters.LogFilter` 这个类：过滤器在自己
    的模块里，反过来 import 本模块，按类引就成环了。
    """

    def match(self, record: LogRecord) -> bool: ...

#: 允许的时间表示形式：时间戳 / ISO 字符串 / datetime / None
TimestampLike: TypeAlias = int | float | str | datetime | None

class LogLevel(IntEnum):
    """日志级别，数值对齐标准库 ``logging``，便于互通。"""

    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40
    CRITICAL = 50

    @classmethod
    def parse(cls, value: LogLevel | str) -> LogLevel:
        """把日志级别对象 / 级别名统一解析成 :class:`LogLevel`。"""
        if isinstance(value, LogLevel):
            return value
        key: str = value.strip().upper()
        if key in cls.__members__:
            return cls[key]
        raise ValueError(f"无法识别的日志级别: {value!r}")

    @property
    def label(self) -> str:
        return self.name


def normalize_timestamp(value: TimestampLike) -> float | None:
    """把多种时间表示统一成 Unix 时间戳（秒）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment: datetime = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    
    text: str = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"无法解析时间: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class Target:
    """一个**投递目标**：处理机 + 挂在它上面的过滤器 + 投放优先级。

    ``bind(targets=[...])`` 之后这条日志投给谁就在**写入那一刻**定了，不用等分发时
    再按名字反查实例表 —— 名字表因此可以彻底不存在。

    :param log_filter: 只放行通过它的记录；``None`` = 这个出口全收。
    :param level: 出口级最低级别；低于它的记录直接跳过，``None`` = 全收。
        与 ``log_filter`` 分工：level 是数值门槛，log_filter 是自定义判定。
    :param priority: 投放顺序，**小的先投**：先落库还是先写文件由它说了算，不必依赖
        ``bind`` 里写 targets 的先后。
    """

    processor: BaseLogProcessor
    log_filter: "FilterLike | None" = None
    level: LogLevel | None = None
    priority: int = 0

    def __post_init__(self) -> None:
        """把 ``level`` 统一解析成 :class:`LogLevel`（挂载时允许传级别名）。"""
        if self.level is not None:
            object.__setattr__(self, "level", LogLevel.parse(self.level))


@dataclass(slots=True)
class LogRecord:
    """一条结构化日志记录。"""

    message: str
    level: LogLevel = LogLevel.INFO
    logger_name: str = ""
    timestamp: float = field(default_factory=time.time)
    record_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    extra: dict[str, object] = field(default_factory=dict)
    exc_text: str | None = None
    #: 这条日志的**所有者**：谁的操作就填谁——api 层填登录用户 id，ws 层填那条连接的归属。
    #: 空串 = **公共所有者**：没有明确归属的日志（启动、框架自身、握手被拒、定时任务……）。
    owner_id: str = ""
    #: 落库那份的**自增序号**（插入顺序，由数据库分配，见
    #: :class:`~tickneko.db.LogTable`）：检索按它倒序。
    #:
    #: 为什么不能只靠时间戳：同一次调用里写下的几条（甚至一整批）时间戳常常一模一样 ——
    #: 秒级浮点的分辨率不够，Windows 上 ``time.time()`` 的粒度更粗（约 15ms）。那时谁先谁后
    #: 只有序号知道，翻页也不会因为「同刻几条顺序随机」而漏记 / 重记。
    #:
    #: 还没落库（以及不留存历史的出口：控制台 / 文件）是 ``0`` —— 那一路没有「第几条」。
    seq: int = 0
    #: **路由信息，不属于日志内容**：这条记录在写入时就已经定好要投给哪些出口。
    #: ``None`` = 没定（由分发器按 ``logger_name`` 查实例表兜底）。
    #: 刻意不进 :meth:`to_dict` / :meth:`from_dict`：它装的是活的处理机对象，不是这条
    #: 日志的内容，别让它跟着序列化一路跑到磁盘 / 库里去。
    targets: "tuple[Target, ...] | None" = None

    def __post_init__(self) -> None:
        self.level = LogLevel.parse(value=self.level)

    @property
    def datetime_text(self) -> str:
        """可读时间文本，精确到毫秒。"""
        return datetime.fromtimestamp(self.timestamp).strftime(format="%Y-%m-%d %H:%M:%S.%f")[:-3]

    def to_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "timestamp": self.timestamp,
            "level": self.level.label,
            "logger_name": self.logger_name,
            "message": self.message,
            "extra": dict[str, object](self.extra),
            "exc_text": self.exc_text,
            "owner_id": self.owner_id,
            "seq": self.seq,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> LogRecord:
        raw_extra: object | None = data.get("extra")
        raw_exc_text: object | None = data.get("exc_text")
        raw_owner: object | None = data.get("owner_id")
        return cls(
            message=cast(str, data.get("message", "")),
            level=LogLevel.parse(value=cast(LogLevel | str, data.get("level", LogLevel.INFO))),
            logger_name=cast(str, data.get("logger_name", "")),
            timestamp=float(cast(float | str | int, data.get("timestamp", time.time()))),
            record_id=cast(str, data.get("record_id") or uuid.uuid4().hex),
            extra=(
                dict[str, object](cast(Mapping[str, object], raw_extra))
                if isinstance(raw_extra, Mapping)
                else {}
            ),
            exc_text=raw_exc_text if isinstance(raw_exc_text, str) else None,
            owner_id=raw_owner if isinstance(raw_owner, str) else "",
            seq=int(cast(int | float | str, data.get("seq", 0))),
        )

    def matches(
        self,
        *,
        query: str | None = None,
        level: LogLevel | str | None = None,
        start: TimestampLike = None,
        end: TimestampLike = None,
        logger_name: str | None = None,
        owner_id: str | None = None,
    ) -> bool:
        """判断当前记录是否满足检索条件（供各处理机复用）。

        ``owner_id`` 是**精确匹配**：给 ``None`` 表示不限所有者；给空串就是「只要公共的」。
        """
        if level is not None and self.level < LogLevel.parse(value=level):
            return False
        if logger_name is not None and self.logger_name != logger_name:
            return False
        if owner_id is not None and self.owner_id != owner_id:
            return False

        start_ts: float | None = normalize_timestamp(value=start)
        if start_ts is not None and self.timestamp < start_ts:
            return False

        end_ts: float | None = normalize_timestamp(value=end)
        if end_ts is not None and self.timestamp > end_ts:
            return False

        if query:
            needle: str = query.lower()
            haystack: str = f"{self.message} {self.extra} {self.exc_text or ''}".lower()
            if needle not in haystack:
                return False
        return True


@dataclass(slots=True)
class LogSearchResult:
    """一次检索的结果：**这一页**的记录 + 同条件下的**命中总数**。

    总数只跟筛选条件有关，与 ``limit`` / ``offset`` 无关 —— 所以它跟 ``search`` 一起回来：
    翻页要算总页数，调用方不必再拿同一套条件去问一次「有多少条」。

    ``total`` 的口径与 ``records`` 略有差别（多出口时）：各出口独立计数后相加、**不去重**，
    同一个 ``record_id`` 落在两个出口就会被数两次；默认只查单个出口（如落库那份）时即精确总数。
    """

    records: list[LogRecord] = field(default_factory=list)
    total: int = 0
