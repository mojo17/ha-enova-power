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

from enovapower import (
    AsyncEnovaClient,
    EnovaAuthError,
    EnovaError,
    TariffRate,
    UsageReading,
)

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
    PLAN_TIERED,
    PLAN_TOU,
    RECENT_DAYS,
    UPDATE_INTERVAL,
)
from .statistics import (
    COST_PERIODS,
    async_import_cost,
    async_import_statistics,
    async_import_tiered_cost,
    async_last_statistic_start,
    consumption_statistic_id,
    plan_prices,
    tiered_rates,
    tiered_total_cost,
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
        """Effective pricing plan.

        Priority: an explicit options override, then the plan auto-detected from
        the portal (``client.plan``, populated during the tariff fetch), then a
        legacy configured value (entries created before auto-detection), then the
        default.
        """
        entry = self.config_entry
        return (
            entry.options.get(CONF_PLAN)
            or self.client.plan
            or entry.data.get(CONF_PLAN)
            or DEFAULT_PLAN
        )

    async def _async_update_data(self) -> dict[str, MeterData]:
        """Fetch + import each meter; return latest reading and MTD totals."""
        today = date.today()
        month_start = today.replace(day=1)
        data: dict[str, MeterData] = {}
        try:
            rates = await self._fetch_rates(today)
            self.prices = plan_prices(rates, self.plan)
            tou_prices = (
                self.prices
                if self.plan == PLAN_TOU and set(COST_PERIODS) <= self.prices.keys()
                else None
            )
            tiered = tiered_rates(rates) if self.plan == PLAN_TIERED else None
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
                if tou_prices:
                    await async_import_cost(
                        self.hass, meter_id, readings, tou_prices, CURRENCY
                    )
                elif tiered:
                    await async_import_tiered_cost(
                        self.hass, meter_id, readings, tiered, CURRENCY
                    )

                month = [r for r in readings if r.date >= month_start]
                if tou_prices:
                    mtd_cost = total_cost(month, tou_prices)
                elif tiered:
                    mtd_cost = tiered_total_cost(month, tiered)
                else:
                    mtd_cost = None
                data[meter_id] = MeterData(
                    latest=max(readings, key=lambda r: r.date) if readings else None,
                    mtd_energy=sum(r.total for r in month),
                    mtd_cost=mtd_cost,
                )
        except EnovaAuthError as err:  # also covers EnovaSessionExpiredError
            raise ConfigEntryAuthFailed(str(err)) from err
        except EnovaError as err:  # also covers EnovaNetworkError + parse/form errors
            raise UpdateFailed(str(err)) from err

        return data

    async def _fetch_rates(self, today: date) -> list[TariffRate]:
        """Current tariff rates for all plans (best effort; empty on failure).

        Reduced to the active plan's period rates for the current-rate sensor,
        and used for the TOU/Tiered cost estimates. Historical cost uses these
        current rates/threshold (a documented approximation).
        """
        try:
            return await self.client.download_tariff(today - timedelta(days=30), today)
        except EnovaError as err:
            LOGGER.warning("Could not fetch tariff prices: %s", err)
            return []
