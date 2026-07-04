"""Sensors for Enova Power.

Two device groups:

* One **per-meter** device carries the informational sensors (latest reading,
  billing-cycle-to-date consumption/energy-cost, last actual bill) and the live
  plan/period/rate/tier — all of which depend on *that meter's* plan (a
  subscriber can be on different plans per meter).
* One **account** device carries the rate card: a diagnostic sensor per plan
  rate, shared across meters (utility rates don't vary by meter).

The real energy history lives in long-term statistics (see ``statistics.py``);
these sensors are for at-a-glance use and automations, not energy-dashboard
meters. The monotonic total-consumption sensor additionally serves as the one
supported utility_meter source (monthly-or-longer cycles only — data arrives
days late, so finer cycles misattribute usage). Live period/rate refresh on the
hour (period boundaries are whole hours) and classify by local Ontario time to
match how the portal bills.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timezone

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import EnovaPowerConfigEntry
from .const import CURRENCY, DOMAIN, PERIODS, PLANS
from .coordinator import EnovaPowerCoordinator, MeterData
from .schedule import period_for_interval
from .statistics import plan_prices, tiered_rates

# Rate-card sensors (account device): key → the scraped (plan, rate name).
RATE_CARD: tuple[tuple[str, str, str], ...] = (
    ("tou_off_peak_rate", "Time-of-Use", "TOU Off-peak"),
    ("tou_mid_peak_rate", "Time-of-Use", "TOU Mid-peak"),
    ("tou_on_peak_rate", "Time-of-Use", "TOU On-peak"),
    ("ulo_overnight_rate", "Ultra-Low Overnight", "ULO Lon-peak"),
    ("ulo_off_peak_rate", "Ultra-Low Overnight", "ULO Off-peak"),
    ("ulo_mid_peak_rate", "Ultra-Low Overnight", "ULO Mid-peak"),
    ("ulo_on_peak_rate", "Ultra-Low Overnight", "ULO On-peak"),
    ("tier_1_rate", "Tiered", "Tier 1"),
    ("tier_2_rate", "Tiered", "Tier 2"),
)

TIER_1 = "tier_1"
TIER_2 = "tier_2"


def _current_tier(d: MeterData) -> str | None:
    if d.threshold is None:
        return None
    return TIER_2 if d.cycle_energy >= d.threshold else TIER_1


@dataclass(frozen=True, kw_only=True)
class EnovaSensorDescription(SensorEntityDescription):
    """Describes a per-meter sensor backed by ``MeterData``."""

    value_fn: Callable[[MeterData], float | str | datetime | None]


METER_SENSORS: tuple[EnovaSensorDescription, ...] = (
    EnovaSensorDescription(
        key="latest_daily_consumption",
        translation_key="latest_daily_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda d: round(d.latest.total, 3) if d.latest else None,
    ),
    EnovaSensorDescription(
        key="latest_reading_date",
        translation_key="latest_reading_date",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: (
            datetime.combine(d.latest.date, time.min, tzinfo=timezone.utc)
            if d.latest
            else None
        ),
    ),
    # Monotonic lifetime total (the LTS cumulative sum) — the supported source
    # for utility_meter helpers. Deliberately no state_class: one would make the
    # recorder build a second, import-time-bucketed kWh series that users could
    # add to the Energy dashboard and double-count against the external stats.
    EnovaSensorDescription(
        key="total_consumption",
        translation_key="total_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda d: (
            round(d.lifetime_energy, 3) if d.lifetime_energy is not None else None
        ),
    ),
    EnovaSensorDescription(
        key="cycle_to_date_consumption",
        translation_key="cycle_to_date_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda d: round(d.cycle_energy, 3),
    ),
    EnovaSensorDescription(
        key="cycle_to_date_energy_cost",
        translation_key="cycle_to_date_energy_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        value_fn=lambda d: round(d.cycle_cost, 2) if d.cycle_cost is not None else None,
    ),
    EnovaSensorDescription(
        key="last_bill_amount",
        translation_key="last_bill_amount",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        value_fn=lambda d: round(d.last_bill.amount, 2) if d.last_bill else None,
    ),
    EnovaSensorDescription(
        key="active_plan",
        translation_key="active_plan",
        device_class=SensorDeviceClass.ENUM,
        options=list(PLANS),
        value_fn=lambda d: d.plan,
    ),
    EnovaSensorDescription(
        key="current_tier",
        translation_key="current_tier",
        device_class=SensorDeviceClass.ENUM,
        options=[TIER_1, TIER_2],
        value_fn=_current_tier,
    ),
    EnovaSensorDescription(
        key="kwh_to_tier_2",
        translation_key="kwh_to_tier_2",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda d: (
            round(max(0.0, d.threshold - d.cycle_energy), 3)
            if d.threshold is not None
            else None
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnovaPowerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Enova Power sensors: per-meter groups + the account rate card."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for meter_id in coordinator.client.meter_ids:
        entities += [
            EnovaMeterSensor(coordinator, meter_id, description)
            for description in METER_SENSORS
        ]
        entities.append(EnovaCurrentPeriodSensor(coordinator, meter_id))
        entities.append(EnovaCurrentRateSensor(coordinator, meter_id))
    entities += [
        EnovaRateSensor(coordinator, entry.entry_id, key, plan, name)
        for key, plan, name in RATE_CARD
    ]
    async_add_entities(entities)


def _meter_device(meter_id: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, meter_id)},
        manufacturer="Enova Power",
        name=f"Enova Power meter {meter_id}",
    )


def _account_device(entry_id: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_account")},
        manufacturer="Enova Power",
        name="Enova Power",
    )


class EnovaMeterSensor(CoordinatorEntity[EnovaPowerCoordinator], SensorEntity):
    """A per-meter sensor backed by ``MeterData``."""

    entity_description: EnovaSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EnovaPowerCoordinator,
        meter_id: str,
        description: EnovaSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._meter_id = meter_id
        self._attr_unique_id = f"{meter_id}_{description.key}"
        self._attr_device_info = _meter_device(meter_id)

    @property
    def _data(self) -> MeterData | None:
        data = self.coordinator.data
        return data.get(self._meter_id) if data else None

    @property
    def available(self) -> bool:
        # Tier sensors only apply on the Tiered plan (threshold is set then);
        # mark them unavailable elsewhere instead of showing "Unknown".
        if not super().available:
            return False
        if self.entity_description.key in ("current_tier", "kwh_to_tier_2"):
            data = self._data
            return data is not None and data.threshold is not None
        return True

    @property
    def native_value(self) -> float | str | datetime | None:
        data = self._data
        return self.entity_description.value_fn(data) if data else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        data = self._data
        if self.entity_description.key == "last_bill_amount" and data and data.last_bill:
            bill = data.last_bill
            return {
                "period_start": bill.start_date.isoformat(),
                "period_end": bill.end_date.isoformat(),
                "days": bill.days,
                "kwh": round(bill.usage_kwh, 2),
            }
        return None


class _MeterHourlyEntity(CoordinatorEntity[EnovaPowerCoordinator], SensorEntity):
    """A per-meter sensor that also refreshes on the hour (period boundaries)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EnovaPowerCoordinator, meter_id: str, key: str) -> None:
        super().__init__(coordinator)
        self._meter_id = meter_id
        self._attr_translation_key = key
        self._attr_unique_id = f"{meter_id}_{key}"
        self._attr_device_info = _meter_device(meter_id)

    @property
    def _meter(self) -> MeterData | None:
        data = self.coordinator.data
        return data.get(self._meter_id) if data else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_change(self.hass, self._tick, minute=0, second=0)
        )

    @callback
    def _tick(self, now: datetime) -> None:
        self.async_write_ha_state()


