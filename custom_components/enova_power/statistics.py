"""Import Enova Power usage into Home Assistant long-term statistics.

Energy data is historical and lagged, so it belongs in external statistics (not
a live sensor): this backfills the Energy dashboard with real history.

All series are **hourly** — the source's own resolution (the portal publishes
``h01``–``h24`` per day) and the finest granularity long-term statistics can
hold — so hourly charts attribute buckets and costs to the hours the energy
was used. ``STATS_VERSION`` tracks this format (and, as of v5, forces a
one-time repair of already-stored series; see below).

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

External statistics carry an absolute cumulative ``sum``. Every series of a
meter shares one **download window** — the coordinator's ``[from_date, today]``
as UTC hour bounds — and the set of **covered days** (local dates the download
returned with at least one published hour). When the window overlaps rows
already stored, the import **anchors, merges and rewrites**: it anchors on the
last stored row *before* the window, then re-derives one continuous chain over
the stored rows from the window on and the new points (``_merge_statistics``):
new points replace stored hours; a stored in-window hour on a covered day that
the download no longer carries becomes a zero increment (so a revised tier
split or a revised preliminary day can't leave stale plateaus behind); stored
hours on uncovered days and rows after the window keep their stored increments,
re-based on the new chain. This is idempotent when nothing changed, and it
heals the portal's publication pattern — a day first appears as a preliminary
row with the whole total in the first hour slot and explicit zeros elsewhere,
then gets revised to real hourly values a day later. History older than the
window is never touched; a series added by an upgrade still can't backfill on
its own — ``expected_statistic_ids`` + ``async_missing_series`` let the
coordinator detect that case and refetch full history once.

The same pre-write read doubles as an **integrity check**: a cumulative sum
must never fall, so ``find_sum_drops`` over the anchor plus the stored rows
reports every hour whose sum is below its predecessor's. ``async_import_meter``
returns those per series (``ImportResult.broken``) and ``async_scan_series``
runs the check over a series' full history once at startup; the coordinator
heals a broken meter by re-importing it from its oldest stored date — the same
in-place merge-and-check import used for every other cycle, no clear involved.
``STATS_VERSION`` triggers that heal unconditionally once per entry: bumping it
makes the coordinator's first refresh treat every meter as broken regardless of
what the check finds, repairing whatever shape the previous version left behind
(daily granularity, misclassified summer hours, or a corrupted sum) from the
meter's oldest stored date. There is no separate rebuild path any more — imports
are still forward-only in the sense that they never invent history the portal
doesn't return, but a version bump no longer needs one, because the merge
already rewrites any window in place.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from itertools import pairwise

from enovapower import BillingPeriod, TariffRate, UsageReading

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    STATISTIC_UNIT_TO_UNIT_CONVERTER,
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import slugify

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
    TIME_ZONE,
)
from .schedule import period_for_interval

# --------------------------------------------------------------------------- #
# Statistic IDs
# --------------------------------------------------------------------------- #


def _meter_slug(meter_id: str) -> str:
    """Normalize a meter ID for HA external statistic_ids (lowercase [a-z0-9_])."""
    slug = slugify(meter_id)
    if not slug:
        raise ValueError(f"Invalid meter_id for statistic_id: {meter_id!r}")
    return slug


def consumption_statistic_id(meter_id: str) -> str:
    """External statistic_id for a meter's total consumption (kWh)."""
    return f"{DOMAIN}:energy_consumption_{_meter_slug(meter_id)}"


def bucket_statistic_id(meter_id: str, key: str) -> str:
    """External statistic_id for a usage bucket (e.g. ``tou_on_peak``, ``tier1``)."""
    return f"{DOMAIN}:energy_{key}_{_meter_slug(meter_id)}"


def bucket_cost_statistic_id(meter_id: str, key: str) -> str:
    """External statistic_id for a usage bucket's energy cost (CAD)."""
    return f"{DOMAIN}:cost_{key}_{_meter_slug(meter_id)}"


