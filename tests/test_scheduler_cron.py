"""cron.py 的单元测试：解析、next_after 的推算、语义与报错。全部用固定时间，无等待。"""
from __future__ import annotations

from datetime import datetime

import pytest

from tickneko.core.scheduler import CronError, CronExpr, CronField

DT = datetime  # 缩写，用例里对齐更清爽


class TestParse:
    def test_five_fields_ok(self) -> None:
        expr = CronExpr.parse("*/15 9-17 1,15 * 1-5")
        assert expr.minute.values == frozenset(range(0, 60, 15))
        assert expr.hour.values == frozenset(range(9, 18))
        assert expr.day.values == frozenset({1, 15})
        assert expr.month.values == frozenset(range(1, 13))
        assert expr.weekday.values == frozenset(range(1, 6))

    def test_weekday_7_maps_to_sunday_0(self) -> None:
        assert CronExpr.parse("0 0 * * 7").weekday.values == frozenset({0})
        assert CronExpr.parse("0 0 * * 0").weekday.values == frozenset({0})

    def test_six_fields_with_seconds(self) -> None:
        expr = CronExpr.parse("*/30 5,35 9-17 1,15 * 1-5")
        assert expr.second.values == frozenset({0, 30})
        assert expr.minute.values == frozenset({5, 35})
        assert expr.hour.values == frozenset(range(9, 18))
        assert expr.with_seconds is True

    def test_five_fields_default_second_to_zero(self) -> None:
        """5 段写法秒固定 0，语义与原来一致。"""
        expr = CronExpr.parse("*/15 * * * *")
        assert expr.second.values == frozenset({0})
        assert expr.with_seconds is False

    def test_extra_spaces_are_fine(self) -> None:
        expr = CronExpr.parse("  0   12   *   *   *  ")
        assert expr.minute.values == frozenset({0})
        assert CronExpr.parse(" 0  0  12  *  *  * ").minute.values == frozenset({0})

    def test_str_roundtrip(self) -> None:
        assert str(CronExpr.parse("*/5 * * * *")) == "*/5 * * * *"
        assert str(CronExpr.parse("30 2 * * *")) == "30 2 * * *"
        assert str(CronExpr.parse("0 9-17 * * 1-5")) == "0 9-17 * * 1-5"
        assert str(CronExpr.parse("1,15 0 * * *")) == "1,15 0 * * *"
        # 6 段按原样还原，不会退化成 5 段
        assert str(CronExpr.parse("*/30 * * * * *")) == "*/30 * * * * *"
        assert str(CronExpr.parse("5,45 0 12 * * *")) == "5,45 0 12 * * *"

    def test_field_str_variants(self) -> None:
        assert str(CronField.parse("*", "minute", 0, 59)) == "*"
        assert str(CronField.parse("10", "minute", 0, 59)) == "10"
        assert str(CronField.parse("10-20", "minute", 0, 59)) == "10-20"
        assert str(CronField.parse("*/10", "minute", 0, 59)) == "*/10"
        assert str(CronField.parse("3-59/10", "minute", 0, 59)) == "3-59/10"
        assert str(CronField.parse("5,35", "minute", 0, 59)) == "5,35"

    @pytest.mark.parametrize(
        "text",
        [
            "60 * * * *",  # 分钟越界
            "* 24 * * *",  # 小时越界
            "* * 0 * *",  # 日从 1 起
            "* * 32 * *",  # 日越界
            "* * * 0 *",  # 月从 1 起
            "* * * 13 *",  # 月越界
            "* * * * 8",  # 星期只到 7（=周日）
            "*/0 * * * *",  # 步进要 >=1
            "5-2 * * * *",  # 区间反了
            "a * * * *",  # 不是数字
            "1, * * * *",  # 空项
            "0 0 * *",  # 少一段
            "0 0 * * * * *",  # 七段，最多 6 段
            "60 * * * * *",  # 秒越界
            "* 60 * * * *",  # 6 段的分钟越界
            "",  # 空串
        ],
    )
    def test_bad_expressions_raise(self, text: str) -> None:
        with pytest.raises(CronError):
            CronExpr.parse(text)


