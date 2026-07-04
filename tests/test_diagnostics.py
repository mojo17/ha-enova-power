"""Tests for the diagnostics payload."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.enova_power.coordinator import MeterData
from custom_components.enova_power.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def test_diagnostics_shape_and_redaction() -> None:
    coordinator = MagicMock()
    coordinator.client.meter_ids = ["111"]
    coordinator.data = {
        "111": MeterData(
            latest=None,
            plan="time_of_use",
            cycle_energy=42.5,
            cycle_cost=5.1,
            last_bill=None,
            threshold=None,
            lifetime_energy=1234.5,
        )
    }
    entry = MagicMock()
    entry.runtime_data = coordinator
    entry.data = {"username": "u", "password": "p", "plan": "tiered"}

    result = await async_get_config_entry_diagnostics(None, entry)

    assert result["meter_count"] == 1
    meter = result["meters"][0]
    assert meter["latest_reading_date"] is None
    assert meter["cycle_energy_kwh"] == 42.5
    assert meter["cycle_cost"] == 5.1
    assert meter["lifetime_energy_kwh"] == 1234.5
    # Credentials must never appear, even as key names.
    assert result["config_keys_present"] == ["plan"]
