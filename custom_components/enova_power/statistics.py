"""Import Enova Power usage into Home Assistant long-term statistics.

Energy data is historical and lagged, so it belongs in external statistics (not
a live sensor): this backfills the Energy dashboard with real history.

Buckets are **usage classifications**, computed from the hourly intervals by the
meter's fixed-EST hour (verified to match the portal's own TOU totals — see
``schedule.period_for_interval``). All plan schemes are classified for every
account (plan-independent), so a plan change never orphans a bucket; only the
active ``energy_cost`` series changes which rates it applies. Cost is the energy
line item only (excludes delivery, regulatory charges, rebates and tax); the
actual all-in bill is surfaced separately as ``last_bill_amount``. The portal
only exposes *current* rates (querying a past range still returns today's
rates), so cost applies the current rates/threshold to all history — an
approximation that self-corrects going forward as rates update on May 1 / Nov 1.

External statistics carry an absolute cumulative ``sum``, so imports are
**forward-only**: the running sum resumes from the last stored point and skips
anything at or before it, which also makes re-imports idempotent.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone

from enovapower import BillingPeriod, TariffRate, UsageReading

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    LOGGER,
    PERIOD_MID_PEAK,
    PERIOD_OFF_PEAK,
    PERIOD_ON_PEAK,
    PERIOD_ULO_OVERNIGHT,
    PLAN_TIERED,
    PLAN_TOU,
    PLAN_ULO,
)
from .schedule import period_for_interval

# --------------------------------------------------------------------------- #
# Statistic IDs
# --------------------------------------------------------------------------- #


def consumption_statistic_id(meter_id: str) -> str:
    """External statistic_id for a meter's total consumption (kWh)."""
    return f"{DOMAIN}:energy_consumption_{meter_id}"


def bucket_statistic_id(meter_id: str, key: str) -> str:
    """External statistic_id for a usage bucket (e.g. ``tou_on_peak``, ``tier1``)."""
    return f"{DOMAIN}:energy_{key}_{meter_id}"


def cost_statistic_id(meter_id: str) -> str:
    """External statistic_id for a meter's active-plan energy cost (CAD)."""
    return f"{DOMAIN}:energy_cost_{meter_id}"


def cost_if_statistic_id(meter_id: str, plan: str) -> str:
    """External statistic_id for a what-if energy cost under ``plan`` (CAD)."""
    return f"{DOMAIN}:cost_if_{_SCHEME[plan]}_{meter_id}"


# Short scheme tag per plan, used in statistic ids.
_SCHEME = {PLAN_TOU: "tou", PLAN_ULO: "ulo", PLAN_TIERED: "tiered"}

# Time-of-day bucket key → the period ``current_period()`` returns for the plan.
TOU_BUCKETS: dict[str, str] = {
    "tou_off_peak": PERIOD_OFF_PEAK,
    "tou_mid_peak": PERIOD_MID_PEAK,
    "tou_on_peak": PERIOD_ON_PEAK,
}
ULO_BUCKETS: dict[str, str] = {
    "ulo_overnight": PERIOD_ULO_OVERNIGHT,
    "ulo_off_peak": PERIOD_OFF_PEAK,
    "ulo_mid_peak": PERIOD_MID_PEAK,
    "ulo_on_peak": PERIOD_ON_PEAK,
}
TIER_BUCKETS = ("tier1", "tier2")

# All bucket keys, always imported regardless of the account's plan.
ALL_BUCKET_KEYS = (*TOU_BUCKETS, *ULO_BUCKETS, *TIER_BUCKETS)

# --------------------------------------------------------------------------- #
# Rates
# --------------------------------------------------------------------------- #