class TestNextAfter:
    def test_step_minutes(self) -> None:
        expr = CronExpr.parse("*/15 * * * *")
        assert expr.next_after(DT(2026, 9, 15, 10, 7, 3)) == DT(2026, 9, 15, 10, 15)

    def test_seconds_are_zeroed(self) -> None:
        expr = CronExpr.parse("*/5 * * * *")
        result = expr.next_after(DT(2026, 9, 15, 10, 7, 59, 999999))
        assert result == DT(2026, 9, 15, 10, 10, 0, 0)

    def test_exact_hit_moves_to_next(self) -> None:
        """after 本身命中不算，严格往后找。"""
        expr = CronExpr.parse("*/10 * * * *")
        assert expr.next_after(DT(2026, 9, 15, 10, 20)) == DT(2026, 9, 15, 10, 30)

    def test_daily_time_next_day(self) -> None:
        expr = CronExpr.parse("30 2 * * *")
        assert expr.next_after(DT(2026, 9, 15, 10, 0)) == DT(2026, 9, 16, 2, 30)

    def test_rolls_over_midnight(self) -> None:
        expr = CronExpr.parse("0 0 * * *")
        assert expr.next_after(DT(2026, 9, 15, 23, 59, 59)) == DT(2026, 9, 16, 0, 0)

    def test_month_mismatch_jumps_whole_month(self) -> None:
        expr = CronExpr.parse("0 0 1 1 *")  # 每年 1 月 1 日
        assert expr.next_after(DT(2026, 3, 1, 12, 34)) == DT(2027, 1, 1, 0, 0)

    def test_specific_month_only(self) -> None:
        expr = CronExpr.parse("* * * 2 *")  # 只在 2 月
        assert expr.next_after(DT(2026, 9, 15, 8, 0)) == DT(2027, 2, 1, 0, 0)

    def test_weekday_only(self) -> None:
        expr = CronExpr.parse("0 5 * * 1")  # 周一 05:00
        # 2026-09-15 是周二
        assert expr.next_after(DT(2026, 9, 15, 0, 0)) == DT(2026, 9, 21, 5, 0)

    def test_weekday_7_is_sunday(self) -> None:
        expr = CronExpr.parse("0 5 * * 7")
        assert expr.next_after(DT(2026, 9, 15, 0, 0)) == DT(2026, 9, 20, 5, 0)

    def test_day_and_weekday_both_restricted_means_or(self) -> None:
        """vixie 语义：日和周都受限时取 OR —— 每月 13 号或每个周五，12 点。"""
        expr = CronExpr.parse("0 12 13 * 5")
        # 2026-01-01 是周四：下一个命中的是 1 月 2 日（周五），而不是等到 13 号
        assert expr.next_after(DT(2026, 1, 1, 0, 0)) == DT(2026, 1, 2, 12, 0)
        # 下一个周五 1 月 9 日比下一个 13 号（1 月 13 日）早
        assert expr.next_after(DT(2026, 1, 2, 12, 0)) == DT(2026, 1, 9, 12, 0)
        # 13 号（周二）插在两个周五之间
        assert expr.next_after(DT(2026, 1, 9, 12, 0)) == DT(2026, 1, 13, 12, 0)

    def test_day_one_or_monday(self) -> None:
        expr = CronExpr.parse("1 1 1 * 1")  # 每月 1 号或周一，01:01
        # 2026-01-01 本身就是 1 号，虽然不是周一
        assert expr.next_after(DT(2026, 1, 1, 0, 0)) == DT(2026, 1, 1, 1, 1)
        # 接下来周一 1 月 5 日比下个月 1 号早
        assert expr.next_after(DT(2026, 1, 1, 1, 1)) == DT(2026, 1, 5, 1, 1)

    def test_list_and_range_minutes(self) -> None:
        expr = CronExpr.parse("5,45 8 * * *")
        assert expr.next_after(DT(2026, 9, 15, 8, 46)) == DT(2026, 9, 16, 8, 5)

    def test_second_step(self) -> None:
        """6 段秒级：落点精确到秒，分钟内的秒用完了就进下一分钟。"""
        expr = CronExpr.parse("*/15 * * * * *")
        assert expr.next_after(DT(2026, 9, 15, 10, 0, 0)) == DT(2026, 9, 15, 10, 0, 15)
        assert expr.next_after(DT(2026, 9, 15, 10, 0, 44)) == DT(2026, 9, 15, 10, 0, 45)
        assert expr.next_after(DT(2026, 9, 15, 10, 0, 45)) == DT(2026, 9, 15, 10, 1, 0)
        assert expr.next_after(DT(2026, 9, 15, 10, 0, 59)) == DT(2026, 9, 15, 10, 1, 0)

    def test_fixed_second_each_minute(self) -> None:
        expr = CronExpr.parse("30 * * * * *")  # 每分钟的第 30 秒
        assert expr.next_after(DT(2026, 9, 15, 10, 0, 29)) == DT(2026, 9, 15, 10, 0, 30)
        assert expr.next_after(DT(2026, 9, 15, 10, 0, 30)) == DT(2026, 9, 15, 10, 1, 30)
        assert expr.next_after(DT(2026, 9, 15, 10, 0, 31)) == DT(2026, 9, 15, 10, 1, 30)

    def test_seconds_roll_over_hour(self) -> None:
        expr = CronExpr.parse("*/10 * * * * *")
        # 本小时剩下的秒都没戏：跨到下个小时 0 分 0 秒
        assert expr.next_after(DT(2026, 9, 15, 23, 59, 55)) == DT(2026, 9, 16, 0, 0, 0)

    def test_five_field_expr_keeps_zero_second(self) -> None:
        """5 段写法不受影响，仍然落在 0 秒。"""
        expr = CronExpr.parse("*/5 * * * *")
        assert expr.next_after(DT(2026, 9, 15, 10, 7, 3)).second == 0

    def test_impossible_date_raises(self) -> None:
        """2 月 31 日永远不存在，一年内找不到就该报错而不是死循环。"""
        with pytest.raises(CronError, match="一年内没有触发点"):
            CronExpr.parse("0 0 31 2 *").next_after(DT(2026, 9, 15, 0, 0))


