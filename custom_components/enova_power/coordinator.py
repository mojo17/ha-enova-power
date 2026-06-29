"""Data update coordinator for Enova Power.

Each cycle fetches recent usage and imports it into Home Assistant long-term
statistics (the correct home for lagged historical energy data). On first run
it backfills ``BACKFILL_MONTHS`` of history. The coordinator's ``data`` is the
latest reading, used by the informational sensors.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

from enovapower import (
    AsyncEnovaClient,
    EnovaAuthError,
    EnovaNetworkError,
    EnovaSessionExpiredError,
    UsageReading,
)

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import BACKFILL_MONTHS, DOMAIN, LOGGER, RECENT_DAYS, UPDATE_INTERVAL
from .statistics import async_import_statistics

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


class EnovaPowerCoordinator(DataUpdateCoordinator[UsageReading | None]):
    """Coordinate Enova Power downloads and statistics imports."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: AsyncEnovaClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            config_entry=entry,
        )
        self.client = client
        self._backfilled = False

    async def _async_update_data(self) -> UsageReading | None:
        """Fetch usage, import statistics, and return the latest reading."""
        try:
            today = date.today()
            if not self._backfilled:
                from_date = today - timedelta(days=BACKFILL_MONTHS * 31)
                LOGGER.debug("Backfilling Enova Power usage from %s", from_date)
            else:
                from_date = today - timedelta(days=RECENT_DAYS)

            readings = await self.client.download_usage(from_date, today)
            await async_import_statistics(self.hass, self.client.meter_id, readings)
            self._backfilled = True

            return max(readings, key=lambda r: r.date) if readings else None
        except (EnovaAuthError, EnovaSessionExpiredError) as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except EnovaNetworkError as err:
            raise UpdateFailed(str(err)) from err
