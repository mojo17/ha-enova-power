"""Tests for the Enova Power config flow."""

from __future__ import annotations

from enovapower import EnovaAuthError, EnovaNetworkError

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.enova_power.const import DOMAIN

USER_INPUT = {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"}


async def test_user_flow_success(hass: HomeAssistant, mock_client) -> None:
    """A valid login creates an entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == USER_INPUT[CONF_USERNAME]
    assert result["result"].unique_id == "1234567890"
    mock_client.login.assert_awaited_once()


async def test_user_flow_invalid_auth(hass: HomeAssistant, mock_client) -> None:
    """A bad login shows invalid_auth and lets the user retry."""
    mock_client.login.side_effect = EnovaAuthError("bad")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_cannot_connect(hass: HomeAssistant, mock_client) -> None:
    """A network failure shows cannot_connect."""
    mock_client.login.side_effect = EnovaNetworkError("down")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
