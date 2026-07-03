"""Tests for the statistics importer's pure logic (no recorder needed)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from enovapower import TariffRate, UsageReading

from custom_components.enova_power.const import PLAN_TOU
from custom_components.enova_power.statistics import (
    TOU_BUCKETS,
    TieredRates,
    _build_statistics,
    _cost_points,
    _daily_points,
    _flatten_points,
    _normalize_start,
    _tiered_cost_points,
    bucket_statistic_id,
    consumption_statistic_id,
    cost_statistic_id,
    plan_prices,
    tiered_rates,
    tiered_total_cost,
    total_cost,
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


async def test_total_cost_sums_days() -> None:
    prices = {"on_peak": 20.0, "mid_peak": 15.0, "off_peak": 10.0}
    readings = []
    for day in (2, 3):
        r = _reading(date(2026, 1, day), h01=1.0)
        r.total_on_peak, r.total_mid_peak, r.total_off_peak = 2.0, 3.0, 5.0
        readings.append(r)
    # each day = $1.35, two days = $2.70
    assert total_cost(readings, prices) == pytest.approx(2.70)


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


def _tier_rate(name: str, price: float, tstart: float, tend: float | None) -> TariffRate:
    return TariffRate(
        start_date=date(2026, 5, 1),
        end_date=date(2026, 10, 31),
        plan="Tiered",
        name=name,
        price=price,
        threshold_start=tstart,
        threshold_end=tend,
    )


# tier1 10¢/kWh, tier2 20¢/kWh, 600 kWh/month threshold.
TIERED = TieredRates(tier1=10.0, tier2=20.0, threshold=600.0)


async def test_tiered_rates_extracts() -> None:
    rates = [
        _tier_rate("Tier 1", 10.0, 0.0, 600.0),
        _tier_rate("Tier 2", 20.0, 600.0, None),
    ]
    assert tiered_rates(rates) == TIERED


async def test_tiered_rates_none_when_incomplete() -> None:
    # Missing Tier 2.
    assert tiered_rates([_tier_rate("Tier 1", 10.0, 0.0, 600.0)]) is None
    # Missing threshold on Tier 1.
    rates = [
        _tier_rate("Tier 1", 10.0, 0.0, None),
        _tier_rate("Tier 2", 20.0, 600.0, None),
    ]
    assert tiered_rates(rates) is None


async def test_tiered_cost_below_threshold_all_tier1() -> None:
    points = _tiered_cost_points([_reading(date(2026, 6, 1), h01=100.0)], TIERED)
    assert [c for _, c in points] == pytest.approx([10.0])  # 100 kWh × 10¢ = $10


async def test_tiered_cost_crosses_threshold_within_month() -> None:
    readings = [
        _reading(date(2026, 6, 1), h01=500.0),  # 500 @ tier1 = $50
        _reading(date(2026, 6, 2), h01=200.0),  # 100 @ tier1 + 100 @ tier2 = $30
    ]
    assert [c for _, c in _tiered_cost_points(readings, TIERED)] == pytest.approx(
        [50.0, 30.0]
    )


async def test_tiered_cost_resets_each_calendar_month() -> None:
    readings = [
        _reading(date(2026, 6, 30), h01=700.0),  # June: 600@t1 + 100@t2 = $80
        _reading(date(2026, 7, 1), h01=100.0),  # July resets: 100@t1 = $10
    ]
    by_month = {start.month: cost for start, cost in _tiered_cost_points(readings, TIERED)}
    assert by_month[6] == pytest.approx(80.0)
    assert by_month[7] == pytest.approx(10.0)


async def test_tiered_total_cost_sums_month() -> None:
    readings = [
        _reading(date(2026, 6, 1), h01=500.0),
        _reading(date(2026, 6, 2), h01=200.0),
    ]
    assert tiered_total_cost(readings, TIERED) == pytest.approx(80.0)
