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
import custom_components.enova_power.statistics as statistics_module
from custom_components.enova_power.statistics import (
    TieredRates,
    _async_import_series,
    _build_statistics,
    _cycle_key,
    _missing_series,
    _period_hourly,
    _tier_hourly,
    bucket_cost_points,
    bucket_cost_statistic_id,
    bucket_points,
    bucket_statistic_id,
    consumption_statistic_id,
    cost_if_statistic_id,
    cost_points,
    cost_statistic_id,
    cost_total,
    expected_statistic_ids,
    plan_prices,
    rebuild_statistic_ids,
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
    assert bucket_cost_statistic_id("111", "tou_on_peak") == "enova_power:cost_tou_on_peak_111"
    assert bucket_cost_statistic_id("111", "tier1") == "enova_power:cost_tier1_111"
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


# --- classification (local Ontario clock) ------------------------------------ #


async def test_period_hourly_tou_summer_weekday() -> None:
    # 2026-06-01 is a Monday. h01=00:00 local (off), h09=08:00 (mid), h13=12:00 (on).
    r = _reading(date(2026, 6, 1), h01=1.0, h09=3.0, h13=2.0)
    hourly = _period_hourly([r], PLAN_TOU)
    # Points are hourly, timestamped at the interval start; June is EDT (UTC-4).
    assert hourly[PERIOD_OFF_PEAK] == [(datetime(2026, 6, 1, 4, tzinfo=timezone.utc), 1.0)]
    assert hourly[PERIOD_MID_PEAK] == [(datetime(2026, 6, 1, 12, tzinfo=timezone.utc), 3.0)]
    assert hourly[PERIOD_ON_PEAK] == [(datetime(2026, 6, 1, 16, tzinfo=timezone.utc), 2.0)]


async def test_period_hourly_ulo_overnight() -> None:
    # h02 = 01:00 local → ULO overnight (23:00-07:00); h18 = 17:00 → ULO on-peak (16-21).
    r = _reading(date(2026, 6, 1), h02=4.0, h18=5.0)
    hourly = _period_hourly([r], PLAN_ULO)
    assert hourly[PERIOD_ULO_OVERNIGHT] == [(datetime(2026, 6, 1, 5, tzinfo=timezone.utc), 4.0)]
    assert hourly[PERIOD_ON_PEAK] == [(datetime(2026, 6, 1, 21, tzinfo=timezone.utc), 5.0)]


async def test_period_hourly_winter_matches_est() -> None:
    # In winter EST = local, so h18 (17:00) lands at 22:00 UTC — the winter
    # on-peak window the user verified in HA.
    r = _reading(date(2026, 1, 15), h18=2.0)  # Thursday, winter on-peak 17-19
    hourly = _period_hourly([r], PLAN_TOU)
    assert hourly[PERIOD_ON_PEAK] == [(datetime(2026, 1, 15, 22, tzinfo=timezone.utc), 2.0)]


async def test_period_hourly_keeps_hours_separate() -> None:
    # Two off-peak hours on the same day stay two points — no day aggregation.
    r = _reading(date(2026, 6, 1), h01=1.0, h02=2.0)
    hourly = _period_hourly([r], PLAN_TOU)
    assert [kwh for _, kwh in hourly[PERIOD_OFF_PEAK]] == [1.0, 2.0]


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


async def test_tier_hourly_crosses_threshold_in_cycle() -> None:
    periods = [BillingPeriod(date(2026, 5, 31), date(2026, 6, 30), 30, 0.0, 0.0)]
    readings = [
        _reading(date(2026, 6, 1), h13=500.0),  # under 600
        _reading(date(2026, 6, 2), h13=200.0),  # crosses 600 → 100 t1 + 100 t2
    ]
    tiers = _tier_hourly(readings, periods)
    assert [k for _, k in tiers["tier1"]] == pytest.approx([500.0, 100.0])
    # Zero-contribution hours are omitted: tier2 only starts at the crossing.
    assert tiers["tier2"] == [(datetime(2026, 6, 2, 16, tzinfo=timezone.utc), 100.0)]


async def test_tier_hourly_splits_within_a_day() -> None:
    # The crossing lands in the exact hour it happens, not smeared over the day.
    periods = [BillingPeriod(date(2026, 5, 31), date(2026, 6, 30), 30, 0.0, 0.0)]
    readings = [_reading(date(2026, 6, 1), h01=590.0, h02=20.0, h03=5.0)]
    tiers = _tier_hourly(readings, periods)
    assert [k for _, k in tiers["tier1"]] == pytest.approx([590.0, 10.0])
    assert [k for _, k in tiers["tier2"]] == pytest.approx([10.0, 5.0])
    assert tiers["tier2"][0][0] == datetime(2026, 6, 1, 5, tzinfo=timezone.utc)  # h02, EDT


# --- cost ------------------------------------------------------------------- #


async def test_cost_points_tou_hourly() -> None:
    r = _reading(date(2026, 6, 1), h01=10.0, h13=5.0)  # off 10, on 5
    points = cost_points([r], PLAN_TOU, TOU_RATES, None, [])
    # One point per hour: 10 × 9.8¢ at h01, 5 × 20.3¢ at h13 (June = EDT).
    assert points == [
        (datetime(2026, 6, 1, 4, tzinfo=timezone.utc), pytest.approx(0.98)),
        (datetime(2026, 6, 1, 16, tzinfo=timezone.utc), pytest.approx(1.015)),
    ]


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


# --- per-bucket cost ---------------------------------------------------------- #


async def test_bucket_cost_points_tou() -> None:
    r = _reading(date(2026, 6, 1), h01=10.0, h13=5.0)  # off 10, on 5
    buckets = bucket_cost_points([r], TOU_RATES, None, [])
    assert buckets["tou_off_peak"][0][1] == pytest.approx(0.98)  # 10 × 9.8¢
    assert buckets["tou_on_peak"][0][1] == pytest.approx(1.015)  # 5 × 20.3¢
    # No ULO rates and no tiered rates → those buckets are omitted entirely.
    assert not any(key.startswith("ulo_") for key in buckets)
    assert "tier1" not in buckets


async def test_bucket_cost_points_tiers() -> None:
    periods = [BillingPeriod(date(2026, 5, 31), date(2026, 6, 30), 30, 0.0, 0.0)]
    readings = [
        _reading(date(2026, 6, 1), h13=500.0),
        _reading(date(2026, 6, 2), h13=200.0),  # crosses 600 → 100 t1 + 100 t2
    ]
    buckets = bucket_cost_points(readings, [], TieredRates(tier1=10.0, tier2=20.0), periods)
    assert [c for _, c in buckets["tier1"]] == pytest.approx([50.0, 10.0])
    assert [c for _, c in buckets["tier2"]] == pytest.approx([20.0])


async def test_bucket_costs_sum_to_scheme_cost() -> None:
    # A scheme's bucket costs must always sum to its cost series.
    r = _reading(date(2026, 6, 1), h01=10.0, h09=3.0, h13=5.0)
    buckets = bucket_cost_points([r], TOU_RATES, None, [])
    bucket_total = sum(cost for points in buckets.values() for _, cost in points)
    scheme_total = sum(cost for _, cost in cost_points([r], PLAN_TOU, TOU_RATES, None, []))
    assert bucket_total == pytest.approx(scheme_total)


# --- upgrade detection (expected vs stored series) ---------------------------- #


async def test_expected_ids_without_rates() -> None:
    ids = expected_statistic_ids("111", PLAN_TOU, [], None)
    # Consumption + the 9 kWh buckets always; no cost ids without rates.
    assert len(ids) == 10
    assert consumption_statistic_id("111") in ids
    assert not any(":cost" in statistic_id for statistic_id in ids)


async def test_expected_ids_with_tou_rates() -> None:
    ids = expected_statistic_ids("111", PLAN_TOU, TOU_RATES, None)
    assert bucket_cost_statistic_id("111", "tou_on_peak") in ids
    assert cost_statistic_id("111") in ids  # active plan (TOU) is priced
    assert cost_if_statistic_id("111", PLAN_TOU) in ids
    # ULO rates and tiered rates unavailable → their cost ids are not expected.
    assert bucket_cost_statistic_id("111", "ulo_overnight") not in ids
    assert bucket_cost_statistic_id("111", "tier1") not in ids
    assert cost_if_statistic_id("111", PLAN_TIERED) not in ids


async def test_expected_ids_tiered_plan() -> None:
    ids = expected_statistic_ids("111", PLAN_TIERED, TIER_RATES, tiered_rates(TIER_RATES))
    assert bucket_cost_statistic_id("111", "tier1") in ids
    assert bucket_cost_statistic_id("111", "tier2") in ids
    assert cost_statistic_id("111") in ids
    assert cost_if_statistic_id("111", PLAN_TIERED) in ids
    # TOU prices missing → the active-cost id must not depend on them, but
    # TOU bucket costs and cost_if_tou are not expected.
    assert bucket_cost_statistic_id("111", "tou_on_peak") not in ids


async def test_missing_series(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = {"enova_power:energy_consumption_111"}
    monkeypatch.setattr(
        statistics_module,
        "get_last_statistics",
        lambda hass, n, statistic_id, convert, types: (
            {statistic_id: [{"sum": 1.0}]} if statistic_id in stored else {}
        ),
    )
    ids = ["enova_power:energy_consumption_111", "enova_power:cost_tou_on_peak_111"]
    assert _missing_series(None, ids) == ["enova_power:cost_tou_on_peak_111"]


# --- statistics-format rebuild ------------------------------------------------ #


async def test_rebuild_ids_cover_every_series() -> None:
    ids = rebuild_statistic_ids("111")
    # consumption + 9 kWh buckets + 9 cost buckets + energy_cost + 3 cost_if.
    assert len(ids) == 23
    # v3 moves timestamps, so consumption rebuilds too.
    assert consumption_statistic_id("111") in ids
    assert bucket_statistic_id("111", "tou_on_peak") in ids
    assert bucket_cost_statistic_id("111", "tier2") in ids
    assert cost_statistic_id("111") in ids
    assert cost_if_statistic_id("111", PLAN_ULO) in ids
    # Rate-gating must not apply here: clear everything that may exist.
    assert set(ids) >= set(expected_statistic_ids("111", PLAN_TOU, [], None))


async def test_start_rebuild_queues_clear_for_all_meters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleared: list[str] = []

    class FakeRecorder:
        # Mirrors Recorder.async_clear_statistics: queues onto the recorder
        # thread. The rebuild must never wait on it — the recorder holds its
        # queue until HA has started, so a wait deadlocks bootstrap.
        def async_clear_statistics(self, ids):
            cleared.extend(ids)

    monkeypatch.setattr(statistics_module, "get_instance", lambda hass: FakeRecorder())

    statistics_module.async_start_rebuild(None, ["111", "222"])

    assert len(cleared) == 46
    assert consumption_statistic_id("111") in cleared
    assert bucket_statistic_id("111", "tier1") in cleared
    assert bucket_cost_statistic_id("222", "ulo_overnight") in cleared


async def test_import_series_fresh_ignores_stored_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A stale row exists (the queued clear hasn't executed yet); fresh=True
    # must neither filter against it nor resume from its sum.
    base = datetime(2026, 1, 1, 5, tzinfo=timezone.utc)
    written = _patch_import(monkeypatch, {"start": base.replace(hour=7), "sum": 500.0})
    points = [(base, 1.0), (base.replace(hour=6), 2.0)]

    total = await _async_import_series(
        None, "enova_power:x", "x", points, "kWh", fresh=True
    )

    assert total == 3.0
    assert [s["sum"] for s in written] == [1.0, 3.0]


async def test_import_meter_rebuild_reimports_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_row = {"start": datetime(2026, 6, 2, 0, tzinfo=timezone.utc), "sum": 500.0}
    written: dict[str, list[float]] = {}

    async def fake_last_row(hass, statistic_id):
        return stale_row

    monkeypatch.setattr(statistics_module, "_async_last_row", fake_last_row)
    monkeypatch.setattr(
        statistics_module,
        "async_add_external_statistics",
        lambda hass, metadata, stats: written.__setitem__(
            metadata["statistic_id"], [s["sum"] for s in stats]
        ),
    )

    readings = [_reading(date(2026, 6, 1), h01=10.0, h13=5.0)]
    total = await statistics_module.async_import_meter(
        None, "111", readings, PLAN_TOU, TOU_RATES, None, [], "CAD", rebuild=True
    )

    # Every series — consumption included (its timestamps moved in v3) —
    # ignores the stale row: full points, sums restarting from zero.
    assert total == 15.0
    assert written[consumption_statistic_id("111")] == [10.0, 15.0]
    assert written[bucket_statistic_id("111", "tou_off_peak")] == [10.0]
    assert written[bucket_cost_statistic_id("111", "tou_on_peak")] == pytest.approx([1.015])


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


# --- import series return value (lifetime total) ----------------------------- #


def _patch_import(monkeypatch: pytest.MonkeyPatch, row: dict | None) -> list:
    """Stub the recorder read/write; return the list capturing written stats."""
    written: list = []

    async def fake_last_row(hass, statistic_id):
        return row

    monkeypatch.setattr(statistics_module, "_async_last_row", fake_last_row)
    monkeypatch.setattr(
        statistics_module,
        "async_add_external_statistics",
        lambda hass, metadata, stats: written.extend(stats),
    )
    return written


async def test_import_series_returns_cumulative_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written = _patch_import(monkeypatch, None)
    base = datetime(2026, 1, 1, 5, tzinfo=timezone.utc)
    points = [(base, 1.0), (base.replace(hour=6), 2.0)]
    total = await _async_import_series(None, "enova_power:x", "x", points, "kWh")
    assert total == 3.0
    assert [s["sum"] for s in written] == [1.0, 3.0]


async def test_import_series_resumes_from_stored_sum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = datetime(2026, 1, 1, 5, tzinfo=timezone.utc)
    _patch_import(monkeypatch, {"start": base, "sum": 10.0})
    total = await _async_import_series(
        None, "enova_power:x", "x", [(base.replace(hour=6), 2.0)], "kWh"
    )
    assert total == 12.0


async def test_import_series_total_survives_no_new_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nothing new published (or everything already stored) → the stored sum.
    base = datetime(2026, 1, 1, 5, tzinfo=timezone.utc)
    written = _patch_import(monkeypatch, {"start": base, "sum": 10.0})
    assert await _async_import_series(None, "enova_power:x", "x", [], "kWh") == 10.0
    assert await _async_import_series(
        None, "enova_power:x", "x", [(base, 1.0)], "kWh"
    ) == 10.0
    assert written == []


async def test_import_series_none_when_series_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_import(monkeypatch, None)
    assert await _async_import_series(None, "enova_power:x", "x", [], "kWh") is None


async def test_import_meter_writes_bucket_costs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written: dict[str, str] = {}  # statistic_id → unit

    async def fake_last_row(hass, statistic_id):
        return None

    monkeypatch.setattr(statistics_module, "_async_last_row", fake_last_row)
    monkeypatch.setattr(
        statistics_module,
        "async_add_external_statistics",
        lambda hass, metadata, stats: written.__setitem__(
            metadata["statistic_id"], metadata["unit_of_measurement"]
        ),
    )

    rates = TOU_RATES + TIER_RATES
    tiered = tiered_rates(TIER_RATES)
    readings = [_reading(date(2026, 6, 1), h01=10.0, h13=5.0)]
    total = await statistics_module.async_import_meter(
        None, "111", readings, PLAN_TOU, rates, tiered, [], "CAD"
    )

    assert total == 15.0
    assert written[consumption_statistic_id("111")] == "kWh"
    assert written[bucket_cost_statistic_id("111", "tou_off_peak")] == "CAD"
    assert written[bucket_cost_statistic_id("111", "tou_on_peak")] == "CAD"
    assert written[bucket_cost_statistic_id("111", "tier1")] == "CAD"
    assert cost_statistic_id("111") in written
    assert cost_if_statistic_id("111", PLAN_TIERED) in written
    # No ULO rates → no ULO cost series (and none expected, so no refetch loop).
    assert cost_if_statistic_id("111", PLAN_ULO) not in written
    assert bucket_cost_statistic_id("111", "ulo_overnight") not in written
    # Everything written must be expected, or upgrade detection would never settle.
    expected = set(expected_statistic_ids("111", PLAN_TOU, rates, tiered))
    assert set(written) <= expected
