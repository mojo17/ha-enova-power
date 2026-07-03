"""Tests for the coordinator's download-window selection (pure logic)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enova_power.const import (
    BACKFILL_MONTHS,
    CONF_PLAN,
    DEFAULT_PLAN,
    DOMAIN,
    RECENT_DAYS,
)
from custom_components.enova_power.coordinator import (
    EnovaPowerCoordinator,
    fetch_from_date,
)


async def test_backfills_when_no_statistics() -> None:
    today = date(2026, 6, 1)
    assert fetch_from_date(None, today) == today - timedelta(days=BACKFILL_MONTHS * 31)


async def test_incremental_uses_recent_window_when_current() -> None:
    today = date(2026, 6, 10)
    last_start = datetime(2026, 6, 9, 5, tzinfo=timezone.utc)  # yesterday
    # recent window is earlier than (last_start - 1 day), so it wins
    assert fetch_from_date(last_start, today) == today - timedelta(days=RECENT_DAYS)


async def test_incremental_covers_long_gap_after_downtime() -> None:
    today = date(2026, 6, 30)
    last_start = datetime(2026, 6, 1, 5, tzinfo=timezone.utc)  # ~29 days ago
    # gap is older than the recent window, so fetch from just before the gap
    assert fetch_from_date(last_start, today) == date(2026, 5, 31)


def _coordinator(hass, *, detected=None, options=None, data=None):
    entry = MockConfigEntry(domain=DOMAIN, data=data or {}, options=options or {})
    entry.add_to_hass(hass)
    client = MagicMock()
    client.get_current_plan = AsyncMock(return_value=detected)
    return EnovaPowerCoordinator(hass, entry, client)


async def test_plan_override_from_options(hass: HomeAssistant) -> None:
    assert _coordinator(hass, options={CONF_PLAN: "tiered"}).plan_override() == "tiered"


async def test_plan_override_from_legacy_data(hass: HomeAssistant) -> None:
    assert _coordinator(hass, data={CONF_PLAN: "ulo"}).plan_override() == "ulo"


async def test_plan_override_none(hass: HomeAssistant) -> None:
    assert _coordinator(hass).plan_override() is None


async def test_meter_plan_uses_detection(hass: HomeAssistant) -> None:
    assert await _coordinator(hass, detected="ulo")._meter_plan("111") == "ulo"


async def test_meter_plan_override_wins(hass: HomeAssistant) -> None:
    coord = _coordinator(hass, detected="ulo", options={CONF_PLAN: "tiered"})
    assert await coord._meter_plan("111") == "tiered"


async def test_meter_plan_defaults_when_undetected(hass: HomeAssistant) -> None:
    assert await _coordinator(hass, detected=None)._meter_plan("111") == DEFAULT_PLAN
