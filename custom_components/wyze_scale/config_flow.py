"""Config flow for the Wyze Scale integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.device_registry import format_mac

from .const import (
    CONF_ADDRESS,
    CONF_ADVERTISEMENT_TRIGGER,
    CONF_DISPLAY_UNIT,
    CONF_FALLBACK_INTERVAL,
    CONF_SYNC_COOLDOWN,
    DEFAULT_ADVERTISEMENT_TRIGGER,
    DEFAULT_FALLBACK_INTERVAL,
    DEFAULT_SYNC_COOLDOWN,
    DOMAIN,
    MAX_FALLBACK_INTERVAL,
    MAX_SYNC_COOLDOWN,
    MIN_SYNC_COOLDOWN,
    UNIT_OPTION_KG,
    UNIT_OPTION_LB,
    UNIT_OPTION_NONE,
)
from .wyze_ble import LOCAL_NAME, SERVICE_UUID


def _is_wyze_scale(service_info: BluetoothServiceInfoBleak) -> bool:
    return (
        SERVICE_UUID in service_info.service_uuids
        or service_info.name == LOCAL_NAME
    )


class WyzeScaleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle discovery and manual setup."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, str] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle automatic Bluetooth discovery."""
        await self.async_set_unique_id(format_mac(discovery_info.address))
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or LOCAL_NAME,
            "address": discovery_info.address,
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered scale."""
        assert self._discovery_info is not None
        if user_input is not None:
            return self.async_create_entry(
                title=f"Wyze Scale ({self._discovery_info.address})",
                data={CONF_ADDRESS: self._discovery_info.address},
            )
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": self._discovery_info.name or LOCAL_NAME,
                "address": self._discovery_info.address,
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick from currently-visible scales."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(format_mac(address), raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"Wyze Scale ({address})",
                data={CONF_ADDRESS: address},
            )

        configured = self._async_current_ids(include_ignore=True)
        self._discovered = {}
        for service_info in bluetooth.async_discovered_service_info(self.hass):
            if not _is_wyze_scale(service_info):
                continue
            if format_mac(service_info.address) in configured:
                continue
            self._discovered[service_info.address] = (
                f"{service_info.name or LOCAL_NAME} ({service_info.address})"
            )
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In(self._discovered)}
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "WyzeScaleOptionsFlow":
        return WyzeScaleOptionsFlow()


class WyzeScaleOptionsFlow(OptionsFlow):
    """Options: advertisement trigger, cooldown, display unit."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ADVERTISEMENT_TRIGGER,
                        default=options.get(
                            CONF_ADVERTISEMENT_TRIGGER, DEFAULT_ADVERTISEMENT_TRIGGER
                        ),
                    ): bool,
                    vol.Required(
                        CONF_SYNC_COOLDOWN,
                        default=options.get(CONF_SYNC_COOLDOWN, DEFAULT_SYNC_COOLDOWN),
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SYNC_COOLDOWN, max=MAX_SYNC_COOLDOWN),
                    ),
                    vol.Required(
                        CONF_FALLBACK_INTERVAL,
                        default=options.get(
                            CONF_FALLBACK_INTERVAL, DEFAULT_FALLBACK_INTERVAL
                        ),
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=0, max=MAX_FALLBACK_INTERVAL),
                    ),
                    vol.Required(
                        CONF_DISPLAY_UNIT,
                        default=options.get(CONF_DISPLAY_UNIT, UNIT_OPTION_NONE),
                    ): vol.In([UNIT_OPTION_NONE, UNIT_OPTION_KG, UNIT_OPTION_LB]),
                }
            ),
        )
