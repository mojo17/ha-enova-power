"""Tests for sensor helper logic."""

from __future__ import annotations

from custom_components.enova_power.coordinator import MeterData
from custom_components.enova_power.sensor import (
    METER_SENSORS,
    TIER_1,
    TIER_2,
    _current_tier,
)


def _meter(threshold: float | None, cycle_energy: float) -> MeterData:
    return MeterData(
        latest=None,
        plan="tiered",
        cycle_energy=cycle_energy,
        cycle_cost=None,
        last_bill=None,
        threshold=threshold,
        lifetime_energy=None,
    )


async def test_current_tier_below_threshold() -> None:
    assert _current_tier(_meter(600.0, 300.0)) == TIER_1


async def test_current_tier_at_or_above_threshold() -> None:
    assert _current_tier(_meter(600.0, 600.0)) == TIER_2
    assert _current_tier(_meter(600.0, 750.0)) == TIER_2


async def test_current_tier_none_when_not_tiered() -> None:
    assert _current_tier(_meter(None, 300.0)) is None


async def test_total_consumption_has_no_state_class() -> None:
    # Deliberate: a state_class would create a second, import-time-bucketed kWh
    # statistics series that could be double-counted in the Energy dashboard.
    # The sensor exists as a monotonic source for utility_meter helpers only.
    description = next(d for d in METER_SENSORS if d.key == "total_consumption")
    assert description.state_class is None
    assert description.native_unit_of_measurement == "kWh"