class EnovaCurrentPeriodSensor(_MeterHourlyEntity):
    """The live pricing period for this meter's plan."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PERIODS

    def __init__(self, coordinator: EnovaPowerCoordinator, meter_id: str) -> None:
        super().__init__(coordinator, meter_id, "current_period")

    @property
    def native_value(self) -> str | None:
        meter = self._meter
        if meter is None:
            return None
        return period_for_interval(dt_util.utcnow(), meter.plan)


class EnovaCurrentRateSensor(_MeterHourlyEntity):
    """The ¢/kWh rate for this meter's active period (or tier)."""

    _attr_native_unit_of_measurement = "¢/kWh"
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: EnovaPowerCoordinator, meter_id: str) -> None:
        super().__init__(coordinator, meter_id, "current_rate")

    @property
    def native_value(self) -> float | None:
        meter = self._meter
        if meter is None:
            return None
        tier = _current_tier(meter)
        if tier is not None:  # Tiered: rate depends on cycle-to-date usage.
            tiered = tiered_rates(self.coordinator.rates)
            if tiered is None:
                return None
            return tiered.tier1 if tier == TIER_1 else tiered.tier2
        period = period_for_interval(dt_util.utcnow(), meter.plan)
        return plan_prices(self.coordinator.rates, meter.plan).get(period)


class EnovaRateSensor(CoordinatorEntity[EnovaPowerCoordinator], SensorEntity):
    """A single plan rate (¢/kWh) from the scraped rate card (account device)."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "¢/kWh"
    _attr_suggested_display_precision = 2
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: EnovaPowerCoordinator,
        entry_id: str,
        key: str,
        plan: str,
        rate_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._plan = plan
        self._rate_name = rate_name
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_device_info = _account_device(entry_id)

    @property
    def native_value(self) -> float | None:
        for rate in self.coordinator.rates:
            if rate.plan == self._plan and rate.name == self._rate_name:
                return rate.price
        return None
