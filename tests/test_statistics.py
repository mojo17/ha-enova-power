"""Tests for the statistics importer's pure logic (no recorder needed)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from enovapower import TariffRate, UsageReading

from custom_components.enova_power.const import PLAN_TOU
from custom_components.enova_power.statistics import (
    TOU_BUCKETS,
    _build_statistics,
    _cost_points,
    _daily_points,
    _flatten_points,
    _normalize_start,
    bucket_statistic_id,
    consumption_statistic_id,
    cost_statistic_id,
    plan_prices,
)


def _reading(day: date, **hours: float) -> UsageReading:
    hourly: dict[str, float | None] = {f"h{i:02d}": None for i in range(1, 25)}
    hourly.update(hours)
    return UsageReading(date=day, hourly=hourly)


async def test_statistic_id() -> None:
    assert consumption_statistic_id("111111") == "enova_power:energy_consumption_111111"


async def test_bucket_statistic_ids() -> None:
    assert set(TOU_BUCKETS) == {"on_peak", "mid_peak", "off_peak"}
    assert bucket_statistic_id("111111", "on_peak") == "enova_power:energy_on_peak_111111"


async def test_daily_points_uses_day_start_and_attr() -> None:
    reading = _reading(date(2026, 1, 2), h01=1.0)
    reading.total_on_peak = 5.0
    points = _daily_points([reading], "total_on_peak")
    assert len(points) == 1
    start, value = points[0]
    assert value == 5.0
    # day start = h01 = 2026-01-02 00:00 EST = 05:00 UTC
    assert start == datetime(2026, 1, 2, 5, tzinfo=timezone.utc)


def _tou_rate(name: str, price: float, plan: str = "Time-of-Use") -> TariffRate:
    return TariffRate(
        start_date=date(2026, 1, 1),
        end_date=date(2026, 4, 30),
        plan=plan,
        name=name,
        price=price,
    )


async def test_cost_statistic_id() -> None:
    assert cost_statistic_id("111111") == "enova_power:energy_cost_111111"


async def test_plan_prices_tou() -> None:
    rates = [
        _tou_rate("TOU On-peak", 20.0),
        _tou_rate("TOU Mid-peak", 15.0),
        _tou_rate("TOU Off-peak", 9.0),
        _tou_rate("ULO Off-peak", 8.0, plan="Ultra-Low Overnight"),  # ignored
    ]
    assert plan_prices(rates, PLAN_TOU) == {
        "on_peak": 20.0,
        "mid_peak": 15.0,
        "off_peak": 9.0,
    }


async def test_plan_prices_partial_on_missing_names() -> None:
    # Only the matched periods are returned; callers degrade gracefully.
    assert plan_prices([_tou_rate("TOU On-peak", 20.0)], PLAN_TOU) == {"on_peak": 20.0}


async def test_cost_points_converts_cents_to_dollars() -> None:
    reading = _reading(date(2026, 1, 2), h01=1.0)
    reading.total_on_peak = 2.0
    reading.total_mid_peak = 3.0
    reading.total_off_peak = 5.0
    prices = {"on_peak": 20.0, "mid_peak": 15.0, "off_peak": 10.0}  # cents/kWh
    points = _cost_points([reading], prices)
    # 2*20 + 3*15 + 5*10 = 135 cents = $1.35
    assert len(points) == 1
    start, cost = points[0]
    assert cost == pytest.approx(1.35)
    assert start == datetime(2026, 1, 2, 5, tzinfo=timezone.utc)


async def test_normalize_start_float() -> None:
    assert _normalize_start(1700000000.0) == datetime.fromtimestamp(
        1700000000.0, tz=timezone.utc
    )


async def test_normalize_start_naive_datetime_assumed_utc() -> None:
    assert _normalize_start(datetime(2026, 1, 1, 5)) == datetime(
        2026, 1, 1, 5, tzinfo=timezone.utc
    )


async def test_normalize_start_aware_datetime_passthrough() -> None:
    aware = datetime(2026, 1, 1, 5, tzinfo=timezone.utc)
    assert _normalize_start(aware) == aware


async def test_normalize_start_none() -> None:
    assert _normalize_start(None) is None


async def test_flatten_drops_missing_hours_and_sorts() -> None:
    later = _reading(date(2026, 1, 2), h01=1.0)
    earlier = _reading(date(2026, 1, 1), h02=2.0)
    points = _flatten_points([later, earlier])
    assert len(points) == 2  # only the two present hours
    assert points[0][0] < points[1][0]  # sorted by start
    assert points[0][1] == 2.0  # earlier reading first


async def test_build_statistics_cumulative_sum() -> None:
    base = datetime(2026, 1, 1, 5, tzinfo=timezone.utc)
    points = [(base, 1.0), (base.replace(hour=6), 2.0), (base.replace(hour=7), 3.0)]
    stats = _build_statistics(points, last_start=None, base_sum=0.0)
    assert [s["sum"] for s in stats] == [1.0, 3.0, 6.0]
    assert stats[0]["start"] == base


async def test_build_statistics_resumes_and_dedups() -> None:
    base = datetime(2026, 1, 1, 5, tzinfo=timezone.utc)
    points = [(base, 1.0), (base.replace(hour=6), 2.0), (base.replace(hour=7), 3.0)]
    # Already imported through hour 6 (sum 10); only hour 7 is appended.
    stats = _build_statistics(points, last_start=base.replace(hour=6), base_sum=10.0)
    assert len(stats) == 1
    assert stats[0]["start"] == base.replace(hour=7)
    assert stats[0]["sum"] == 13.0
