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
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import LOGGER
from .coordinator import EnovaPowerCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]

# Typed config entry: entry.runtime_data is the coordinator.
EnovaPowerConfigEntry = ConfigEntry[EnovaPowerCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: EnovaPowerConfigEntry) -> bool:
    """Set up Enova Power from a config entry."""
    session = async_get_clientsession(hass)
    client = AsyncEnovaClient(session=session)

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

    coordinator = EnovaPowerCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EnovaPowerConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
