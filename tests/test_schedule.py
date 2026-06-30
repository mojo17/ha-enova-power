"""Tests for the Ontario OEB TOU/ULO schedule (pure logic)."""

from __future__ import annotations

from datetime import date, datetime

from custom_components.enova_power.const import (
    PERIOD_MID_PEAK,
    PERIOD_OFF_PEAK,
    PERIOD_ON_PEAK,
    PERIOD_TIERED,
    PERIOD_ULO_OVERNIGHT,
    PLAN_TIERED,
    PLAN_TOU,
    PLAN_ULO,
    TIME_ZONE,
)
from custom_components.enova_power.schedule import current_period, ontario_tou_holidays


def _dt(year: int, month: int, day: int, hour: int) -> datetime:
    return datetime(year, month, day, hour, tzinfo=TIME_ZONE)


# 2026-07-15 is a summer Wednesday; 2026-01-14 a winter Wednesday;
# 2026-07-18 a Saturday; 2026-12-25 (Fri) is Christmas.

async def test_holidays_include_fixed_and_computed() -> None:
    hols = ontario_tou_holidays(2026)
    assert date(2026, 12, 25) in hols  # Christmas
    assert date(2026, 2, 16) in hols  # Family Day (3rd Mon Feb)
    assert date(2026, 4, 3) in hols  # Good Friday (Easter 2026-04-05 - 2)


async def test_tou_summer_weekday_periods() -> None:
    assert current_period(_dt(2026, 7, 15, 13), PLAN_TOU) == PERIOD_ON_PEAK  # 11-17
    assert current_period(_dt(2026, 7, 15, 8), PLAN_TOU) == PERIOD_MID_PEAK  # 7-11
    assert current_period(_dt(2026, 7, 15, 22), PLAN_TOU) == PERIOD_OFF_PEAK  # >=19


async def test_tou_winter_weekday_on_peak_shifts() -> None:
    assert current_period(_dt(2026, 1, 14, 8), PLAN_TOU) == PERIOD_ON_PEAK  # 7-11
    assert current_period(_dt(2026, 1, 14, 13), PLAN_TOU) == PERIOD_MID_PEAK  # 11-17


async def test_tou_weekend_and_holiday_off_peak() -> None:
    assert current_period(_dt(2026, 7, 18, 13), PLAN_TOU) == PERIOD_OFF_PEAK  # Saturday
    assert current_period(_dt(2026, 12, 25, 13), PLAN_TOU) == PERIOD_OFF_PEAK  # Christmas


async def test_ulo_overnight_applies_every_day() -> None:
    assert current_period(_dt(2026, 7, 15, 2), PLAN_ULO) == PERIOD_ULO_OVERNIGHT
    assert current_period(_dt(2026, 7, 18, 23), PLAN_ULO) == PERIOD_ULO_OVERNIGHT  # Sat 23:00


async def test_ulo_weekday_on_and_mid_peak() -> None:
    assert current_period(_dt(2026, 7, 15, 17), PLAN_ULO) == PERIOD_ON_PEAK  # 16-21
    assert current_period(_dt(2026, 7, 15, 9), PLAN_ULO) == PERIOD_MID_PEAK  # 7-16
    assert current_period(_dt(2026, 7, 18, 13), PLAN_ULO) == PERIOD_OFF_PEAK  # weekend daytime


async def test_tiered_is_constant() -> None:
    assert current_period(_dt(2026, 7, 15, 13), PLAN_TIERED) == PERIOD_TIERED
