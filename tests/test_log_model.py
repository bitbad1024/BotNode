"""``nacho/core/logger/models.py`` 单元测试。

命名约定：``tests/test_log_<模块>.py`` 对应 ``nacho/core/logger/<模块>.py``；
``log_`` 前缀用于指明是「日志框架」的哪个模块，避免与框架里其它 ``models``
模块的同名测试混淆。

对应提交 ``8a3aa12 feat(logger): 新增日志数据模型 LogLevel 与 LogRecord``，
并覆盖其后对 ``LogLevel.parse`` 签名、``Any`` 清理等调整后的行为。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import pytest

from nacho.core.logger.models import LogLevel, LogRecord, normalize_timestamp

#: ``2020-01-01T00:00:00Z`` 对应的 Unix 时间戳，多个用例共用
EPOCH_2020 = 1577836800.0
UTC = timezone.utc
DATETIME_TEXT = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$")


class TestLogLevel:
    """日志级别：数值对齐标准库、可解析、可比较。"""

    @pytest.mark.parametrize(
        ("level", "expected"),
        [
            (LogLevel.DEBUG, logging.DEBUG),
            (LogLevel.INFO, logging.INFO),
            (LogLevel.WARNING, logging.WARNING),
            (LogLevel.ERROR, logging.ERROR),
            (LogLevel.CRITICAL, logging.CRITICAL),
        ],
    )
    def test_values_align_with_stdlib(self, level: LogLevel, expected: int) -> None:
        assert int(level) == expected

    def test_ordering_and_label(self) -> None:
        assert LogLevel.DEBUG < LogLevel.INFO < LogLevel.WARNING < LogLevel.ERROR
        assert LogLevel.ERROR < LogLevel.CRITICAL
        assert LogLevel.WARNING.label == "WARNING"

    def test_parse_returns_same_level_object(self) -> None:
        assert LogLevel.parse(LogLevel.ERROR) is LogLevel.ERROR

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("DEBUG", LogLevel.DEBUG),
            ("debug", LogLevel.DEBUG),
            ("  Warning  ", LogLevel.WARNING),
            ("error", LogLevel.ERROR),
        ],
    )
    def test_parse_name_is_case_and_space_insensitive(self, raw: str, expected: LogLevel) -> None:
        assert LogLevel.parse(raw) is expected

    def test_parse_unknown_name_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="无法识别的日志级别"):
            LogLevel.parse("TRACE")

    def test_parse_rejects_bare_number(self) -> None:
        """级别只接受 ``LogLevel`` 或名字，裸数字已不再支持。"""
        with pytest.raises((AttributeError, TypeError, ValueError)):
            LogLevel.parse(20)  # type: ignore[arg-type]


class TestNormalizeTimestamp:
    """时间表示归一化：统一成 Unix 时间戳（秒）。"""

    def test_none_returns_none(self) -> None:
        assert normalize_timestamp(None) is None

    @pytest.mark.parametrize("raw", [EPOCH_2020, int(EPOCH_2020), "1577836800"])
    def test_numeric_representations(self, raw: object) -> None:
        assert normalize_timestamp(raw) == EPOCH_2020  # type: ignore[arg-type]

    def test_fractional_seconds_are_kept(self) -> None:
        assert normalize_timestamp(1.5) == 1.5
        assert normalize_timestamp("1.5") == 1.5

    def test_naive_datetime_is_treated_as_utc(self) -> None:
        assert normalize_timestamp(datetime(2020, 1, 1)) == EPOCH_2020

    def test_aware_datetime(self) -> None:
        assert normalize_timestamp(datetime(2020, 1, 1, tzinfo=UTC)) == EPOCH_2020
        offset = timezone(timedelta(hours=8))
        assert normalize_timestamp(datetime(2020, 1, 1, 8, tzinfo=offset)) == EPOCH_2020

    @pytest.mark.parametrize(
        "raw",
        [
            "2020-01-01T00:00:00Z",
            "2020-01-01T00:00:00+00:00",
            "2020-01-01T08:00:00+08:00",
        ],
    )
    def test_iso_strings(self, raw: str) -> None:
        assert normalize_timestamp(raw) == EPOCH_2020

    @pytest.mark.parametrize("raw", ["", "   ", "not-a-time"])
    def test_invalid_text_raises_value_error(self, raw: str) -> None:
        with pytest.raises(ValueError, match="无法解析时间"):
            normalize_timestamp(raw)


class TestLogRecord:
    """日志记录：默认值、派生属性、序列化。"""

    def test_defaults(self) -> None:
        before = time.time()
        record = LogRecord(message="hello")
        assert record.level is LogLevel.INFO
        assert record.logger_name == ""
        assert record.extra == {}
        assert record.exc_text is None
        assert before <= record.timestamp <= time.time()
        assert len(record.record_id) == 32
        assert int(record.record_id, 16) >= 0  # 合法的十六进制字符串

    def test_mutable_state_is_not_shared_between_records(self) -> None:
        first = LogRecord(message="a")
        second = LogRecord(message="b")
        first.extra["k"] = "v"
        assert second.extra == {}
        assert first.record_id != second.record_id

    def test_level_is_parsed_on_construction(self) -> None:
        record = LogRecord(message="m", level="error")  # type: ignore[arg-type]
        assert record.level is LogLevel.ERROR

    def test_dataclass_uses_slots(self) -> None:
        record = LogRecord(message="m")
        with pytest.raises(AttributeError):
            record.unknown = 1  # type: ignore[attr-defined]

    def test_datetime_text_format(self) -> None:
        record = LogRecord(message="m", timestamp=EPOCH_2020 + 0.123456)
        text = record.datetime_text
        assert DATETIME_TEXT.match(text)
        assert text.endswith(".123")  # 毫秒截断，不四舍五入

    def test_to_dict_is_json_serializable(self) -> None:
        record = LogRecord(message="机器人启动", level=LogLevel.ERROR, extra={"robot_id": "r-001"})
        data = record.to_dict()
        assert set(data) == {
            "record_id",
            "timestamp",
            "level",
            "logger_name",
            "message",
            "extra",
            "exc_text",
        }
        assert data["level"] == "ERROR"  # 存级别名，便于落库与检索
        assert json.loads(json.dumps(data, ensure_ascii=False))["message"] == "机器人启动"

    def test_to_dict_does_not_leak_internal_state(self) -> None:
        record = LogRecord(message="m")
        snapshot = record.to_dict()
        assert isinstance(snapshot["extra"], dict)
        snapshot["extra"]["injected"] = True  # type: ignore[index]
        assert record.extra == {}


class TestLogRecordFromDict:
    """反序列化边界：外部数据不可信，类型不对要降级而不是抛异常。"""

    def test_round_trip(self) -> None:
        record = LogRecord(
            message="机器人执行失败",
            level=LogLevel.ERROR,
            logger_name="nacho.robot",
            timestamp=EPOCH_2020,
            extra={"robot_id": "r-001"},
            exc_text="Traceback ...",
        )
        assert LogRecord.from_dict(record.to_dict()).to_dict() == record.to_dict()

    def test_round_trip_through_json(self) -> None:
        record = LogRecord(message="m", extra={"n": 1, "s": "x"})
        restored = LogRecord.from_dict(json.loads(json.dumps(record.to_dict())))
        assert restored.to_dict() == record.to_dict()

    def test_missing_fields_fall_back_to_defaults(self) -> None:
        record = LogRecord.from_dict({})
        assert record.message == ""
        assert record.logger_name == ""
        assert record.level is LogLevel.INFO
        assert record.extra == {}
        assert record.exc_text is None
        assert record.record_id  # 自动补一个 id
        assert record.timestamp > 0

    def test_tolerates_wrongly_typed_fields(self) -> None:
        record = LogRecord.from_dict(
            {
                "message": "m",
                "level": LogLevel.WARNING,
                "timestamp": "1577836800",
                "extra": None,
                "exc_text": 123,
            }
        )
        assert record.level is LogLevel.WARNING
        assert record.timestamp == EPOCH_2020
        assert record.extra == {}
        assert record.exc_text is None

    def test_non_mapping_extra_is_dropped(self) -> None:
        assert LogRecord.from_dict({"extra": "not-a-mapping"}).extra == {}
        assert LogRecord.from_dict({"extra": [1, 2]}).extra == {}

    def test_extra_is_copied_not_aliased(self) -> None:
        source: dict[str, object] = {"extra": {"a": 1}}
        record = LogRecord.from_dict(source)
        record.extra["b"] = 2
        assert source["extra"] == {"a": 1}


class TestLogRecordMatches:
    """检索条件复用逻辑（本地处理机与数据库处理机共用）。"""

    @pytest.fixture
    def record(self) -> LogRecord:
        return LogRecord(
            message="机器人执行第 1 步",
            level=LogLevel.INFO,
            logger_name="nacho.robot",
            timestamp=EPOCH_2020,
            extra={"robot_id": "r-001"},
        )

    def test_no_condition_matches(self, record: LogRecord) -> None:
        assert record.matches()

    def test_level_is_a_lower_bound(self, record: LogRecord) -> None:
        assert record.matches(level="DEBUG")
        assert record.matches(level=LogLevel.INFO)
        assert not record.matches(level="WARNING")

    def test_logger_name_must_equal(self, record: LogRecord) -> None:
        assert record.matches(logger_name="nacho.robot")
        assert not record.matches(logger_name="nacho.api")

    def test_time_range_accepts_datetime_and_text(self, record: LogRecord) -> None:
        assert record.matches(start=datetime(2019, 12, 31, tzinfo=UTC))
        assert record.matches(end="2020-01-01T00:00:01Z")
        assert not record.matches(start="2020-01-02T00:00:00Z")
        assert not record.matches(end=EPOCH_2020 - 1)

    def test_query_is_case_insensitive_and_searches_extra(self, record: LogRecord) -> None:
        assert record.matches(query="执行")
        assert record.matches(query="R-001")
        assert not record.matches(query="不存在的关键字")
