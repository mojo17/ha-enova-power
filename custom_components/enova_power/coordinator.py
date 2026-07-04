"""Data update coordinator for Enova Power.

Each cycle fetches usage per meter and imports it into Home Assistant long-term
statistics. The download window is derived from the recorder (no in-memory flag):
with no prior statistics it backfills ``BACKFILL_MONTHS`` of history once, and
thereafter fetches incrementally but always covers the current billing cycle so
cycle-to-date totals are correct. Plans are resolved per meter (a subscriber can
be on different plans per meter); an account-wide options value overrides
detection. The coordinator's ``data`` maps each meter id to its ``MeterData``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from enovapower import (
    AsyncEnovaClient,
    BillingPeriod,
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
    CONF_STATS_VERSION,
    CURRENCY,
    DEFAULT_PLAN,
    DOMAIN,
    LOGGER,
    PLAN_TIERED,
    RECENT_DAYS,
    UPDATE_INTERVAL,
)
from .statistics import (
    STATS_VERSION,
    TieredRates,
    async_import_meter,
    async_last_statistic_start,
    async_missing_series,
    consumption_statistic_id,
    cost_total,
    expected_statistic_ids,
    season_threshold,
    tiered_rates,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


@dataclass
class MeterData:
    """Per-meter state exposed to sensors."""

    latest: UsageReading | None
    plan: str  # this meter's active plan
    cycle_energy: float  # kWh consumed this billing cycle, to the latest day
    cycle_cost: float | None  # estimated energy cost this cycle to date (CAD)
    last_bill: BillingPeriod | None  # most recent closed cycle (actual $)
    threshold: float | None  # current tier-1 kWh cap (Tiered only)
    lifetime_energy: float | None  # kWh since first import (the LTS cumulative sum)


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


def current_cycle_start(periods: list[BillingPeriod], today: date) -> date:
    """First day of the current (open) billing cycle.

    The last closed cycle's read date is the day before the current cycle begins;
    with no billing data, fall back to the calendar month.
    """
    if periods:
        return max(p.end_date for p in periods) + timedelta(days=1)
    return today.replace(day=1)


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
        # Account-wide scraped tariff rates (all plans), refreshed each cycle and
        # read by the rate-card and current-rate sensors. Empty until first fetch.
        self.rates: list[TariffRate] = []
        # Statistics-format rebuild pending: setup queued a clear of the old
        # series (see statistics.async_start_rebuild); this cycle must re-import
        # them from scratch over the full backfill window. Stamped complete
        # (and the flag dropped) only after a fully successful cycle, so any
        # failure retries the whole rebuild idempotently.
        self._rebuild = entry.data.get(CONF_STATS_VERSION, 1) < STATS_VERSION

    def plan_override(self) -> str | None:
        """Account-wide plan override (options, or a legacy configured value)."""
        entry = self.config_entry
        return entry.options.get(CONF_PLAN) or entry.data.get(CONF_PLAN)

    async def _meter_plan(self, meter_id: str) -> str:
        """Resolve a meter's plan: override, else portal detection, else default."""
        override = self.plan_override()
        if override:
            return override
        try:
            return await self.client.get_current_plan(meter_id) or DEFAULT_PLAN
        except EnovaError as err:
            LOGGER.warning("Could not detect plan for meter %s: %s", meter_id, err)
            return DEFAULT_PLAN

    async def _async_update_data(self) -> dict[str, MeterData]:
        """Fetch + import each meter; return per-meter state for the sensors."""
        today = date.today()
        data: dict[str, MeterData] = {}
        try:
            self.rates = await self._fetch_rates(today)
            tiered = tiered_rates(self.rates)
            for meter_id in self.client.meter_ids:
                data[meter_id] = await self._update_meter(meter_id, today, tiered)
        except EnovaAuthError as err:  # also covers EnovaSessionExpiredError
            raise ConfigEntryAuthFailed(str(err)) from err
        except EnovaError as err:  # also covers EnovaNetworkError + parse/form errors
            raise UpdateFailed(str(err)) from err
        if self._rebuild:
            # All meters re-imported in the new format; record it so the next
            # setup doesn't rebuild again. Runs before the entry's update
            # listener is registered (first refresh), so no reload is triggered.
            self._rebuild = False
            entry = self.config_entry
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_STATS_VERSION: STATS_VERSION}
            )
            LOGGER.info("Statistics format rebuild complete (v%s)", STATS_VERSION)
        return data

    async def _update_meter(
        self, meter_id: str, today: date, tiered: TieredRates | None
    ) -> MeterData:
        """Fetch, import, and summarize a single meter."""
        plan = await self._meter_plan(meter_id)
        try:
            periods = await self.client.billing_periods(meter_id)
        except EnovaError as err:
            LOGGER.warning("Could not fetch billing cycles for %s: %s", meter_id, err)
            periods = []

        cycle_start = current_cycle_start(periods, today)
        if self._rebuild:
            # Format rebuild: full backfill window, and skip the missing-series
            # check — the queued clear may not have run yet, so stored rows
            # can't be trusted either way (the fresh imports ignore them).
            last_start = None
        else:
            last_start = await async_last_statistic_start(
                self.hass, consumption_statistic_id(meter_id)
            )
            if last_start is not None:
                # Imports are forward-only, so a series added by an upgrade can
                # only get history older than its first point from a full
                # refetch now.
                missing = await async_missing_series(
                    self.hass, expected_statistic_ids(meter_id, plan, self.rates, tiered)
                )
                if missing:
                    LOGGER.info(
                        "Meter %s gained %d statistics series; refetching full "
                        "history once to backfill them",
                        meter_id,
                        len(missing),
                    )
                    last_start = None
        # Always cover the whole current cycle so cycle-to-date and tier
        # accumulation are correct.
        from_date = min(fetch_from_date(last_start, today), cycle_start)
        if last_start is None:
            LOGGER.debug("No prior statistics for %s; backfilling from %s", meter_id, from_date)

        readings = await self.client.download_usage(from_date, today, meter_id=meter_id)
        lifetime = await async_import_meter(
            self.hass,
            meter_id,
            readings,
            plan,
            self.rates,
            tiered,
            periods,
            CURRENCY,
            rebuild=self._rebuild,
        )

        cycle = [r for r in readings if r.date >= cycle_start]
        return MeterData(
            latest=max(readings, key=lambda r: r.date) if readings else None,
            plan=plan,
            cycle_energy=sum(r.total for r in cycle),
            cycle_cost=cost_total(cycle, plan, self.rates, tiered, periods) if cycle else None,
            last_bill=max(periods, key=lambda p: p.end_date) if periods else None,
            threshold=season_threshold(today) if plan == PLAN_TIERED else None,
            lifetime_energy=lifetime,
        )

    async def _fetch_rates(self, today: date) -> list[TariffRate]:
        """Current tariff rates for all plans (best effort; empty on failure)."""
        try:
            return await self.client.download_tariff(today - timedelta(days=30), today)
        except EnovaError as err:
            LOGGER.warning("Could not fetch tariff prices: %s", err)
            return []
