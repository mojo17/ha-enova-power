"""Import Enova Power usage into Home Assistant long-term statistics.

Energy data is historical and lagged, so it belongs in external statistics (not
a live sensor): this backfills the Energy dashboard with real history.

All series are **hourly** — the source's own resolution (the portal publishes
``h01``–``h24`` per day) and the finest granularity long-term statistics can
hold — so hourly charts attribute buckets and costs to the hours the energy
was used. ``STATS_VERSION`` tracks this format: bumping it makes setup clear
and rebuild the affected series from a full re-download (imports are
forward-only, so a granularity change can't be fixed in place).

Buckets are **usage classifications**, computed from the hourly intervals by
local Ontario wall-clock time (verified to match the portal's own TOU totals —
see ``schedule.period_for_interval``). All plan schemes are classified for every
account (plan-independent), so a plan change never orphans a bucket; only the
active ``energy_cost`` series changes which rates it applies. Cost is the energy
line item only (excludes delivery, regulatory charges, rebates and tax); the
actual all-in bill is surfaced separately as ``last_bill_amount``. The portal
only exposes *current* rates (querying a past range still returns today's
rates), so cost applies the current rates/threshold to all history — an
approximation that self-corrects going forward as rates update on May 1 / Nov 1.

Each kWh bucket also gets a paired **cost series** (``cost_<bucket>_<meter>``),
priced at its scheme's current rates, so buckets can be tracked with costs in
the Energy dashboard; a scheme's bucket costs sum to its ``cost_if_*`` series.

External statistics carry an absolute cumulative ``sum``, so imports are
**forward-only**: the running sum resumes from the last stored point and skips
anything at or before it, which also makes re-imports idempotent. The flip side
is that a series added by an upgrade can never fill history older than its
first import — ``expected_statistic_ids`` + ``async_missing_series`` let the
coordinator detect that case and refetch full history once so it backfills.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone

from enovapower import BillingPeriod, TariffRate, UsageReading

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
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


def bucket_cost_statistic_id(meter_id: str, key: str) -> str:
    """External statistic_id for a usage bucket's energy cost (CAD)."""
    return f"{DOMAIN}:cost_{key}_{meter_id}"


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
# Classification → hourly bucket points
# --------------------------------------------------------------------------- #


def _period_hourly(
    readings: Iterable[UsageReading], plan: str
) -> dict[str, list[tuple[datetime, float]]]:
    """``{period: [(hour_start, kWh)]}`` classifying each hour by local Ontario time.

    ``plan`` selects the schedule (TOU vs ULO). Points keep the source's hourly
    granularity — the same resolution as the consumption series — so charts
    attribute each bucket's energy to the hour it was actually used.
    """
    per_period: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for reading in readings:
        for utc_dt, kwh in reading.intervals():
            if kwh is None:
                continue
            per_period[period_for_interval(utc_dt, plan)].append((utc_dt, kwh))
    return {period: sorted(points) for period, points in per_period.items()}


def _cycle_key(d: date, periods: list[BillingPeriod]) -> tuple:
    """Billing cycle (``start < d <= end``) containing ``d``; else its month."""
    for p in periods:
        if p.start_date < d <= p.end_date:
            return ("cycle", p.end_date)
    return ("month", d.year, d.month)


