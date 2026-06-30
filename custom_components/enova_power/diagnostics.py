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
    data = coordinator.data or {}
    # meter ids are account-linked identifiers; report per-meter shape, not the ids.
    return {
        "meter_count": len(coordinator.client.meter_ids),
        "meters": [
            {
                "latest_reading_date": reading.date.isoformat() if reading else None,
                "latest_total_kwh": reading.total if reading else None,
            }
            for reading in data.values()
        ],
        "config_keys_present": sorted(k for k in entry.data if k not in TO_REDACT),
    }