class TestMatches:
    def test_hit_and_miss(self) -> None:
        expr = CronExpr.parse("*/10 * * * *")
        assert expr.matches(DT(2026, 9, 15, 10, 20))
        assert not expr.matches(DT(2026, 9, 15, 10, 21))

    def test_month_mismatch(self) -> None:
        expr = CronExpr.parse("0 0 1 2 *")  # 2 月 1 日
        assert expr.matches(DT(2027, 2, 1, 0, 0))
        assert not expr.matches(DT(2026, 1, 1, 0, 0))

    def test_second_field_is_checked(self) -> None:
        expr = CronExpr.parse("*/15 * * * * *")
        assert expr.matches(DT(2026, 9, 15, 10, 0, 30))
        assert not expr.matches(DT(2026, 9, 15, 10, 0, 31))
        # 5 段写法的秒固定 0：非 0 秒不命中
        assert CronExpr.parse("*/10 * * * *").matches(DT(2026, 9, 15, 10, 20, 0))
        assert not CronExpr.parse("*/10 * * * *").matches(DT(2026, 9, 15, 10, 20, 5))

    def test_or_semantics_in_matches(self) -> None:
        expr = CronExpr.parse("0 0 13 * 5")  # 13 号或周五
        assert expr.matches(DT(2026, 9, 13, 0, 0))  # 周日，但是 13 号
        assert expr.matches(DT(2026, 9, 11, 0, 0))  # 周五，不是 13 号
        assert not expr.matches(DT(2026, 9, 14, 0, 0))  # 周一，也不是 13 号
