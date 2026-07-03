"""Config flow for Enova Power."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from enovapower import AsyncEnovaClient, EnovaAuthError, EnovaNetworkError

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import CONF_PLAN, DEFAULT_PLAN, DOMAIN, LOGGER, PLANS

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

# Re-auth collects credentials only; the plan is unchanged there.
REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class EnovaPowerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Enova Power."""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return EnovaPowerOptionsFlow()

    async def _validate(self, username: str, password: str) -> tuple[str, dict[str, str]]:
        """Try to log in. Return (account_number, errors)."""
        # Use a dedicated session with its own cookie jar — never the shared
        # async_get_clientsession. The portal returns a non-login page (no CSRF
        # token) when the jar already holds an authenticated session cookie, so a
        # jar shared across the config flow and setup makes the second login fail.
        session = async_create_clientsession(self.hass)
        client = AsyncEnovaClient(session=session)
        errors: dict[str, str] = {}
        account = username
        try:
            await client.login(username, password)
            account = client.account_number or username
        except EnovaAuthError:
            errors["base"] = "invalid_auth"
        except EnovaNetworkError:
            errors["base"] = "cannot_connect"
        except Exception:  # noqa: BLE001 - surface unexpected failures to the UI
            LOGGER.exception("Unexpected error validating Enova Power credentials")
            errors["base"] = "unknown"
        finally:
            await client.close()  # clears credentials; won't touch external session
            await session.close()
        return account, errors

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            account, errors = await self._validate(
                user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if not errors:
                await self.async_set_unique_id(account)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_USERNAME], data=user_input
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication when credentials stop working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm new credentials during re-authentication."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        if user_input is not None:
            account, errors = await self._validate(
                user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if not errors:
                await self.async_set_unique_id(account)
                self._abort_if_unique_id_mismatch(reason="account_mismatch")
                return self.async_update_reload_and_abort(
                    reauth_entry, data_updates=user_input
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
        )


class EnovaPowerOptionsFlow(OptionsFlow):
    """Handle Enova Power options (pricing plan)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the pricing plan option."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        # Default to the plan currently in effect — an explicit override, else
        # the plan auto-detected from the portal (via the coordinator), else the
        # fallback. Lets the user correct a wrong/undetected detection.
        coordinator = self.config_entry.runtime_data
        current = self.config_entry.options.get(CONF_PLAN) or (
            coordinator.plan if coordinator else DEFAULT_PLAN
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Required(CONF_PLAN, default=current): vol.In(PLANS)}
            ),
        )
