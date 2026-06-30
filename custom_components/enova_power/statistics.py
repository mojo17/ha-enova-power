"""Import Enova Power usage into Home Assistant long-term statistics.

Energy data is historical and lagged, so it belongs in external statistics
(not a live sensor): this backfills the Energy dashboard with real history.
Each hourly interval from ``UsageReading.intervals()`` is already a
timezone-aware UTC, hour-aligned timestamp. Missing hours (``None``) are
skipped — never written as a real ``0 kWh``.

External statistics carry an absolute cumulative ``sum``, so imports must be
**forward-only**: we resume the running sum from the last stored point and skip
anything at or before it. That also makes re-imports idempotent.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from enovapower import UsageReading

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
    PERIOD_TIERED,
    PERIOD_ULO_OVERNIGHT,
    PLAN_TIERED,
    PLAN_TOU,
    PLAN_ULO,
)


def consumption_statistic_id(meter_id: str) -> str:
    """Return the external statistic_id for a meter's total consumption."""
    return f"{DOMAIN}:energy_consumption_{meter_id}"


# Daily Time-of-Use buckets the portal classifies in every reading. Keyed by
# statistic suffix → UsageReading attribute.
TOU_BUCKETS: dict[str, str] = {
    "on_peak": "total_on_peak",
    "mid_peak": "total_mid_peak",
    "off_peak": "total_off_peak",
}


def bucket_statistic_id(meter_id: str, bucket: str) -> str:
    """Return the external statistic_id for a meter's TOU bucket (e.g. on_peak)."""
    return f"{DOMAIN}:energy_{bucket}_{meter_id}"


def cost_statistic_id(meter_id: str) -> str:
    """Return the external statistic_id for a meter's estimated cost."""
    return f"{DOMAIN}:energy_cost_{meter_id}"


# period → (tariff plan name, rate name) the library scrapes from the price
# table. TOU names are confirmed; ULO/Tiered names are a best guess until
# validated against a real portal export, so plan_prices returns only what it
# matches and callers degrade gracefully on misses.
PLAN_PRICE_NAMES: dict[str, dict[str, tuple[str, str]]] = {
    PLAN_TOU: {
        PERIOD_ON_PEAK: ("Time-of-Use", "TOU On-peak"),
        PERIOD_MID_PEAK: ("Time-of-Use", "TOU Mid-peak"),
        PERIOD_OFF_PEAK: ("Time-of-Use", "TOU Off-peak"),
    },
    PLAN_ULO: {
        PERIOD_ULO_OVERNIGHT: ("Ultra-Low Overnight", "ULO Overnight"),
        PERIOD_OFF_PEAK: ("Ultra-Low Overnight", "ULO Off-peak"),
        PERIOD_MID_PEAK: ("Ultra-Low Overnight", "ULO Mid-peak"),
        PERIOD_ON_PEAK: ("Ultra-Low Overnight", "ULO On-peak"),
    },
    PLAN_TIERED: {
        PERIOD_TIERED: ("Tiered", "Tier 1"),
    },
}

# TOU buckets cost computation requires.
COST_PERIODS = (PERIOD_ON_PEAK, PERIOD_MID_PEAK, PERIOD_OFF_PEAK)


def plan_prices(rates: Iterable, plan: str) -> dict[str, float]:
    """Map scraped tariff rates to ``{period: cents_per_kWh}`` for ``plan``.

    Returns only the periods whose (plan, rate name) was found, so a partial
    or empty result is possible if the portal's names differ from expectations.
    """
    names = PLAN_PRICE_NAMES.get(plan, {})
    by_key = {(r.plan, r.name): r.price for r in rates}
    return {period: by_key[key] for period, key in names.items() if key in by_key}


def _normalize_start(value: object) -> datetime | None:
    """Normalize a ``get_last_statistics`` 'start' to a UTC-aware datetime.

    HA has returned this as either a float unix timestamp or a ``datetime``
    across versions, so handle both (and assume UTC for naive datetimes).
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def _flatten_points(
    readings: Iterable[UsageReading],
) -> list[tuple[datetime, float]]:
    """Flatten readings to sorted ``(interval_start, kWh)``, dropping missing hours."""
    return sorted(
        (start, kwh)
        for reading in readings
        for start, kwh in reading.intervals()
        if kwh is not None
    )


def _daily_points(
    readings: Iterable[UsageReading], attr: str
) -> list[tuple[datetime, float]]:
    """Sorted ``(day_start, value)`` points for a daily TOU bucket attribute.

    The day start is the reading's first hourly interval (``h01``), which the
    library already maps to a UTC-aware, hour-aligned timestamp.
    """
    points: list[tuple[datetime, float]] = []
    for reading in readings:
        intervals = reading.intervals()
        if not intervals:
            continue
        points.append((intervals[0][0], getattr(reading, attr)))
    return sorted(points)


def _cost_points(
    readings: Iterable[UsageReading], prices: dict[str, float]
) -> list[tuple[datetime, float]]:
    """Daily ``(day_start, cost)`` points: TOU kWh × cents/kWh, in dollars.

    Cost is an estimate of the energy line item only — it excludes delivery,
    regulatory charges, rebates and tax.
    """
    points: list[tuple[datetime, float]] = []
    for reading in readings:
        intervals = reading.intervals()
        if not intervals:
            continue
        cents = (
            reading.total_on_peak * prices["on_peak"]
            + reading.total_mid_peak * prices["mid_peak"]
            + reading.total_off_peak * prices["off_peak"]
        )
        points.append((intervals[0][0], cents / 100.0))
    return sorted(points)


def _build_statistics(
    points: list[tuple[datetime, float]],
    last_start: datetime | None,
    base_sum: float,
) -> list[StatisticData]:
    """Build forward-only statistics with a continuous cumulative sum.

    Points at or before ``last_start`` are skipped (already imported); the
    running ``sum`` resumes from ``base_sum`` (the last stored sum).
    """
    stats: list[StatisticData] = []
    running = base_sum
    for start, kwh in points:
        if last_start is not None and start <= last_start:
            continue
        running += kwh
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
    unit: str = UnitOfEnergy.KILO_WATT_HOUR,
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


async def async_import_cost(
    hass: HomeAssistant,
    meter_id: str,
    readings: Iterable[UsageReading],
    prices: dict[str, float],
    currency: str,
) -> int:
    """Import an estimated daily cost statistic from TOU usage × prices."""
    return await _async_import_series(
        hass,
        cost_statistic_id(meter_id),
        f"Enova Power cost ({meter_id})",
        _cost_points(readings, prices),
        unit=currency,
    )


async def async_import_statistics(
    hass: HomeAssistant, meter_id: str, readings: Iterable[UsageReading]
) -> int:
    """Import a meter's consumption plus per-day TOU buckets; return total points.

    Total consumption is hourly (from ``intervals()``); the on/mid/off-peak
    buckets are daily (the portal's own classification in each reading).
    """
    readings = list(readings)

    added = await _async_import_series(
        hass,
        consumption_statistic_id(meter_id),
        f"Enova Power consumption ({meter_id})",
        _flatten_points(readings),
    )

    for bucket, attr in TOU_BUCKETS.items():
        await _async_import_series(
            hass,
            bucket_statistic_id(meter_id, bucket),
            f"Enova Power {bucket.replace('_', '-')} ({meter_id})",
            _daily_points(readings, attr),
        )

    return added
