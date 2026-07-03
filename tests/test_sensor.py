"""Tests for sensor helper logic."""

from __future__ import annotations

from custom_components.enova_power.coordinator import MeterData
from custom_components.enova_power.sensor import TIER_1, TIER_2, _current_tier


def _meter(threshold: float | None, cycle_energy: float) -> MeterData:
    return MeterData(
        latest=None,
        plan="tiered",
        cycle_energy=cycle_energy,
        cycle_cost=None,
        last_bill=None,
        threshold=threshold,
    )


async def test_current_tier_below_threshold() -> None:
    assert _current_tier(_meter(600.0, 300.0)) == TIER_1


async def test_current_tier_at_or_above_threshold() -> None:
    assert _current_tier(_meter(600.0, 600.0)) == TIER_2
    assert _current_tier(_meter(600.0, 750.0)) == TIER_2


async def test_current_tier_none_when_not_tiered() -> None:
    assert _current_tier(_meter(None, 300.0)) is None
