"""Tests for the coordinator's download-window selection (pure logic)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from custom_components.enova_power.const import BACKFILL_MONTHS, RECENT_DAYS
from custom_components.enova_power.coordinator import fetch_from_date


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
