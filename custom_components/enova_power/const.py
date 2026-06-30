"""Constants for the Enova Power integration."""

from __future__ import annotations

import logging
from datetime import timedelta

DOMAIN = "enova_power"

LOGGER = logging.getLogger(__package__)

# The portal is a utility web UI, not a high-throughput API. The library
# recommends not polling more often than every 15 minutes; 30 is comfortable.
UPDATE_INTERVAL = timedelta(minutes=30)

# How much history to pull on first setup (the library chunks >90-day ranges).
BACKFILL_MONTHS = 12

# How many recent days to re-fetch each cycle (portal data lags a few days).
RECENT_DAYS = 5

# External statistics namespace: "<domain>:<object_id>" (the colon is required).
STAT_ID_PREFIX = f"{DOMAIN}:"

# Pricing plan selection (config/options). Values map to the library's tariff
# plan names. Cost is currently computed for Time-of-Use; ULO/Tiered are
# selectable but their cost math is not implemented yet.
CONF_PLAN = "plan"
PLAN_TOU = "time_of_use"
PLAN_ULO = "ulo"
PLAN_TIERED = "tiered"
PLANS = {
    PLAN_TOU: "Time-of-Use",
    PLAN_ULO: "Ultra-Low Overnight",
    PLAN_TIERED: "Tiered",
}
DEFAULT_PLAN = PLAN_TOU

# Cost statistics are reported in Canadian dollars.
CURRENCY = "CAD"
