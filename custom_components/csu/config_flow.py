"""Config flow for Colorado Springs Utilities integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry  # type: ignore
from homeassistant.config_entries import ConfigFlow  # type: ignore
from homeassistant.config_entries import ConfigFlowResult  # type: ignore
from homeassistant.config_entries import CONN_CLASS_CLOUD_POLL  # type: ignore
from homeassistant.config_entries import OptionsFlow  # type: ignore
from homeassistant.config_entries import SOURCE_REAUTH  # type: ignore
from homeassistant.const import CONF_PASSWORD  # type: ignore
from homeassistant.const import CONF_USERNAME  # type: ignore
from homeassistant.core import HomeAssistant  # type: ignore
from homeassistant.helpers.aiohttp_client import async_create_clientsession  # type: ignore

from .const import CONF_MFA_CODE
from .const import DOMAIN
from .csu import CannotConnect
from .csu import CSU
from .csu import InvalidAuth
from .exceptions import MfaRequired

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def _validate_login(
    hass: HomeAssistant, login_data: Mapping[str, Any], mfa: str = ""
) -> dict[str, str]:
    """Try a login and return any errors."""

    api = CSU(
        async_create_clientsession(hass),
        login_data[CONF_USERNAME],
        login_data[CONF_PASSWORD],
        mfa,
    )
    errors: dict[str, str] = {}
    try:
        await api.async_login()
    except MfaRequired as err:
        if mfa:
            errors["base"] = "invalid_mfa"
        else:
            # The login itself sends nothing; ask CSU to email the code.
            try:
                await api.async_send_mfa_code(err.mfa_token)
                errors["base"] = "mfa_required"
            except CannotConnect:
                errors["base"] = "cannot_connect"
    except InvalidAuth:
        errors["base"] = "invalid_auth"
    except CannotConnect:
        errors["base"] = "cannot_connect"
    return errors


class CSUConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config Flow for setting up CSU."""

    VERSION = 1
    CONNECTION_CLASS = CONN_CLASS_CLOUD_POLL

    def __init__(self) -> None:
        """Initialize a new CSUConfigFlow."""
        self._login_data: dict[str, Any] = {}

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry):
        """Get the options flow for this handler."""
        return CSUOptionsFlow()

    async def _async_login_step(
        self, step_id: str, schema: vol.Schema, user_input: dict[str, Any]
    ) -> ConfigFlowResult:
        """Log in; ask for the emailed/texted code if the account needs one."""

        self._login_data = dict(user_input)
        errors = await _validate_login(self.hass, self._login_data)
        if errors.get("base") == "mfa_required":
            return await self.async_step_mfa()
        if errors:
            return self.async_show_form(
                step_id=step_id, data_schema=schema, errors=errors
            )
        return self._async_finish(self._login_data)

    def _async_finish(self, data: dict[str, Any]) -> ConfigFlowResult:
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data=data
            )
        return self.async_create_entry(title=f"CSU - {data[CONF_USERNAME]}", data=data)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""

        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=STEP_USER_DATA_SCHEMA
            )
        self._async_abort_entries_match({CONF_USERNAME: user_input[CONF_USERNAME]})
        return await self._async_login_step("user", STEP_USER_DATA_SCHEMA, user_input)

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the one-time code CSU just sent."""

        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await _validate_login(
                self.hass, self._login_data, user_input[CONF_MFA_CODE].strip()
            )
            if not errors:
                return self._async_finish(self._login_data)
        return self.async_show_form(
            step_id="mfa",
            data_schema=vol.Schema({vol.Required(CONF_MFA_CODE): str}),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle configuration by re-auth."""

        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Dialog that informs the user that reauth is required."""

        entry = self._get_reauth_entry()
        schema = vol.Schema({vol.Required(CONF_PASSWORD): str})
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm", data_schema=schema)
        return await self._async_login_step(
            "reauth_confirm",
            schema,
            {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]},
        )


class CSUOptionsFlow(OptionsFlow):
    """Options flow for the integration."""

    @property
    def options(self) -> dict[str, Any]:
        """Working copy of the entry options."""
        if not hasattr(self, "_options"):
            self._options = dict(self.config_entry.options)
        return self._options

    async def async_step_init(self, user_input=None):
        """Manage the options."""

        return await self.async_step_options()

    async def async_step_options(self, user_input=None):
        """Handle options step flow initiated by user."""

        if user_input is not None:
            self.options.update(user_input)
            return await self._update_options()

        return self.async_show_form(
            step_id="options",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "electric_rate_per_kwh",
                        default=self.config_entry.options.get(
                            "electric_rate_per_kwh", 0.0
                        ),
                    ): float,
                    vol.Optional(
                        "electric_rate_per_day",
                        default=self.config_entry.options.get(
                            "electric_rate_per_day", 0.0
                        ),
                    ): float,
                    vol.Optional(
                        "gas_rate_per_ccf",
                        default=self.config_entry.options.get("gas_rate_per_ccf", 0.0),
                    ): float,
                    vol.Optional(
                        "gas_rate_per_day",
                        default=self.config_entry.options.get("gas_rate_per_day", 0.0),
                    ): float,
                    vol.Optional(
                        "water_rate_per_cf",
                        default=self.config_entry.options.get("water_rate_per_cf", 0.0),
                    ): float,
                    vol.Optional(
                        "water_rate_per_day",
                        default=self.config_entry.options.get(
                            "water_rate_per_day", 0.0
                        ),
                    ): float,
                }
            ),
            last_step=True,
        )

    async def _update_options(self):
        """Update config entry options."""
        return self.async_create_entry(title="", data=self.options)
