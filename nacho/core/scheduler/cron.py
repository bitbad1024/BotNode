"""cron 表达式：五段「分 时 日 月 周」的解析与触发时间计算。

支持的语法（每个字段内）::

    *        任意值
    5        单值
    a-b      区间
    a,b,c    列表（各项还可以是区间）
    */n      步进（全域每 n 个）；也可与区间组合：a-b/n、5/n（从 5 到上限每 n 个）

字段范围：分钟 0-59、小时 0-23、日 1-31、月 1-12、星期 0-6（0 与 7 都是周日）。

语义约定：日 / 周遵循标准 vixie cron —— 两者都被限制（不是 ``*``）时取 OR，
否则取 AND。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import override

#: next_after 的搜索上限：一年内还没有触发点就当死配置报错
_SEARCH_LIMIT = timedelta(days=366)

#: 单个字段里的一项：``*`` / 数字 / 数字区间，后面可带 ``/步进``
_ITEM_RE = re.compile(r"^(\*|\d+(?:-\d+)?)(?:/([1-9]\d*))?$")

#: 字段名 -> 取值下界与上界
_FIELD_RANGES: dict[str, tuple[int, int]] = {
    "minute": (0, 59),
    "hour": (0, 23),
    "day": (1, 31),
    "month": (1, 12),
    "weekday": (0, 7),  # 7 当周日，解析完映射成 0
}


class CronError(ValueError):
    """cron 表达式不合法：语法错、数值越界、字段数不对等，报错带字段名和原因。"""


@dataclass(frozen=True)
class CronField:
    """单个字段的命中集合，如 ``*/5`` 的分钟 = {0, 5, 10, ..., 55}。"""

    values: frozenset[int]
    #: 字段名（minute/hour/...），报错提示用
    field: str
    #: 字段的合法取值范围，校验用
    low: int
    high: int
    #: 解析前的原文，``__str__`` 原样还原（逗号列表可能撞上步进数列，重算反而会失真）
    text: str

    @classmethod
    def parse(cls, text: str, field: str, low: int, high: int) -> "CronField":
        """解析一个字段；``*``、单值、区间、列表、步进，越界或语法错抛 :class:`CronError`。"""
        values: set[int] = set()
        for part in text.split(","):
            part = part.strip()
            matched = _ITEM_RE.match(part)
            if matched is None:
                raise CronError(f"cron {field} 字段看不懂 {part!r}（整段是 {text!r}）")
            rng, step_text = matched.groups()
            step = int(step_text) if step_text else 1
            if rng == "*":
                start, end = low, high
            elif "-" in rng:
                start_text, end_text = rng.split("-")
                start, end = int(start_text), int(end_text)
                if start > end:
                    raise CronError(f"cron {field} 字段区间起点要 <= 终点，收到 {part!r}")
            else:
                start = int(rng)
                # vixie 语义：单值带步进 = 从该值到字段上限
                end = high if step_text else start
            if start < low or end > high:
                raise CronError(f"cron {field} 字段取值要 {low}-{high}，收到 {part!r}")
            values.update(range(start, end + 1, step))
        return cls(frozenset(values), field, low, high, text)

    def contains(self, value: int) -> bool:
        """某个具体取值是否命中本字段。"""
        return value in self.values

    @override
    def __str__(self) -> str:
        """还原成解析前的原文（如 ``*/5``、``9-17``、``1,15``）。"""
        return self.text


@dataclass(frozen=True)
class CronExpr:
    """一份解析好的 cron 表达式，不可变；``next_after`` 是调度循环唯一要调的方法。"""

    minute: CronField
    hour: CronField
    day: CronField
    month: CronField
    weekday: CronField

    @classmethod
    def parse(cls, text: str) -> "CronExpr":
        """解析 ``"*/5 * * * *"`` 这类表达式；字段数不对、任一字段不合法都抛 :class:`CronError`。"""
        parts = text.split()
        names = ("minute", "hour", "day", "month", "weekday")
        if len(parts) != len(names):
            raise CronError(
                f"cron 表达式要 {len(names)} 段（分 时 日 月 周），收到 {len(parts)} 段：{text!r}"
            )
        fields: dict[str, CronField] = {}
        for name, part in zip(names, parts):
            low, high = _FIELD_RANGES[name]
            parsed = CronField.parse(part, name, low, high)
            if name == "weekday":  # 7 也是周日，统一收成 0
                parsed = CronField(
                    frozenset(0 if value == 7 else value for value in parsed.values),
                    name,
                    low,
                    high,
                    str(parsed),
                )
            fields[name] = parsed
        return cls(**fields)

    def next_after(self, after: datetime) -> datetime:
        """after 之后（不含 after 本身）第一次命中的时刻（秒 / 微秒清零）。

        找不到一年内的命中点就抛 :class:`CronError`（如 2 月 31 日这类死配置）。
        """
        moment = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
        limit = after + _SEARCH_LIMIT
        while moment < limit:
            if not self.month.contains(moment.month):
                # 整月都不命中：直接跳到下个月 1 号零点
                moment = (moment.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(
                    day=1
                )
                continue
            if not self._day_matches(moment):
                # 这一天不命中：跳到明天零点
                moment = (moment + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if moment.hour in self.hour.values:
                if moment.minute in self.minute.values:
                    return moment  # 月 / 日 / 时 / 分全中：moment 本身严格晚于 after
                nxt = self._next_value(self.minute.values, moment.minute)
                if nxt is not None:
                    return moment.replace(minute=nxt)
            # 这个小时没戏：跳到下个小时零分
            moment = (moment + timedelta(hours=1)).replace(minute=0)
        raise CronError(f"cron {self!s} 一年内没有触发点，检查日 / 周 / 月的组合")

    def matches(self, dt: datetime) -> bool:
        """某个时刻是否命中（测试用；调度循环走 next_after，不靠它）。"""
        return (
            dt.month in self.month.values
            and dt.hour in self.hour.values
            and dt.minute in self.minute.values
            and self._day_matches(dt)
        )

    @override
    def __str__(self) -> str:
        """还原成 ``"*/5 * * * *"`` 形式的文本。"""
        return " ".join(
            str(field) for field in (self.minute, self.hour, self.day, self.month, self.weekday)
        )

    def _day_matches(self, dt: datetime) -> bool:
        """日 / 周的命中判断：都受限取 OR（vixie 语义），否则取 AND。"""
        day_restricted = self.day.values != frozenset(range(1, 32))
        weekday_restricted = self.weekday.values != frozenset(range(0, 7))
        day_hit = self.day.contains(dt.day)
        # Python 的 isoweekday 是 1-7（周一到周日），cron 的周字段是 0-6（周日=0）
        weekday_hit = self.weekday.contains(dt.isoweekday() % 7)
        if day_restricted and weekday_restricted:
            return day_hit or weekday_hit
        return day_hit and weekday_hit

    @staticmethod
    def _next_value(values: frozenset[int], current: int) -> int | None:
        """集合里比 current 大的最小值；没有则 None。"""
        bigger = [value for value in values if value > current]
        return min(bigger) if bigger else None
