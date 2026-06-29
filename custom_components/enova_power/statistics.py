"""Import Enova Power usage into Home Assistant long-term statistics.

Energy data is historical and lagged, so it belongs in external statistics
(not a live sensor): this backfills the Energy dashboard with real history.
Each hourly interval from ``UsageReading.intervals()`` is already a
timezone-aware UTC, hour-aligned timestamp. Missing hours (``None``) are
skipped — never written as a real ``0 kWh``. Imports are idempotent: HA dedupes
on ``start`` and we resume the running ``sum`` from the last stored point.
"""

from __future__ import annotations

from collections.abc import Iterable

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


def consumption_statistic_id(meter_id: str | None) -> str:
    """Return the external statistic_id for a meter's consumption."""
    suffix = f"_{meter_id}" if meter_id else ""
    return f"{DOMAIN}:energy_consumption{suffix}"


async def async_import_statistics(
    hass: HomeAssistant,
    meter_id: str | None,
    readings: Iterable[UsageReading],
) -> None:
    """Import hourly consumption from readings into external statistics."""
    statistic_id = consumption_statistic_id(meter_id)

    # Flatten to (start, kWh) hourly points, dropping unreported hours.
    points = sorted(
        (start, kwh)
        for reading in readings
        for start, kwh in reading.intervals()
        if kwh is not None
    )
    if not points:
        return

    # Resume the running total from the last stored statistic, and skip points
    # we've already imported.
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )
    running_sum = 0.0
    last_start_ts = None
    if last.get(statistic_id):
        row = last[statistic_id][0]
        running_sum = row.get("sum") or 0.0
        last_start_ts = row.get("start")

    stats: list[StatisticData] = []
    for start, kwh in points:
        if last_start_ts is not None and start.timestamp() <= last_start_ts:
            continue
        running_sum += kwh
        stats.append(StatisticData(start=start, state=running_sum, sum=running_sum))

    if not stats:
        return

    metadata = StatisticMetaData(
        has_mean=False,
        has_sum=True,
        name=f"Enova Power consumption{f' ({meter_id})' if meter_id else ''}",
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )
    LOGGER.debug("Adding %d statistics points to %s", len(stats), statistic_id)
    async_add_external_statistics(hass, metadata, stats)
