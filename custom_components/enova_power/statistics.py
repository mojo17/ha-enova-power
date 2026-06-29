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

from .const import DOMAIN, LOGGER


def consumption_statistic_id(meter_id: str) -> str:
    """Return the external statistic_id for a meter's consumption."""
    return f"{DOMAIN}:energy_consumption_{meter_id}"


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


async def async_import_statistics(
    hass: HomeAssistant, meter_id: str, readings: Iterable[UsageReading]
) -> int:
    """Import hourly consumption into external statistics; return points added."""
    statistic_id = consumption_statistic_id(meter_id)
    points = _flatten_points(readings)
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
        name=f"Enova Power consumption ({meter_id})",
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )
    LOGGER.debug("Adding %d statistics points to %s", len(stats), statistic_id)
    async_add_external_statistics(hass, metadata, stats)
    return len(stats)
