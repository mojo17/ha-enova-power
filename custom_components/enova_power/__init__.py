"""The Enova Power integration.

Unofficial — not affiliated with, endorsed by, or supported by Enova Power Corp.
This is a thin Home Assistant wrapper over the generic ``enovapower`` library;
all portal/API logic lives there.
"""

from __future__ import annotations

from enovapower import AsyncEnovaClient, EnovaAuthError, EnovaNetworkError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import CONF_STATS_VERSION, LOGGER
from .coordinator import EnovaPowerCoordinator
from .statistics import STATS_VERSION, async_rebuild_statistics

PLATFORMS: list[Platform] = [Platform.SENSOR]

# Typed config entry: entry.runtime_data is the coordinator.
EnovaPowerConfigEntry = ConfigEntry[EnovaPowerCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: EnovaPowerConfigEntry) -> bool:
    """Set up Enova Power from a config entry."""
    # Dedicated session with an isolated cookie jar — NOT async_get_clientsession.
    # The portal serves a non-login page (no CSRF token) when the shared jar
    # already holds an authenticated session cookie from the config flow's login,
    # which made the setup login fail with "Invalid credentials".
    session = async_create_clientsession(hass)
    client = AsyncEnovaClient(session=session)

    ok = False
    try:
        try:
            await client.login(entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
        except EnovaAuthError as err:
            raise ConfigEntryAuthFailed("Invalid Enova Power credentials") from err
        except EnovaNetworkError as err:
            raise ConfigEntryNotReady(f"Cannot reach Enova Power: {err}") from err

        LOGGER.debug("Logged in; %d meter(s) found", len(client.meter_ids))

        # Every statistic/entity is keyed on the meter id; never set up on None.
        if not client.meter_id:
            raise ConfigEntryNotReady("No Enova Power meter found for this account yet")

        # One-time statistics-format rebuild: clear outdated series before the
        # first refresh so the missing-series check backfills them in the new
        # format (imports are forward-only; granularity can't change in place).
        # Runs before the update listener is registered, so the entry update
        # below does not trigger a reload.
        if entry.data.get(CONF_STATS_VERSION, 1) < STATS_VERSION:
            LOGGER.info(
                "Statistics format changed (v%s -> v%s); rebuilding bucket and "
                "cost series at hourly granularity",
                entry.data.get(CONF_STATS_VERSION, 1),
                STATS_VERSION,
            )
            await async_rebuild_statistics(hass, client.meter_ids)
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_STATS_VERSION: STATS_VERSION}
            )

        coordinator = EnovaPowerCoordinator(hass, entry, client)
        await coordinator.async_config_entry_first_refresh()
        ok = True
    finally:
        # Free the dedicated session on any setup failure (HA retries create a
        # fresh one); on success it lives until the entry is unloaded.
        if not ok:
            await session.close()

    entry.runtime_data = coordinator
    entry.async_on_unload(session.close)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: EnovaPowerConfigEntry
) -> None:
    """Reload the entry when options (e.g. the pricing plan) change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: EnovaPowerConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