# period → the scraped (plan, rate name) whose price feeds it.
PLAN_PRICE_NAMES: dict[str, dict[str, tuple[str, str]]] = {
    PLAN_TOU: {
        PERIOD_ON_PEAK: ("Time-of-Use", "TOU On-peak"),
        PERIOD_MID_PEAK: ("Time-of-Use", "TOU Mid-peak"),
        PERIOD_OFF_PEAK: ("Time-of-Use", "TOU Off-peak"),
    },
    PLAN_ULO: {
        PERIOD_ULO_OVERNIGHT: ("Ultra-Low Overnight", "ULO Lon-peak"),
        PERIOD_OFF_PEAK: ("Ultra-Low Overnight", "ULO Off-peak"),
        PERIOD_MID_PEAK: ("Ultra-Low Overnight", "ULO Mid-peak"),
        PERIOD_ON_PEAK: ("Ultra-Low Overnight", "ULO On-peak"),
    },
}


def plan_prices(rates: Iterable[TariffRate], plan: str) -> dict[str, float]:
    """Map scraped tariff rates to ``{period: cents_per_kWh}`` for ``plan``.

    Returns only the periods whose (plan, rate name) was found.
    """
    names = PLAN_PRICE_NAMES.get(plan, {})
    by_key = {(r.plan, r.name): r.price for r in rates}
    return {period: by_key[key] for period, key in names.items() if key in by_key}


@dataclass
class TieredRates:
    """The Tiered plan's two rates (current-season prices from the scrape)."""

    tier1: float  # cents/kWh at or below the threshold
    tier2: float  # cents/kWh above the threshold


def tiered_rates(rates: Iterable[TariffRate]) -> TieredRates | None:
    """Extract the Tiered plan's two rates, or None if either is missing."""
    by_name = {(r.plan, r.name): r for r in rates}
    tier1 = by_name.get(("Tiered", "Tier 1"))
    tier2 = by_name.get(("Tiered", "Tier 2"))
    if tier1 is None or tier2 is None:
        return None
    return TieredRates(tier1=tier1.price, tier2=tier2.price)


def season_threshold(d: date) -> float:
    """Ontario tiered kWh threshold for ``d``: 600 in summer, 1000 in winter.

    Summer is May 1 - Oct 31; winter Nov 1 - Apr 30 (stable OEB regulation).
    """
    return 600.0 if 5 <= d.month <= 10 else 1000.0


# --------------------------------------------------------------------------- #
# Classification → daily bucket points
# --------------------------------------------------------------------------- #


def _period_daily(
    readings: Iterable[UsageReading], plan: str
) -> dict[str, list[tuple[datetime, float]]]:
    """``{period: [(day_start, kWh)]}`` classifying each hour by fixed-EST time.

    ``plan`` selects the schedule (TOU vs ULO). The day start is the reading's
    first interval (``h01``), used as the daily timestamp for every period.
    """
    per_period: dict[str, dict[datetime, float]] = defaultdict(lambda: defaultdict(float))
    for reading in readings:
        intervals = reading.intervals()
        if not intervals:
            continue
        day_start = intervals[0][0]
        for utc_dt, kwh in intervals:
            if kwh is None:
                continue
            per_period[period_for_interval(utc_dt, plan)][day_start] += kwh
    return {period: sorted(days.items()) for period, days in per_period.items()}


def _cycle_key(d: date, periods: list[BillingPeriod]) -> tuple:
    """Billing cycle (``start < d <= end``) containing ``d``; else its month."""
    for p in periods:
        if p.start_date < d <= p.end_date:
            return ("cycle", p.end_date)
    return ("month", d.year, d.month)