def _tier_hourly(
    readings: Iterable[UsageReading],
    periods: list[BillingPeriod],
    threshold_of: Callable[[date], float] = season_threshold,
) -> dict[str, list[tuple[datetime, float]]]:
    """``{'tier1'/'tier2': [(hour_start, kWh)]}`` — cumulative per billing cycle.

    Within each cycle the first ``threshold_of(date)`` kWh bill at Tier 1 and
    the rest at Tier 2, accumulated hour by hour so the threshold crossing
    lands in the hour it actually happens. Hours contributing nothing to a
    tier are omitted (an absent statistics row and a zero row sum the same).
    Cycles come from the billing report; days outside a known cycle fall back
    to their calendar month.
    """
    by_cycle: dict[tuple, list[tuple[datetime, float, date]]] = defaultdict(list)
    for reading in readings:
        for utc_dt, kwh in reading.intervals():
            if kwh is None:
                continue
            by_cycle[_cycle_key(reading.date, periods)].append((utc_dt, kwh, reading.date))

    tier1: list[tuple[datetime, float]] = []
    tier2: list[tuple[datetime, float]] = []
    for hours in by_cycle.values():
        cumulative = 0.0
        for hour_start, kwh, d in sorted(hours):
            threshold = threshold_of(d)
            below_before = min(cumulative, threshold)
            below_after = min(cumulative + kwh, threshold)
            t1 = below_after - below_before
            t2 = kwh - t1
            if t1 > 0.0:
                tier1.append((hour_start, t1))
            if t2 > 0.0:
                tier2.append((hour_start, t2))
            cumulative += kwh
    return {"tier1": sorted(tier1), "tier2": sorted(tier2)}


def bucket_points(
    readings: list[UsageReading], periods: list[BillingPeriod]
) -> dict[str, list[tuple[datetime, float]]]:
    """All bucket series keyed by statistic key (``tou_*``/``ulo_*``/``tier1/2``)."""
    tou = _period_hourly(readings, PLAN_TOU)
    ulo = _period_hourly(readings, PLAN_ULO)
    tiers = _tier_hourly(readings, periods)
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


def _period_cost_hourly(
    readings: list[UsageReading], plan: str, prices: dict[str, float]
) -> dict[str, list[tuple[datetime, float]]]:
    """``{period: [(hour_start, dollars)]}`` for a TOU/ULO scheme (priced periods only)."""
    result: dict[str, list[tuple[datetime, float]]] = {}
    for period, points in _period_hourly(readings, plan).items():
        rate = prices.get(period)
        if rate is not None:
            result[period] = [(start, kwh * rate / 100.0) for start, kwh in points]
    return result


def _tier_cost_hourly(
    readings: list[UsageReading], tiered: TieredRates, periods: list[BillingPeriod]
) -> dict[str, list[tuple[datetime, float]]]:
    """``{'tier1'/'tier2': [(hour_start, dollars)]}`` for the Tiered scheme."""
    hourly = _tier_hourly(readings, periods)
    return {
        tier: [(start, kwh * rate / 100.0) for start, kwh in hourly[tier]]
        for tier, rate in (("tier1", tiered.tier1), ("tier2", tiered.tier2))
    }


def _sum_by_start(
    series: Iterable[list[tuple[datetime, float]]],
) -> list[tuple[datetime, float]]:
    """Merge per-bucket points into one total-per-timestamp series."""
    by_start: dict[datetime, float] = defaultdict(float)
    for points in series:
        for start, value in points:
            by_start[start] += value
    return sorted(by_start.items())


def _time_cost_points(
    readings: list[UsageReading], plan: str, prices: dict[str, float]
) -> list[tuple[datetime, float]]:
    """Hourly energy cost (dollars) for a TOU/ULO scheme: hour_kWh × its rate."""
    return _sum_by_start(_period_cost_hourly(readings, plan, prices).values())


def _tier_cost_points(
    readings: list[UsageReading], tiered: TieredRates, periods: list[BillingPeriod]
) -> list[tuple[datetime, float]]:
    """Hourly energy cost (dollars) for the Tiered plan."""
    return _sum_by_start(_tier_cost_hourly(readings, tiered, periods).values())


def bucket_cost_points(
    readings: list[UsageReading],
    rates: list[TariffRate],
    tiered: TieredRates | None,
    periods: list[BillingPeriod],
) -> dict[str, list[tuple[datetime, float]]]:
    """Hourly energy-cost points per bucket key, at each scheme's current rates.

    Every scheme is priced regardless of the active plan (like the kWh buckets),
    so a scheme's bucket costs always sum to its ``cost_if_*`` series. Buckets
    whose rate is unavailable are omitted, not emitted empty.
    """
    result: dict[str, list[tuple[datetime, float]]] = {}
    for plan, buckets in ((PLAN_TOU, TOU_BUCKETS), (PLAN_ULO, ULO_BUCKETS)):
        cost_hourly = _period_cost_hourly(readings, plan, plan_prices(rates, plan))
        for key, period in buckets.items():
            if period in cost_hourly:
                result[key] = cost_hourly[period]
    if tiered:
        result.update(_tier_cost_hourly(readings, tiered, periods))
    return result


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


