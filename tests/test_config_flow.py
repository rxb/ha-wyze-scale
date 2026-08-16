"""Tests for the Wyze Scale config, options, and user-subentry flows."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wyze_scale.const import (
    CONF_ADDRESS,
    CONF_ADVERTISEMENT_TRIGGER,
    CONF_DISPLAY_UNIT,
    CONF_FALLBACK_INTERVAL,
    CONF_SYNC_COOLDOWN,
    DOMAIN,
    SUBENTRY_TYPE_USER,
    UNIT_OPTION_KG,
)

from conftest import SCALE_ADDRESS, SCALE_UNIQUE_ID, make_service_info


@pytest.fixture(autouse=True)
def bluetooth_ready(enable_bluetooth: None) -> None:
    """Set up the (mocked) bluetooth stack the integration depends on."""


@pytest.fixture(autouse=True)
def mock_setup_entry():
    """Keep flow tests focused on the flow itself."""
    with patch(
        "custom_components.wyze_scale.async_setup_entry", return_value=True
    ) as mock:
        yield mock


async def test_bluetooth_discovery(hass: HomeAssistant) -> None:
    """A discovered scale is confirmed and creates an entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=make_service_info(),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ADDRESS: SCALE_ADDRESS}
    assert result["result"].unique_id == SCALE_UNIQUE_ID


async def test_bluetooth_discovery_already_configured(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Rediscovery of a configured scale aborts."""
    mock_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=make_service_info(),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow(hass: HomeAssistant) -> None:
    """Manual setup lists visible scales and creates an entry."""
    with patch(
        "homeassistant.components.bluetooth.async_discovered_service_info",
        return_value=[make_service_info()],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: SCALE_ADDRESS}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ADDRESS: SCALE_ADDRESS}
    assert result["result"].unique_id == SCALE_UNIQUE_ID


async def test_user_flow_no_devices(hass: HomeAssistant) -> None:
    """Manual setup aborts when no scales are visible."""
    with patch(
        "homeassistant.components.bluetooth.async_discovered_service_info",
        return_value=[],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_user_flow_ignores_non_scales_and_configured(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """The picker filters out foreign devices and already-configured scales."""
    mock_entry.add_to_hass(hass)
    foreign = make_service_info(address="11:22:33:44:55:66", name="NotAScale")
    # Strip the scale's service UUID off the foreign device.
    object.__setattr__(foreign, "service_uuids", [])
    with patch(
        "homeassistant.components.bluetooth.async_discovered_service_info",
        return_value=[make_service_info(), foreign],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_options_flow(hass: HomeAssistant, mock_entry: MockConfigEntry) -> None:
    """Options are validated and stored."""
    mock_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ADVERTISEMENT_TRIGGER: False,
            CONF_SYNC_COOLDOWN: 300,
            CONF_FALLBACK_INTERVAL: 0,
            CONF_DISPLAY_UNIT: UNIT_OPTION_KG,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_entry.options == {
        CONF_ADVERTISEMENT_TRIGGER: False,
        CONF_SYNC_COOLDOWN: 300,
        CONF_FALLBACK_INTERVAL: 0,
        CONF_DISPLAY_UNIT: UNIT_OPTION_KG,
    }


NEW_ADDRESS = "11:22:33:44:55:66"


async def test_reconfigure_flow_moves_entry_to_new_scale(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Reconfigure re-points the entry (and its unique id) at a new scale."""
    mock_entry.add_to_hass(hass)
    with patch(
        "homeassistant.components.bluetooth.async_discovered_service_info",
        return_value=[make_service_info(address=NEW_ADDRESS)],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": mock_entry.entry_id,
            },
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: NEW_ADDRESS}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_entry.data[CONF_ADDRESS] == NEW_ADDRESS
    assert mock_entry.unique_id == NEW_ADDRESS.lower()
    assert mock_entry.title == f"Wyze Scale ({NEW_ADDRESS})"


async def test_reconfigure_flow_keeps_current_scale(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Re-selecting the entry's own scale is allowed and changes nothing."""
    mock_entry.add_to_hass(hass)
    with patch(
        "homeassistant.components.bluetooth.async_discovered_service_info",
        return_value=[make_service_info()],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": mock_entry.entry_id,
            },
        )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: SCALE_ADDRESS}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_entry.data[CONF_ADDRESS] == SCALE_ADDRESS
    assert mock_entry.unique_id == SCALE_UNIQUE_ID


async def test_reconfigure_flow_excludes_other_configured_scales(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """A scale owned by another entry isn't offered as a replacement."""
    mock_entry.add_to_hass(hass)
    MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ADDRESS: NEW_ADDRESS},
        unique_id=NEW_ADDRESS.lower(),
    ).add_to_hass(hass)
    with patch(
        "homeassistant.components.bluetooth.async_discovered_service_info",
        return_value=[make_service_info(address=NEW_ADDRESS)],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": mock_entry.entry_id,
            },
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


NEW_USER_INPUT: dict[str, Any] = {
    "name": "Alice",
    "sex": "female",
    "age": 30,
    "height_cm": 170,
    "weight_kg": 70,
    "athlete_mode": False,
    "weight_only": False,
}


async def test_add_user_subentry(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Adding a scale user creates a subentry with the canonical profile."""
    mock_entry.add_to_hass(hass)
    result = await hass.config_entries.subentries.async_init(
        (mock_entry.entry_id, SUBENTRY_TYPE_USER),
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], NEW_USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    subentry = next(iter(mock_entry.subentries.values()))
    assert subentry.subentry_type == SUBENTRY_TYPE_USER
    assert subentry.title == "Alice"
    data = dict(subentry.data)
    user_id = data.pop("user_id")
    assert len(user_id) == 32  # 16 random bytes as hex
    assert subentry.unique_id == user_id
    assert data == {
        "name": "Alice",
        "sex": "female",
        "age": 30,
        "height_cm": 170,
        "weight_kg": 70.0,
        "athlete_mode": False,
        "weight_only": False,
    }


async def test_reconfigure_user_subentry(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Editing a user keeps their id and updates the profile."""
    mock_entry.add_to_hass(hass)
    result = await hass.config_entries.subentries.async_init(
        (mock_entry.entry_id, SUBENTRY_TYPE_USER),
        context={"source": config_entries.SOURCE_USER},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], NEW_USER_INPUT
    )
    subentry = next(iter(mock_entry.subentries.values()))
    user_id = subentry.data["user_id"]

    result = await hass.config_entries.subentries.async_init(
        (mock_entry.entry_id, SUBENTRY_TYPE_USER),
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "subentry_id": subentry.subentry_id,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**NEW_USER_INPUT, "name": "Alicia", "age": 31}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    subentry = next(iter(mock_entry.subentries.values()))
    assert subentry.title == "Alicia"
    assert subentry.data["user_id"] == user_id
    assert subentry.data["age"] == 31
