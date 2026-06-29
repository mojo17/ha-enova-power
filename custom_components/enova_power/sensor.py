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
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EnovaPowerConfigEntry
from .const import DOMAIN
from .coordinator import EnovaPowerCoordinator


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
    """Set up Enova Power sensors from a config entry."""
    coordinator = entry.runtime_data
    async_add_entities(
        EnovaPowerSensor(coordinator, description) for description in SENSORS
    )


class EnovaPowerSensor(CoordinatorEntity[EnovaPowerCoordinator], SensorEntity):
    """A sensor backed by the latest Enova Power reading."""

    entity_description: EnovaSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EnovaPowerCoordinator,
        description: EnovaSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        meter_id = coordinator.client.meter_id
        self._attr_unique_id = f"{meter_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, meter_id or "unknown")},
            manufacturer="Enova Power",
            name=f"Enova Power meter {meter_id}" if meter_id else "Enova Power",
        )

    @property
    def native_value(self) -> float | datetime | None:
        """Return the current value, or None if no reading yet."""
        reading = self.coordinator.data
        if reading is None:
            return None
        return self.entity_description.value_fn(reading)