def expected_statistic_ids(
    meter_id: str, plan: str, rates: list[TariffRate], tiered: TieredRates | None
) -> list[str]:
    """Every statistic id ``async_import_meter`` would populate given these rates.

    The coordinator compares this against what the recorder already holds to
    spot series introduced by an upgrade: imports are forward-only, so a new
    series needs one full-history refetch to backfill (see ``_update_meter``).
    Cost ids appear only when their scheme's rates resolved, mirroring the
    import's own gating, so a missing rate can't trigger endless refetches.
    """
    ids = [consumption_statistic_id(meter_id)]
    ids += [bucket_statistic_id(meter_id, key) for key in ALL_BUCKET_KEYS]

    tou_prices = plan_prices(rates, PLAN_TOU)
    ulo_prices = plan_prices(rates, PLAN_ULO)
    for prices, buckets in ((tou_prices, TOU_BUCKETS), (ulo_prices, ULO_BUCKETS)):
        ids += [
            bucket_cost_statistic_id(meter_id, key)
            for key, period in buckets.items()
            if period in prices
        ]
    if tiered:
        ids += [bucket_cost_statistic_id(meter_id, key) for key in TIER_BUCKETS]

    active_priced = (
        tiered is not None if plan == PLAN_TIERED else bool(plan_prices(rates, plan))
    )
    if active_priced:
        ids.append(cost_statistic_id(meter_id))
    if tou_prices:
        ids.append(cost_if_statistic_id(meter_id, PLAN_TOU))
    if ulo_prices:
        ids.append(cost_if_statistic_id(meter_id, PLAN_ULO))
    if tiered:
        ids.append(cost_if_statistic_id(meter_id, PLAN_TIERED))
    return ids


# Statistics format version, stamped into the config entry after a rebuild.
# Bump when the shape of already-imported series changes. Version 2 = hourly
# bucket/cost granularity (version 1 imported one point per day). Version 3 =
# local-wall-clock timestamps (fixed-EST interpretation put summer hours one
# hour late) — this one shifts consumption too, so everything rebuilds.
STATS_VERSION = 3


def rebuild_statistic_ids(meter_id: str) -> list[str]:
    """The statistic ids cleared for a format rebuild — every series.

    Consumption is included: the v3 timestamp fix moves its summer points, so
    its history must be re-imported like everything else.
    """
    ids = [consumption_statistic_id(meter_id)]
    ids += [bucket_statistic_id(meter_id, key) for key in ALL_BUCKET_KEYS]
    ids += [bucket_cost_statistic_id(meter_id, key) for key in ALL_BUCKET_KEYS]
    ids.append(cost_statistic_id(meter_id))
    ids += [
        cost_if_statistic_id(meter_id, plan)
        for plan in (PLAN_TOU, PLAN_ULO, PLAN_TIERED)
    ]
    return ids


def async_start_rebuild(hass: HomeAssistant, meter_ids: list[str]) -> None:
    """Queue clearing of the outdated-format series. Fire-and-forget by design.

    The recorder does not process its task queue until Home Assistant has
    fully started, so setup must never *wait* on it — during bootstrap that
    deadlocks into the stage-2 timeout and setup gets cancelled. Correctness
    doesn't need the wait: the rebuild cycle imports these series with
    ``rebuild=True`` (ignoring whatever rows are still stored), and the
    recorder executes this clear before those imports because both go through
    its queue in order.
    """
    ids = [sid for meter_id in meter_ids for sid in rebuild_statistic_ids(meter_id)]
    get_instance(hass).async_clear_statistics(ids)
    LOGGER.info("Queued %d statistics series to be cleared for a format rebuild", len(ids))


