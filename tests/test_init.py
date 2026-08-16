"""Tests for integration setup, unload, and diagnostics."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wyze_scale.coordinator import WyzeScaleCoordinator
from custom_components.wyze_scale.diagnostics import (
    async_get_config_entry_diagnostics,
)

from conftest import SCALE_ADDRESS


async def test_setup_and_unload(
    hass: HomeAssistant, enable_bluetooth: None, mock_entry: MockConfigEntry
) -> None:
    """The entry sets up its entities and unloads cleanly."""
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_entry.state is ConfigEntryState.LOADED
    coordinator = mock_entry.runtime_data
    assert isinstance(coordinator, WyzeScaleCoordinator)
    assert coordinator.address == SCALE_ADDRESS

    registry = er.async_get(hass)
    unique_ids = {
        entry.unique_id
        for entry in er.async_entries_for_config_entry(registry, mock_entry.entry_id)
    }
    assert {
        f"{SCALE_ADDRESS}-poll_now",
        f"{SCALE_ADDRESS}-battery",
        f"{SCALE_ADDRESS}-last_sync",
        f"{SCALE_ADDRESS}-rssi",
        f"{SCALE_ADDRESS}-bt_source",
    } <= unique_ids

    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_entry.state is ConfigEntryState.NOT_LOADED


async def test_diagnostics_redacts_identity(
    hass: HomeAssistant, enable_bluetooth: None, mock_entry: MockConfigEntry
) -> None:
    """Diagnostics include coordinator state but hide the MAC and names."""
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_entry)

    assert diagnostics["entry"]["data"]["address"] == "**REDACTED**"
    coordinator_data = diagnostics["coordinator"]
    assert coordinator_data["last_update_success"] is True
    assert "scale_data" in coordinator_data
    assert "tombstones" in coordinator_data
    assert "pushed_profiles" in coordinator_data
