"""Diagnostics support for the Wyze Scale integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import WyzeScaleConfigEntry

# The MAC identifies the household's physical location via BLE databases;
# names identify the people. Biometrics stay: they're what user matching
# and reconciliation bugs hinge on, and they're anonymous without the name.
TO_REDACT = {"address", "name"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: WyzeScaleConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "subentries": [
            {
                "subentry_type": subentry.subentry_type,
                "data": async_redact_data(dict(subentry.data), TO_REDACT),
            }
            for subentry in entry.subentries.values()
        ],
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            **coordinator.diagnostics_data(),
        },
    }
