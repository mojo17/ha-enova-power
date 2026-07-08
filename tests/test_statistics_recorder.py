"""End-to-end statistics import tests against a real recorder.

Reproduces the production sequence reported broken after v0.5.8: a format
rebuild (fresh import of complete history), followed by incremental cycles as
new days publish — asserting the incrementally-arriving days land as hourly
rows with continuous sums, not daily lumps.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.enova_power.const import PLAN_TOU
from custom_components.enova_power.statistics import (
    async_import_meter,
    bucket_statistic_id,
    consumption_statistic_id,
)

from .test_statistics import TOU_RATES, _reading

METER = "111"


@pytest.fixture
def mock_recorder_before_hass(async_test_recorder):
    """Prepare the recorder database before the hass fixture starts.

    The plugin's ``hass`` fixture depends on this hook; recorder tests must
    override it (chaining ``async_test_recorder`` → ``recorder_db_url``) or
    ``recorder_db_url`` asserts that hass was created first.
    """


def _full_day(day: date, kwh: float = 1.0):
    return _reading(day, **{f"h{i:02d}": kwh for i in range(1, 25)})


async def _hourly_rows(hass: HomeAssistant, statistic_id: str) -> list[dict]:
    start = datetime(2026, 6, 30, 0, tzinfo=timezone.utc)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        None,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    return stats.get(statistic_id, [])


async def test_incremental_day_lands_hourly_after_rebuild(
    recorder_mock, hass: HomeAssistant
) -> None:
    day1, day2 = date(2026, 7, 1), date(2026, 7, 2)

    # Cycle 1 — the format rebuild: fresh import of complete history.
    await async_import_meter(
        hass, METER, [_full_day(day1)], PLAN_TOU, TOU_RATES, None, [], "CAD",
        rebuild=True,
    )
    await async_wait_recording_done(hass)
    assert len(await _hourly_rows(hass, consumption_statistic_id(METER))) == 24

    # Cycle 2 — incremental: the window still contains day 1; day 2 is new.
    await async_import_meter(
        hass, METER, [_full_day(day1), _full_day(day2)], PLAN_TOU, TOU_RATES,
        None, [], "CAD",
    )
    await async_wait_recording_done(hass)

    rows = await _hourly_rows(hass, consumption_statistic_id(METER))
    # Both days must be hourly — 48 rows with a continuous +1 kWh/h sum, no
    # daily lumps and no double-imported day-1 rows.
    assert len(rows) == 48
    assert [r["sum"] for r in rows] == [float(i) for i in range(1, 49)]

    # The TOU bucket series must also have gained hourly rows for the new day.
    bucket_rows = await _hourly_rows(hass, bucket_statistic_id(METER, "tou_off_peak"))
    day2_start = datetime(2026, 7, 2, 4, tzinfo=timezone.utc).timestamp()  # 00:00 EDT
    assert sum(1 for r in bucket_rows if r["start"] >= day2_start) == 12  # off-peak hours
