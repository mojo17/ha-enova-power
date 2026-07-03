"""Ontario OEB Time-of-Use / Ultra-Low Overnight period schedule.

Pure, no Home Assistant dependencies. Periods are defined in **local Ontario
clock time** (which observes DST), so callers pass an America/Toronto-localized
datetime. Statutory holidays observed by the OEB for TOU/ULO pricing are treated
as off-peak all day. The windows are province-wide and stable, so they're
hardcoded rather than scraped.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .const import (
    PERIOD_MID_PEAK,
    PERIOD_OFF_PEAK,
    PERIOD_ON_PEAK,
    PERIOD_TIERED,
    PERIOD_ULO_OVERNIGHT,
    PLAN_TIERED,
    PLAN_ULO,
)

# The meter clock and the portal's own TOU classification use fixed Eastern
# Standard Time (UTC-5) year-round — NOT DST-observing local time. This was
# verified by reclassifying hourly intervals and matching the portal's per-day
# total_on/mid/off_peak exactly. So period classification converts timestamps to
# this fixed offset before applying the schedule (see period_for_interval).
EASTERN_FIXED = timezone(timedelta(hours=-5))


def period_for_interval(interval_utc: datetime, plan: str) -> str:
    """Classify a UTC hourly-interval start into its pricing period for ``plan``.

    Converts to fixed EST first so the result matches the portal's own bucketing.
    """
    return current_period(interval_utc.astimezone(EASTERN_FIXED), plan)


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous computus)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month = (h + el - 7 * m + 114) // 31
    day = ((h + el - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th ``weekday`` (Mon=0) of ``month`` (1-based n)."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def ontario_tou_holidays(year: int) -> set[date]:
    """OEB statutory holidays that are off-peak all day for TOU/ULO pricing."""
    return {
        date(year, 1, 1),  # New Year's Day
        _nth_weekday(year, 2, 0, 3),  # Family Day (3rd Mon Feb)
        _easter(year) - timedelta(days=2),  # Good Friday
        date(year, 5, 24) - timedelta(days=date(year, 5, 24).weekday()),  # Victoria Day
        date(year, 7, 1),  # Canada Day
        _nth_weekday(year, 9, 0, 1),  # Labour Day (1st Mon Sep)
        _nth_weekday(year, 10, 0, 2),  # Thanksgiving (2nd Mon Oct)
        date(year, 12, 25),  # Christmas Day
        date(year, 12, 26),  # Boxing Day
    }


def current_period(now_local: datetime, plan: str) -> str:
    """Return the active pricing period for ``now_local`` (Ontario local time).

    ``plan`` is one of the ``PLAN_*`` keys. Tiered has no time periods.
    """
    if plan == PLAN_TIERED:
        return PERIOD_TIERED

    hour = now_local.hour

    # ULO overnight applies every day, including weekends/holidays.
    if plan == PLAN_ULO and (hour >= 23 or hour < 7):
        return PERIOD_ULO_OVERNIGHT

    is_off = (
        now_local.weekday() >= 5
        or now_local.date() in ontario_tou_holidays(now_local.year)
    )

    if plan == PLAN_ULO:
        if is_off:
            return PERIOD_OFF_PEAK
        # Weekday daytime: on-peak 16:00-21:00, mid-peak 07:00-16:00 & 21:00-23:00.
        return PERIOD_ON_PEAK if 16 <= hour < 21 else PERIOD_MID_PEAK

    # Time-of-Use. Off-peak: weekends/holidays, and weekdays 19:00-07:00.
    if is_off or hour < 7 or hour >= 19:
        return PERIOD_OFF_PEAK

    if 5 <= now_local.month <= 10:  # summer: on-peak 11:00-17:00
        return PERIOD_ON_PEAK if 11 <= hour < 17 else PERIOD_MID_PEAK
    # winter: on-peak 07:00-11:00 & 17:00-19:00
    return PERIOD_ON_PEAK if (7 <= hour < 11 or 17 <= hour < 19) else PERIOD_MID_PEAK