def cost_statistic_id(meter_id: str) -> str:
    """External statistic_id for a meter's active-plan energy cost (CAD)."""
    return f"{DOMAIN}:energy_cost_{_meter_slug(meter_id)}"


def cost_if_statistic_id(meter_id: str, plan: str) -> str:
    """External statistic_id for a what-if energy cost under ``plan`` (CAD)."""
    return f"{DOMAIN}:cost_if_{_SCHEME[plan]}_{_meter_slug(meter_id)}"


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
    """``{period: [(hour_start, dollars)]}`` for a TOU/ULO scheme.

    Every priced period is present — empty when the download has no hours in
    it — so its cost series is still imported (and stale rows on covered days
    zeroed); unpriced periods are omitted.
    """
    hourly = _period_hourly(readings, plan)
    return {
        period: [(start, kwh * rate / 100.0) for start, kwh in hourly.get(period, [])]
        for period, rate in prices.items()
    }


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


def _row_point(row: dict | None) -> tuple[datetime, float] | None:
    """A recorder row as a ``(start_utc, sum)`` point; None if either is unusable."""
    if not row or row.get("sum") is None:
        return None
    start = _normalize_start(row.get("start"))
    return (start, float(row["sum"])) if start is not None else None


def _flatten_points(readings: Iterable[UsageReading]) -> list[tuple[datetime, float]]:
    """Flatten readings to sorted ``(interval_start, kWh)``, dropping missing hours."""
    return sorted(
        (start, kwh)
        for reading in readings
        for start, kwh in reading.intervals()
        if kwh is not None
    )


# The download window as UTC hour bounds ``(window_start, window_end)``, both
# inclusive: the first hour of the first downloaded local day through the last
# hour of the last one. Shared by every series of a meter.
Window = tuple[datetime, datetime]


def download_window(from_date: date, today: date) -> Window:
    """The UTC hour bounds of a download covering local dates ``from_date..today``."""
    start = datetime.combine(from_date, time(0), tzinfo=TIME_ZONE)
    end = datetime.combine(today, time(23), tzinfo=TIME_ZONE)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def days_covered(readings: Iterable[UsageReading]) -> set[date]:
    """Local dates the download actually reported (≥ 1 published hour)."""
    return {r.date for r in readings if any(kwh is not None for _, kwh in r.intervals())}


def _stored_rows(
    hass: HomeAssistant, statistic_ids: set[str], start: datetime
) -> dict[str, list[tuple[datetime, float]]]:
    """Stored ``(start_utc, sum)`` rows from ``start`` on, ascending per id (executor).

    One batched read for all of a meter's series. Rows the recorder returns
    without a usable start or sum are skipped — there is no increment to keep.
    """
    result = statistics_during_period(
        hass, start, None, statistic_ids, "hour", None, {"sum"}
    )
    return {
        statistic_id: sorted(
            (start_utc, float(row["sum"]))
            for row in rows
            if (start_utc := _normalize_start(row.get("start"))) is not None
            and row.get("sum") is not None
        )
        for statistic_id, rows in result.items()
    }


async def _async_stored_rows(
    hass: HomeAssistant, statistic_ids: set[str], start: datetime
) -> dict[str, list[tuple[datetime, float]]]:
    """Async wrapper for :func:`_stored_rows`."""
    return await get_instance(hass).async_add_executor_job(
        _stored_rows, hass, statistic_ids, start
    )


