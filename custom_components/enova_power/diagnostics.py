"""Diagnostics support for Enova Power."""

from __future__ import annotations

from typing import Any

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import EnovaPowerConfigEntry

TO_REDACT = {CONF_USERNAME, CONF_PASSWORD}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EnovaPowerConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry (credentials redacted)."""
    coordinator = entry.runtime_data
    latest = coordinator.data
    # meter ids are account-linked identifiers; report shape, not values.
    return {
        "meter_count": len(coordinator.client.meter_ids),
        "has_meter_id": coordinator.client.meter_id is not None,
        "latest_reading_date": latest.date.isoformat() if latest else None,
        "latest_total_kwh": latest.total if latest else None,
        "config_keys_present": sorted(k for k in entry.data if k not in TO_REDACT),
    }
