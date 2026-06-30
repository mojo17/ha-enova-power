"""Data update coordinator for Enova Power.

Each cycle fetches usage and imports it into Home Assistant long-term
statistics. The download window is derived from the recorder, not an in-memory
flag: with no prior statistics it backfills ``BACKFILL_MONTHS`` of history
(once), and thereafter fetches incrementally — so restarts are cheap and the
cumulative statistics stay forward-only. Every meter on the account is fetched
and imported under its own ``statistic_id``; the coordinator's ``data`` maps
each meter id to its latest reading, used by the informational sensors.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from enovapower import AsyncEnovaClient, EnovaAuthError, EnovaError, UsageReading

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    BACKFILL_MONTHS,
    CONF_PLAN,
    CURRENCY,
    DEFAULT_PLAN,
    DOMAIN,
    LOGGER,
    PLAN_TOU,
    RECENT_DAYS,
    UPDATE_INTERVAL,
)
from .statistics import (
    COST_PERIODS,
    async_import_cost,
    async_import_statistics,
    async_last_statistic_start,
    consumption_statistic_id,
    plan_prices,
    total_cost,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


@dataclass
class MeterData:
    """Per-meter state exposed to sensors."""

    latest: UsageReading | None
    mtd_energy: float  # kWh consumed this calendar month, to the latest day
    mtd_cost: float | None  # estimated month-to-date cost (CAD), TOU only


def fetch_from_date(last_start: datetime | None, today: date) -> date:
    """Choose the download start date.

    No prior statistics → full historical backfill window. Otherwise an
    incremental window: from just before the last stored point (to fill any gap
    after downtime and catch late revisions), but never shorter than the recent
    window.
    """
    if last_start is None:
        return today - timedelta(days=BACKFILL_MONTHS * 31)
    return min(last_start.date() - timedelta(days=1), today - timedelta(days=RECENT_DAYS))


class EnovaPowerCoordinator(DataUpdateCoordinator[dict[str, "MeterData"]]):
    """Coordinate Enova Power downloads and statistics imports (per meter)."""

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
        # period → cents/kWh for the active plan, refreshed each cycle; read by
        # the current-rate sensor. Empty until the first successful tariff fetch.
        self.prices: dict[str, float] = {}

    @property
    def plan(self) -> str:
        """The configured pricing plan (options override config data)."""
        entry = self.config_entry
        return entry.options.get(CONF_PLAN) or entry.data.get(CONF_PLAN, DEFAULT_PLAN)

    async def _async_update_data(self) -> dict[str, MeterData]:
        """Fetch + import each meter; return latest reading and MTD totals."""
        today = date.today()
        month_start = today.replace(day=1)
        data: dict[str, MeterData] = {}
        try:
            self.prices = await self._fetch_prices(today)
            cost_prices = (
                self.prices
                if self.plan == PLAN_TOU and set(COST_PERIODS) <= self.prices.keys()
                else None
            )
            for meter_id in self.client.meter_ids:
                statistic_id = consumption_statistic_id(meter_id)
                last_start = await async_last_statistic_start(self.hass, statistic_id)
                # Always cover the current month so month-to-date can be summed.
                from_date = min(fetch_from_date(last_start, today), month_start)
                if last_start is None:
                    LOGGER.debug(
                        "No prior statistics for meter %s; backfilling from %s",
                        meter_id,
                        from_date,
                    )
                readings = await self.client.download_usage(
                    from_date, today, meter_id=meter_id
                )
                await async_import_statistics(self.hass, meter_id, readings)
                if cost_prices:
                    await async_import_cost(
                        self.hass, meter_id, readings, cost_prices, CURRENCY
                    )

                month = [r for r in readings if r.date >= month_start]
                data[meter_id] = MeterData(
                    latest=max(readings, key=lambda r: r.date) if readings else None,
                    mtd_energy=sum(r.total for r in month),
                    mtd_cost=total_cost(month, cost_prices) if cost_prices else None,
                )
        except EnovaAuthError as err:  # also covers EnovaSessionExpiredError
            raise ConfigEntryAuthFailed(str(err)) from err
        except EnovaError as err:  # also covers EnovaNetworkError + parse/form errors
            raise UpdateFailed(str(err)) from err

        return data

    async def _fetch_prices(self, today: date) -> dict[str, float]:
        """Current ``{period: cents/kWh}`` for the active plan (best effort).

        Read by the current-rate sensor and, for TOU, used for the cost
        estimate. Historical cost uses the current rates (a documented
        approximation).
        """
        try:
            rates = await self.client.download_tariff(today - timedelta(days=30), today)
        except EnovaError as err:
            LOGGER.warning("Could not fetch tariff prices: %s", err)
            return {}
        return plan_prices(rates, self.plan)