def _merge_statistics(
    points: list[tuple[datetime, float]],
    base_sum: float,
    stored: list[tuple[datetime, float]],
    window: Window,
    covered: Callable[[datetime], bool],
) -> list[StatisticData]:
    """The rows to write so the series is one continuous chain from the window on.

    ``base_sum`` is the anchor's sum (the last stored row before the window; 0
    if none) and ``stored`` the stored rows from ``window_start`` on, ascending.
    ``covered(start)`` says whether the download covered that hour's local day.
    Points must lie inside the window; stored rows before it are out of scope.
    Returns ``[]`` when the chain would be unchanged (no points, nothing to zero).
    """
    window_start, window_end = window
    # Sum the new points per hour (duplicate timestamps add up).
    new: dict[datetime, float] = defaultdict(float)
    for start, value in points:
        new[start] += value

    # Walk the stored rows in order, tracking each row's stored increment:
    #   hour also in the new points        → the point replaces it
    #   in-window hour on a covered day    → zero increment (no longer reported)
    #   otherwise (uncovered day, or tail) → keep the stored increment
    increments: dict[datetime, float] = dict(new)
    zeroed = False
    previous = base_sum
    for start, stored_sum in stored:
        if start < window_start:
            continue
        if start not in new:
            if start <= window_end and covered(start):
                increments[start] = 0.0
                zeroed = True
            else:
                increments[start] = stored_sum - previous
        previous = stored_sum
    if not new and not zeroed:
        return []

    # Accumulate the increments in time order from the anchor sum.
    stats: list[StatisticData] = []
    running = base_sum
    for start, increment in sorted(increments.items()):
        running += increment
        stats.append(StatisticData(start=start, state=running, sum=running))
    return stats


async def _async_last_row(hass: HomeAssistant, statistic_id: str) -> dict | None:
    """Return the most recent stored statistic row for ``statistic_id``, or None."""
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )
    rows = last.get(statistic_id)
    return rows[0] if rows else None


# How far back to look for the anchor row in one query before falling back to
# a full-history scan (only relevant for series with months-long gaps).
_ANCHOR_LOOKBACK = timedelta(days=90)

# Start of a full-history read.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _row_before(hass: HomeAssistant, statistic_id: str, before: datetime) -> dict | None:
    """The last stored row strictly before ``before`` (recorder executor)."""
    for start in (before - _ANCHOR_LOOKBACK, _EPOCH):
        rows = statistics_during_period(
            hass, start, before, {statistic_id}, "hour", None, {"sum"}
        ).get(statistic_id)
        if rows:
            return rows[-1]
    return None


async def _async_row_before(
    hass: HomeAssistant, statistic_id: str, before: datetime
) -> dict | None:
    """Async wrapper for :func:`_row_before`."""
    return await get_instance(hass).async_add_executor_job(
        _row_before, hass, statistic_id, before
    )


async def async_last_statistic_start(
    hass: HomeAssistant, statistic_id: str
) -> datetime | None:
    """Return the start of the most recent stored statistic, or None if empty."""
    row = await _async_last_row(hass, statistic_id)
    return _normalize_start(row.get("start")) if row else None


# --------------------------------------------------------------------------- #
# Integrity check (cumulative sums must never fall)
# --------------------------------------------------------------------------- #


def find_sum_drops(
    rows: list[tuple[datetime, float]], tolerance: float = 1e-6
) -> list[datetime]:
    """Start times of the rows whose sum falls below the previous row's.

    ``rows`` are ascending ``(start, sum)``; callers checking a window pass the
    anchor row first so a break at the first stored row is caught too. A fall
    of at most ``tolerance`` is float noise, not a drop.
    """
    return [
        start
        for (_, previous_sum), (start, current_sum) in pairwise(rows)
        if current_sum < previous_sum - tolerance
    ]


@dataclass(frozen=True)
class SeriesScan:
    """A series' full-history check: its first stored hour and its sum drops."""

    first_start: datetime | None  # None → the series has no rows
    drops: list[datetime]


def _scan_series(hass: HomeAssistant, ids: list[str]) -> dict[str, SeriesScan]:
    """Check every series in ``ids`` over its full history (recorder executor).

    One batched read for the whole meter; every requested id gets an entry,
    so a series with no rows scans clean with ``first_start`` None.
    """
    stored = _stored_rows(hass, set(ids), _EPOCH)
    scans: dict[str, SeriesScan] = {}
    for statistic_id in ids:
        rows = stored.get(statistic_id, [])
        first_start = rows[0][0] if rows else None
        scans[statistic_id] = SeriesScan(first_start, find_sum_drops(rows))
    return scans