def _tier_daily(
    readings: Iterable[UsageReading],
    periods: list[BillingPeriod],
    threshold_of: Callable[[date], float] = season_threshold,
) -> dict[str, list[tuple[datetime, float]]]:
    """``{'tier1'/'tier2': [(day_start, kWh)]}`` — cumulative per billing cycle.

    Within each cycle the first ``threshold_of(date)`` kWh bill at Tier 1 and the
    rest at Tier 2. Cycles come from the billing report; days outside a known
    cycle fall back to their calendar month.
    """
    by_cycle: dict[tuple, list[tuple[datetime, float, date]]] = defaultdict(list)
    for reading in readings:
        intervals = reading.intervals()
        if not intervals:
            continue
        by_cycle[_cycle_key(reading.date, periods)].append(
            (intervals[0][0], reading.total, reading.date)
        )

    tier1: list[tuple[datetime, float]] = []
    tier2: list[tuple[datetime, float]] = []
    for days in by_cycle.values():
        cumulative = 0.0
        for day_start, day_kwh, d in sorted(days):
            threshold = threshold_of(d)
            below_before = min(cumulative, threshold)
            below_after = min(cumulative + day_kwh, threshold)
            t1 = below_after - below_before
            tier1.append((day_start, t1))
            tier2.append((day_start, day_kwh - t1))
            cumulative += day_kwh
    return {"tier1": sorted(tier1), "tier2": sorted(tier2)}


def bucket_points(
    readings: list[UsageReading], periods: list[BillingPeriod]
) -> dict[str, list[tuple[datetime, float]]]:
    """All bucket series keyed by statistic key (``tou_*``/``ulo_*``/``tier1/2``)."""
    tou = _period_daily(readings, PLAN_TOU)
    ulo = _period_daily(readings, PLAN_ULO)
    tiers = _tier_daily(readings, periods)
    result: dict[str, list[tuple[datetime, float]]] = {}
    for key, period in TOU_BUCKETS.items():
        result[key] = tou.get(period, [])
    for key, period in ULO_BUCKETS.items():
        result[key] = ulo.get(period, [])
    result["tier1"] = tiers["tier1"]
    result["tier2"] = tiers["tier2"]
    return result


# --------------------------------------------------------------------------- #
# Cost (energy line item)
# --------------------------------------------------------------------------- #


def _time_cost_points(
    readings: list[UsageReading], plan: str, prices: dict[str, float]
) -> list[tuple[datetime, float]]:
    """Daily energy cost (dollars) for a TOU/ULO scheme: Σ period_kWh × rate."""
    daily = _period_daily(readings, plan)
    by_day: dict[datetime, float] = defaultdict(float)
    for period, points in daily.items():
        rate = prices.get(period)
        if rate is None:
            continue
        for day_start, kwh in points:
            by_day[day_start] += kwh * rate
    return sorted((day, cents / 100.0) for day, cents in by_day.items())


def _tier_cost_points(
    readings: list[UsageReading], tiered: TieredRates, periods: list[BillingPeriod]
) -> list[tuple[datetime, float]]:
    """Daily energy cost (dollars) for the Tiered plan."""
    daily = _tier_daily(readings, periods)
    by_day: dict[datetime, float] = defaultdict(float)
    for tier, rate in (("tier1", tiered.tier1), ("tier2", tiered.tier2)):
        for day_start, kwh in daily[tier]:
            by_day[day_start] += kwh * rate
    return sorted((day, cents / 100.0) for day, cents in by_day.items())


def cost_points(
    readings: list[UsageReading],
    plan: str,
    rates: list[TariffRate],
    tiered: TieredRates | None,
    periods: list[BillingPeriod],
) -> list[tuple[datetime, float]]:
    """Daily energy-cost points for ``plan`` (empty if its rates are unavailable)."""
    if plan == PLAN_TIERED:
        return _tier_cost_points(readings, tiered, periods) if tiered else []
    prices = plan_prices(rates, plan)
    return _time_cost_points(readings, plan, prices) if prices else []


def cost_total(
    readings: list[UsageReading],
    plan: str,
    rates: list[TariffRate],
    tiered: TieredRates | None,
    periods: list[BillingPeriod],
) -> float:
    """Total energy cost (dollars) across ``readings`` under ``plan``."""
    return sum(cost for _, cost in cost_points(readings, plan, rates, tiered, periods))


