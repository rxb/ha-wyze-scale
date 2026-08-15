"""Wyze Scale BLE integration for Home Assistant."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .const import CONF_ADDRESS, DOMAIN
from .coordinator import WyzeScaleCoordinator

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.SENSOR]

type WyzeScaleConfigEntry = ConfigEntry[WyzeScaleCoordinator]

SERVICE_ADD_USER = "add_user"
SERVICE_DELETE_USER = "delete_user"

ADD_USER_SCHEMA = vol.Schema(
    {
        vol.Optional("address"): cv.string,
        vol.Required("sex"): vol.In(["male", "female"]),
        vol.Required("age"): vol.All(vol.Coerce(int), vol.Range(min=1, max=120)),
        vol.Required("height_cm"): vol.All(
            vol.Coerce(int), vol.Range(min=50, max=250)
        ),
        vol.Required("weight_kg"): vol.All(
            vol.Coerce(float), vol.Range(min=1, max=300)
        ),
        vol.Optional("athlete_mode", default=False): cv.boolean,
        vol.Optional("weight_only", default=False): cv.boolean,
    }
)

DELETE_USER_SCHEMA = vol.Schema(
    {
        vol.Optional("address"): cv.string,
        vol.Required("user_id"): cv.string,
    }
)


def _resolve_coordinator(hass: HomeAssistant, call: ServiceCall) -> WyzeScaleCoordinator:
    entries: list[WyzeScaleConfigEntry] = hass.config_entries.async_loaded_entries(
        DOMAIN
    )
    if not entries:
        raise ServiceValidationError("No Wyze scale is configured")
    address = call.data.get("address")
    if address:
        for entry in entries:
            if entry.data[CONF_ADDRESS].lower() == address.lower():
                return entry.runtime_data
        raise ServiceValidationError(
            f"No configured Wyze scale with address {address}"
        )
    if len(entries) == 1:
        return entries[0].runtime_data
    raise ServiceValidationError(
        "Multiple scales configured; specify 'address'"
    )


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_ADD_USER):
        return

    async def _handle_add_user(call: ServiceCall) -> ServiceResponse:
        coordinator = _resolve_coordinator(hass, call)
        user_id = await coordinator.async_add_user(
            sex_male=call.data["sex"] == "male",
            age=call.data["age"],
            height_cm=call.data["height_cm"],
            weight_kg=call.data["weight_kg"],
            athlete_mode=call.data["athlete_mode"],
            weight_only=call.data["weight_only"],
        )
        return {"user_id": user_id}

    async def _handle_delete_user(call: ServiceCall) -> None:
        coordinator = _resolve_coordinator(hass, call)
        await coordinator.async_delete_user(call.data["user_id"])

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_USER,
        _handle_add_user,
        schema=ADD_USER_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_USER,
        _handle_delete_user,
        schema=DELETE_USER_SCHEMA,
    )


async def async_setup_entry(hass: HomeAssistant, entry: WyzeScaleConfigEntry) -> bool:
    """Set up a Wyze scale from a config entry.

    Deliberately does NOT connect to the scale here: the scale is asleep
    (and unreachable) unless someone is using it. State is restored from
    storage; syncs are triggered by advertisements or the Poll now button.
    """
    # The coordinator registers its own async_shutdown on entry unload
    # (it receives config_entry in its constructor).
    coordinator = WyzeScaleCoordinator(hass, entry)
    await coordinator.async_load()
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    _async_register_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: WyzeScaleConfigEntry
) -> None:
    """Reload to apply new options (state is persisted, so this is cheap)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: WyzeScaleConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: WyzeScaleConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing stale user sub-devices, but not the scale itself."""
    address = entry.data[CONF_ADDRESS]
    return (DOMAIN, address) not in device.identifiers