async def async_scan_series(hass: HomeAssistant, ids: list[str]) -> dict[str, SeriesScan]:
    """Async wrapper for :func:`_scan_series`."""
    return await get_instance(hass).async_add_executor_job(_scan_series, hass, ids)


@dataclass(frozen=True)
class SeriesResult:
    """One series' import: its cumulative sum after the write and the drops
    its pre-write read found (empty when nothing overlapping was read)."""

    total: float | None
    drops: list[datetime]


@dataclass(frozen=True)
class ImportResult:
    """A meter's import: consumption total and the broken series' drop times."""

    total: float | None
    broken: dict[str, list[datetime]]


def _priced(plan: str, rates: list[TariffRate], tiered: TieredRates | None) -> bool:
    """Whether ``plan``'s energy cost can be computed from the scraped rates."""
    return tiered is not None if plan == PLAN_TIERED else bool(plan_prices(rates, plan))


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

    if _priced(plan, rates, tiered):
        ids.append(cost_statistic_id(meter_id))
    if tou_prices:
        ids.append(cost_if_statistic_id(meter_id, PLAN_TOU))
    if ulo_prices:
        ids.append(cost_if_statistic_id(meter_id, PLAN_ULO))
    if tiered:
        ids.append(cost_if_statistic_id(meter_id, PLAN_TIERED))
    return ids