# --------------------------------------------------------------------------- #
# Import infrastructure (forward-only cumulative sum)
# --------------------------------------------------------------------------- #


def _normalize_start(value: object) -> datetime | None:
    """Normalize a ``get_last_statistics`` 'start' to a UTC-aware datetime."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def _flatten_points(readings: Iterable[UsageReading]) -> list[tuple[datetime, float]]:
    """Flatten readings to sorted ``(interval_start, kWh)``, dropping missing hours."""
    return sorted(
        (start, kwh)
        for reading in readings
        for start, kwh in reading.intervals()
        if kwh is not None
    )


def _build_statistics(
    points: list[tuple[datetime, float]],
    last_start: datetime | None,
    base_sum: float,
) -> list[StatisticData]:
    """Build forward-only statistics with a continuous cumulative sum."""
    stats: list[StatisticData] = []
    running = base_sum
    for start, value in points:
        if last_start is not None and start <= last_start:
            continue
        running += value
        stats.append(StatisticData(start=start, state=running, sum=running))
    return stats


async def _async_last_row(hass: HomeAssistant, statistic_id: str) -> dict | None:
    """Return the most recent stored statistic row for ``statistic_id``, or None."""
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )
    rows = last.get(statistic_id)
    return rows[0] if rows else None


async def async_last_statistic_start(
    hass: HomeAssistant, statistic_id: str
) -> datetime | None:
    """Return the start of the most recent stored statistic, or None if empty."""
    row = await _async_last_row(hass, statistic_id)
    return _normalize_start(row.get("start")) if row else None


async def _async_import_series(
    hass: HomeAssistant,
    statistic_id: str,
    name: str,
    points: list[tuple[datetime, float]],
    unit: str,
) -> int:
    """Import one forward-only sum statistic series; return points added."""
    if not points:
        return 0
    row = await _async_last_row(hass, statistic_id)
    base_sum = (row.get("sum") or 0.0) if row else 0.0
    last_start = _normalize_start(row.get("start")) if row else None

    stats = _build_statistics(points, last_start, base_sum)
    if not stats:
        return 0

    metadata = StatisticMetaData(
        has_mean=False,
        has_sum=True,
        name=name,
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_of_measurement=unit,
    )
    LOGGER.debug("Adding %d statistics points to %s", len(stats), statistic_id)
    async_add_external_statistics(hass, metadata, stats)
    return len(stats)


async def async_import_meter(
    hass: HomeAssistant,
    meter_id: str,
    readings: list[UsageReading],
    plan: str,
    rates: list[TariffRate],
    tiered: TieredRates | None,
    periods: list[BillingPeriod],
    currency: str,
) -> int:
    """Import a meter's consumption, all buckets, active cost, and cost_if_* series.

    Returns the number of consumption points added.
    """
    kwh = UnitOfEnergy.KILO_WATT_HOUR
    added = await _async_import_series(
        hass,
        consumption_statistic_id(meter_id),
        f"Enova Power consumption ({meter_id})",
        _flatten_points(readings),
        kwh,
    )

    for key, points in bucket_points(readings, periods).items():
        await _async_import_series(
            hass,
            bucket_statistic_id(meter_id, key),
            f"Enova Power {key.replace('_', ' ')} ({meter_id})",
            points,
            kwh,
        )

    # Active-plan energy cost.
    await _async_import_series(
        hass,
        cost_statistic_id(meter_id),
        f"Enova Power energy cost ({meter_id})",
        cost_points(readings, plan, rates, tiered, periods),
        currency,
    )

    # What-if energy cost under each plan (plan comparison).
    for scheme_plan in (PLAN_TOU, PLAN_ULO, PLAN_TIERED):
        await _async_import_series(
            hass,
            cost_if_statistic_id(meter_id, scheme_plan),
            f"Enova Power cost if {_SCHEME[scheme_plan]} ({meter_id})",
            cost_points(readings, scheme_plan, rates, tiered, periods),
            currency,
        )

    return added
