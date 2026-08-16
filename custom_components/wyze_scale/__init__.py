"""Wyze Scale BLE integration for Home Assistant."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_ADDRESS, DOMAIN
from .coordinator import WyzeScaleCoordinator

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.SENSOR]

type WyzeScaleConfigEntry = ConfigEntry[WyzeScaleCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: WyzeScaleConfigEntry) -> bool:
    """Set up a Wyze scale from a config entry.

    Deliberately does NOT connect to the scale here: state is restored from
    storage and syncs are triggered by advertisements or the Poll now button.
    Scale users are managed as config subentries (see config_flow).
    """
    # The coordinator registers its own async_shutdown on entry unload
    # (it receives config_entry in its constructor).
    coordinator = WyzeScaleCoordinator(hass, entry)
    await coordinator.async_load()
    entry.runtime_data = coordinator
    # Fires on options changes AND on any subentry add/edit/remove; reloading
    # rebuilds per-subentry entities. State is persisted, so a reload is cheap.
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_entry_updated(
    hass: HomeAssistant, entry: WyzeScaleConfigEntry
) -> None:
    """Reload to apply option or subentry changes."""
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