# Statistics format version, stamped into the config entry after a successful
# repair cycle. Bump to force every meter through one full in-place re-import
# from its oldest stored date (see ``EnovaPowerCoordinator._full_reimport_from``
# and heal rule 3), regardless of what the integrity check finds — the same
# merge-and-check machinery as any other heal, no clear involved. Version 2 =
# hourly bucket/cost granularity (version 1 imported one point per day).
# Version 3 = local-wall-clock timestamps (fixed-EST interpretation put summer
# hours one hour late). Version 4 = heal days permanently locked in their
# preliminary published shape (whole total in the first hour) by the old
# forward-only import. Version 5 = repair only, no format change: heals the
# July-2026-style sum drops and stale tier rows a stale/incomplete read could
# leave behind, in place, from each meter's oldest stored date — history older
# than the 12-month backfill window is never lost, since nothing is cleared.
STATS_VERSION = 5


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
    window: Window,
    covered_days: set[date],
    stored: list[tuple[datetime, float]] | None = None,
) -> SeriesResult:
    """Import one cumulative-sum statistic series over the download ``window``.

    Returns the series' cumulative sum after the import — the value its last
    row will carry once the recorder flushes (computed here rather than read
    back, since recorder writes are queued) — or None if the series has never
    stored a point — together with the sum drops the pre-write read found
    (anchor row + stored rows; see ``find_sum_drops``).

    When stored rows exist from ``window_start`` on, the import anchors on the
    last stored row *before* the window and rewrites one continuous chain over
    the stored rows and the new points (see ``_merge_statistics``); otherwise
    it appends after the newest stored row with no further read — and no
    check, since nothing overlapping was read. ``stored`` carries the
    meter-wide batched read (None → read this series now).
    """
    window_start, window_end = window
    inside = [p for p in points if window_start <= p[0] <= window_end]
    if len(inside) != len(points):
        LOGGER.warning(
            "Dropping %d points outside the download window for %s",
            len(points) - len(inside),
            statistic_id,
        )

    row = None
    drops: list[datetime] = []
    if stored is None:
        stored = (await _async_stored_rows(hass, {statistic_id}, window_start)).get(
            statistic_id, []
        )
    if stored:
        # Stored rows overlap the window: rewrite the chain from the anchor,
        # and check the chain the read saw (anchor first).
        row = await _async_row_before(hass, statistic_id, window_start)
        anchor = _row_point(row)
        drops = find_sum_drops(([anchor] if anchor else []) + stored)
    else:
        # Pure append: the newest stored row is the anchor.
        row = await _async_last_row(hass, statistic_id)
    base_sum = (row.get("sum") or 0.0) if row else 0.0

    def covered(start: datetime) -> bool:
        return start.astimezone(TIME_ZONE).date() in covered_days

    stats = _merge_statistics(inside, base_sum, stored, window, covered)
    if not stats:
        # Chain unchanged: the series still ends on its last stored sum.
        if stored:
            return SeriesResult(stored[-1][1], drops)
        return SeriesResult(base_sum if row else None, drops)

    # unit_class must be stated explicitly (required from HA 2026.11):
    # "energy" for kWh; None for units with no converter (currency).
    converter = STATISTIC_UNIT_TO_UNIT_CONVERTER.get(unit)
    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=name,
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_of_measurement=unit,
        unit_class=converter.UNIT_CLASS if converter else None,
    )
    LOGGER.debug("Adding %d statistics points to %s", len(stats), statistic_id)
    async_add_external_statistics(hass, metadata, stats)
    return SeriesResult(stats[-1]["sum"], drops)


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
    window: Window,
    covered_days: set[date],
) -> ImportResult:
    """Import a meter's consumption, all buckets, active cost, and cost_if_* series.

    ``window`` and ``covered_days`` describe the download ``readings`` came
    from (see ``download_window`` / ``days_covered``); every series is
    rewritten over that same window — including a full re-import from the
    meter's oldest stored date, when the coordinator sets ``window`` that far
    back (a heal, or the one-time ``STATS_VERSION`` repair; see
    ``EnovaPowerCoordinator._full_reimport_from``). Cost series whose scheme
    has no rates are not imported at all — their stored rows stay untouched
    rather than being zeroed as "no longer reported".

    Returns the consumption series' cumulative sum — the meter's lifetime kWh
    since the first backfill (None until anything has been stored) — and, per
    series whose stored chain had sum drops before this write, the drop times.
    """
    kwh = UnitOfEnergy.KILO_WATT_HOUR
    series: list[tuple[str, str, list[tuple[datetime, float]], str]] = [
        (
            consumption_statistic_id(meter_id),
            f"Enova Power consumption ({meter_id})",
            _flatten_points(readings),
            kwh,
        )
    ]
    series += [
        (
            bucket_statistic_id(meter_id, key),
            f"Enova Power {key.replace('_', ' ')} ({meter_id})",
            points,
            kwh,
        )
        for key, points in bucket_points(readings, periods).items()
    ]
    # Per-bucket energy cost, pairable with the kWh buckets in the Energy
    # dashboard (bucket_cost_points already omits unpriced schemes).
    series += [
        (
            bucket_cost_statistic_id(meter_id, key),
            f"Enova Power {key.replace('_', ' ')} cost ({meter_id})",
            points,
            currency,
        )
        for key, points in bucket_cost_points(readings, rates, tiered, periods).items()
    ]
    if _priced(plan, rates, tiered):
        series.append(
            (
                cost_statistic_id(meter_id),
                f"Enova Power energy cost ({meter_id})",
                cost_points(readings, plan, rates, tiered, periods),
                currency,
            )
        )
    # What-if energy cost under each priced plan (plan comparison).
    series += [
        (
            cost_if_statistic_id(meter_id, scheme_plan),
            f"Enova Power cost if {_SCHEME[scheme_plan]} ({meter_id})",
            cost_points(readings, scheme_plan, rates, tiered, periods),
            currency,
        )
        for scheme_plan in (PLAN_TOU, PLAN_ULO, PLAN_TIERED)
        if _priced(scheme_plan, rates, tiered)
    ]

    # One batched read of every series' stored rows from the window on.
    stored = await _async_stored_rows(hass, {sid for sid, *_ in series}, window[0])
    results = [
        await _async_import_series(
            hass,
            statistic_id,
            name,
            points,
            unit,
            window=window,
            covered_days=covered_days,
            stored=stored.get(statistic_id, []),
        )
        for statistic_id, name, points, unit in series
    ]
    broken = {
        statistic_id: result.drops
        for (statistic_id, *_), result in zip(series, results, strict=True)
        if result.drops
    }
    return ImportResult(results[0].total, broken)
