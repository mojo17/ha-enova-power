"""Tests for the statistics importer's pure logic (no recorder needed)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from enovapower import BillingPeriod, TariffRate, UsageReading

from custom_components.enova_power.const import (
    PERIOD_MID_PEAK,
    PERIOD_OFF_PEAK,
    PERIOD_ON_PEAK,
    PERIOD_ULO_OVERNIGHT,
    PLAN_TIERED,
    PLAN_TOU,
    PLAN_ULO,
)
from custom_components.enova_power.statistics import (
    TieredRates,
    _build_statistics,
    _cycle_key,
    _period_daily,
    _tier_daily,
    bucket_points,
    bucket_statistic_id,
    consumption_statistic_id,
    cost_if_statistic_id,
    cost_points,
    cost_statistic_id,
    cost_total,
    plan_prices,
    season_threshold,
    tiered_rates,
)


def _reading(day: date, **hours: float) -> UsageReading:
    hourly: dict[str, float | None] = {f"h{i:02d}": None for i in range(1, 25)}
    hourly.update(hours)
    return UsageReading(date=day, hourly=hourly)


def _tou_rate(name: str, price: float, plan: str = "Time-of-Use") -> TariffRate:
    return TariffRate(
        start_date=date(2026, 5, 1),
        end_date=date(2026, 10, 31),
        plan=plan,
        name=name,
        price=price,
    )


TOU_RATES = [
    _tou_rate("TOU Off-peak", 9.8),
    _tou_rate("TOU Mid-peak", 15.7),
    _tou_rate("TOU On-peak", 20.3),
]
TIER_RATES = [
    TariffRate(date(2026, 5, 1), date(2026, 10, 31), "Tiered", "Tier 1", 10.0),
    TariffRate(date(2026, 5, 1), date(2026, 10, 31), "Tiered", "Tier 2", 20.0),
]


# --- IDs -------------------------------------------------------------------- #


async def test_statistic_ids() -> None:
    assert consumption_statistic_id("111") == "enova_power:energy_consumption_111"
    assert bucket_statistic_id("111", "tou_on_peak") == "enova_power:energy_tou_on_peak_111"
    assert bucket_statistic_id("111", "tier1") == "enova_power:energy_tier1_111"
    assert cost_statistic_id("111") == "enova_power:energy_cost_111"
    assert cost_if_statistic_id("111", PLAN_ULO) == "enova_power:cost_if_ulo_111"


# --- rates ------------------------------------------------------------------ #


async def test_plan_prices_tou() -> None:
    assert plan_prices(TOU_RATES, PLAN_TOU) == {
        PERIOD_OFF_PEAK: 9.8,
        PERIOD_MID_PEAK: 15.7,
        PERIOD_ON_PEAK: 20.3,
    }


async def test_tiered_rates() -> None:
    assert tiered_rates(TIER_RATES) == TieredRates(tier1=10.0, tier2=20.0)
    assert tiered_rates(TIER_RATES[:1]) is None  # missing Tier 2


async def test_season_threshold() -> None:
    assert season_threshold(date(2026, 7, 1)) == 600.0  # summer
    assert season_threshold(date(2026, 1, 1)) == 1000.0  # winter


# --- classification (fixed-EST) --------------------------------------------- #


async def test_period_daily_tou_summer_weekday() -> None:
    # 2026-06-01 is a Monday. h01=00:00 EST (off), h09=08:00 (mid), h13=12:00 (on).
    r = _reading(date(2026, 6, 1), h01=1.0, h09=3.0, h13=2.0)
    daily = _period_daily([r], PLAN_TOU)
    assert daily[PERIOD_OFF_PEAK][0][1] == 1.0
    assert daily[PERIOD_MID_PEAK][0][1] == 3.0
    assert daily[PERIOD_ON_PEAK][0][1] == 2.0


async def test_period_daily_ulo_overnight() -> None:
    # h02 = 01:00 EST → ULO overnight (23:00-07:00); h18 = 17:00 → ULO on-peak (16-21).
    r = _reading(date(2026, 6, 1), h02=4.0, h18=5.0)
    daily = _period_daily([r], PLAN_ULO)
    assert daily[PERIOD_ULO_OVERNIGHT][0][1] == 4.0
    assert daily[PERIOD_ON_PEAK][0][1] == 5.0


async def test_bucket_points_has_all_keys() -> None:
    r = _reading(date(2026, 6, 1), h13=2.0)
    buckets = bucket_points([r], [])
    assert {"tou_on_peak", "ulo_on_peak", "tier1", "tier2"} <= set(buckets)
    assert buckets["tou_on_peak"][0][1] == 2.0


# --- billing cycle grouping ------------------------------------------------- #


async def test_cycle_key_uses_billing_period() -> None:
    periods = [BillingPeriod(date(2026, 5, 19), date(2026, 6, 19), 31, 0.0, 0.0)]
    assert _cycle_key(date(2026, 6, 1), periods) == ("cycle", date(2026, 6, 19))
    # Outside any cycle → calendar-month fallback.
    assert _cycle_key(date(2026, 8, 1), periods) == ("month", 2026, 8)


async def test_tier_daily_crosses_threshold_in_cycle() -> None:
    periods = [BillingPeriod(date(2026, 5, 31), date(2026, 6, 30), 30, 0.0, 0.0)]
    readings = [
        _reading(date(2026, 6, 1), h13=500.0),  # under 600
        _reading(date(2026, 6, 2), h13=200.0),  # crosses 600 → 100 t1 + 100 t2
    ]
    tiers = _tier_daily(readings, periods)
    assert [k for _, k in tiers["tier1"]] == pytest.approx([500.0, 100.0])
    assert [k for _, k in tiers["tier2"]] == pytest.approx([0.0, 100.0])


# --- cost ------------------------------------------------------------------- #


async def test_cost_points_tou() -> None:
    r = _reading(date(2026, 6, 1), h01=10.0, h13=5.0)  # off 10, on 5
    points = cost_points([r], PLAN_TOU, TOU_RATES, None, [])
    # (10 × 9.8 + 5 × 20.3) / 100 = (98 + 101.5)/100 = 1.995
    assert points[0][1] == pytest.approx(1.995)


async def test_cost_total_tiered() -> None:
    periods = [BillingPeriod(date(2026, 5, 31), date(2026, 6, 30), 30, 0.0, 0.0)]
    readings = [
        _reading(date(2026, 6, 1), h13=500.0),
        _reading(date(2026, 6, 2), h13=200.0),
    ]
    tiered = TieredRates(tier1=10.0, tier2=20.0)
    # day1 500@10=$50; day2 100@10 + 100@20 = $30 → $80.
    assert cost_total(readings, PLAN_TIERED, [], tiered, periods) == pytest.approx(80.0)


async def test_cost_points_empty_without_rates() -> None:
    r = _reading(date(2026, 6, 1), h13=5.0)
    assert cost_points([r], PLAN_TOU, [], None, []) == []


# --- forward-only sum ------------------------------------------------------- #


async def test_build_statistics_cumulative_sum() -> None:
    base = datetime(2026, 1, 1, 5, tzinfo=timezone.utc)
    points = [(base, 1.0), (base.replace(hour=6), 2.0)]
    stats = _build_statistics(points, last_start=None, base_sum=0.0)
    assert [s["sum"] for s in stats] == [1.0, 3.0]


async def test_build_statistics_resumes_and_dedups() -> None:
    base = datetime(2026, 1, 1, 5, tzinfo=timezone.utc)
    points = [(base, 1.0), (base.replace(hour=6), 2.0), (base.replace(hour=7), 3.0)]
    stats = _build_statistics(points, last_start=base.replace(hour=6), base_sum=10.0)
    assert len(stats) == 1
    assert stats[0]["sum"] == 13.0
