"""Informational sensors for Enova Power.

The real energy history lives in long-term statistics (see ``statistics.py``)
and powers the Energy dashboard. These sensors just surface the latest known
daily reading for at-a-glance use; they are intentionally not energy-dashboard
meters.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timezone

from enovapower import UsageReading

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import EnovaPowerConfigEntry
from .const import DOMAIN, PERIODS, TIME_ZONE
from .coordinator import EnovaPowerCoordinator
from .schedule import current_period


@dataclass(frozen=True, kw_only=True)
class EnovaSensorDescription(SensorEntityDescription):
    """Describes an Enova Power sensor."""

    value_fn: Callable[[UsageReading], float | datetime | None]


SENSORS: tuple[EnovaSensorDescription, ...] = (
    EnovaSensorDescription(
        key="latest_daily_consumption",
        translation_key="latest_daily_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda r: round(r.total, 3),
    ),
    EnovaSensorDescription(
        key="latest_reading_date",
        translation_key="latest_reading_date",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda r: datetime.combine(r.date, time.min, tzinfo=timezone.utc),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnovaPowerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Enova Power sensors from a config entry (one set per meter)."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        EnovaPowerSensor(coordinator, meter_id, description)
        for meter_id in coordinator.client.meter_ids
        for description in SENSORS
    ]
    # Account-wide sensors for the live pricing period and rate.
    entities.append(EnovaCurrentPeriodSensor(entry.entry_id, coordinator.plan))
    entities.append(EnovaCurrentRateSensor(coordinator, entry.entry_id))
    async_add_entities(entities)


class EnovaPowerSensor(CoordinatorEntity[EnovaPowerCoordinator], SensorEntity):
    """A sensor backed by the latest reading for a specific meter."""

    entity_description: EnovaSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EnovaPowerCoordinator,
        meter_id: str,
        description: EnovaSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._meter_id = meter_id
        self._attr_unique_id = f"{meter_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, meter_id)},
            manufacturer="Enova Power",
            name=f"Enova Power meter {meter_id}",
        )

    @property
    def native_value(self) -> float | datetime | None:
        """Return the current value for this meter, or None if no reading yet."""
        data = self.coordinator.data
        reading = data.get(self._meter_id) if data else None
        if reading is None:
            return None
        return self.entity_description.value_fn(reading)


class EnovaCurrentPeriodSensor(SensorEntity):
    """The live Time-of-Use / ULO pricing period (account-wide).

    Computed from the Ontario OEB schedule, not from the lagged usage data, and
    refreshed on the hour (period boundaries are whole hours). Useful for
    "run when cheap" automations.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "current_period"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PERIODS

    def __init__(self, entry_id: str, plan: str) -> None:
        """Initialize the period sensor."""
        self._plan = plan
        self._attr_unique_id = f"{entry_id}_current_period"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_account")},
            manufacturer="Enova Power",
            name="Enova Power",
        )

    @property
    def native_value(self) -> str:
        """Return the active pricing period right now."""
        return current_period(dt_util.now(TIME_ZONE), self._plan)

    async def async_added_to_hass(self) -> None:
        """Refresh on the hour, when periods can change."""
        self.async_on_remove(
            async_track_time_change(
                self.hass, self._handle_tick, minute=0, second=0
            )
        )

    @callback
    def _handle_tick(self, now: datetime) -> None:
        self.async_write_ha_state()


class EnovaCurrentRateSensor(CoordinatorEntity[EnovaPowerCoordinator], SensorEntity):
    """The electricity rate (¢/kWh) for the active pricing period (account-wide).

    Combines the live period (OEB schedule) with the plan's scraped prices.
    Returns None if the price for the current period isn't available (e.g. the
    portal's rate names for ULO/Tiered differ from expectations).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "current_rate"
    _attr_native_unit_of_measurement = "¢/kWh"
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: EnovaPowerCoordinator, entry_id: str) -> None:
        """Initialize the rate sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_current_rate"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_account")},
            manufacturer="Enova Power",
            name="Enova Power",
        )

    @property
    def native_value(self) -> float | None:
        """Return the rate for the current period, or None if unknown."""
        prices = self.coordinator.prices
        if not prices:
            return None
        period = current_period(dt_util.now(TIME_ZONE), self.coordinator.plan)
        return prices.get(period)

    async def async_added_to_hass(self) -> None:
        """Also refresh on the hour, when the period (and thus rate) changes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_change(
                self.hass, self._handle_tick, minute=0, second=0
            )
        )

    @callback
    def _handle_tick(self, now: datetime) -> None:
        self.async_write_ha_state()