def _missing_series(hass: HomeAssistant, ids: list[str]) -> list[str]:
    """The subset of ``ids`` with no stored statistics (runs in the recorder executor)."""
    return [
        statistic_id
        for statistic_id in ids
        if not get_last_statistics(hass, 1, statistic_id, True, {"sum"}).get(statistic_id)
    ]


async def async_missing_series(hass: HomeAssistant, ids: list[str]) -> list[str]:
    """Return the ids from ``ids`` that have no stored statistics yet."""
    return await get_instance(hass).async_add_executor_job(_missing_series, hass, ids)


async def _async_import_series(
    hass: HomeAssistant,
    statistic_id: str,
    name: str,
    points: list[tuple[datetime, float]],
    unit: str,
    *,
    fresh: bool = False,
) -> float | None:
    """Import one forward-only sum statistic series.

    Returns the series' cumulative sum after the import — the value its last
    row will carry once the recorder flushes (computed here rather than read
    back, since recorder writes are queued) — or None if the series has never
    stored a point.

    With ``fresh=True`` any stored rows are ignored (sum restarts at zero,
    nothing is filtered): used by the format rebuild, whose queued clear may
    not have executed yet when this runs.
    """
    row = None if fresh else await _async_last_row(hass, statistic_id)
    base_sum = (row.get("sum") or 0.0) if row else 0.0
    last_start = _normalize_start(row.get("start")) if row else None

    stats = _build_statistics(points, last_start, base_sum)
    if not stats:
        return base_sum if row else None

    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=name,
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_of_measurement=unit,
    )
    LOGGER.debug("Adding %d statistics points to %s", len(stats), statistic_id)
    async_add_external_statistics(hass, metadata, stats)
    return stats[-1]["sum"]


async def async_import_meter(
    hass: HomeAssistant,
    meter_id: str,
    readings: list[UsageReading],
    plan: str,
    rates: list[TariffRate],
    tiered: TieredRates | None,
    periods: list[BillingPeriod],
    currency: str,
    *,
    rebuild: bool = False,
) -> float | None:
    """Import a meter's consumption, all buckets, active cost, and cost_if_* series.

    ``rebuild=True`` re-imports every series from scratch, consumption
    included (see ``async_start_rebuild`` and ``rebuild_statistic_ids``).

    Returns the consumption series' cumulative sum — the meter's lifetime kWh
    since the first backfill (None until anything has been stored).
    """
    kwh = UnitOfEnergy.KILO_WATT_HOUR
    total = await _async_import_series(
        hass,
        consumption_statistic_id(meter_id),
        f"Enova Power consumption ({meter_id})",
        _flatten_points(readings),
        kwh,
        fresh=rebuild,
    )

    for key, points in bucket_points(readings, periods).items():
        await _async_import_series(
            hass,
            bucket_statistic_id(meter_id, key),
            f"Enova Power {key.replace('_', ' ')} ({meter_id})",
            points,
            kwh,
            fresh=rebuild,
        )

    # Per-bucket energy cost, pairable with the kWh buckets in the Energy dashboard.
    for key, points in bucket_cost_points(readings, rates, tiered, periods).items():
        await _async_import_series(
            hass,
            bucket_cost_statistic_id(meter_id, key),
            f"Enova Power {key.replace('_', ' ')} cost ({meter_id})",
            points,
            currency,
            fresh=rebuild,
        )

    # Active-plan energy cost.
    await _async_import_series(
        hass,
        cost_statistic_id(meter_id),
        f"Enova Power energy cost ({meter_id})",
        cost_points(readings, plan, rates, tiered, periods),
        currency,
        fresh=rebuild,
    )

    # What-if energy cost under each plan (plan comparison).
    for scheme_plan in (PLAN_TOU, PLAN_ULO, PLAN_TIERED):
        await _async_import_series(
            hass,
            cost_if_statistic_id(meter_id, scheme_plan),
            f"Enova Power cost if {_SCHEME[scheme_plan]} ({meter_id})",
            cost_points(readings, scheme_plan, rates, tiered, periods),
            currency,
            fresh=rebuild,
        )

    return total
