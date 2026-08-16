"""Tests for the sync coordinator, driving a fake BLE client."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from bleak.backends.device import BLEDevice
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.wyze_scale.const import EVENT_MEASUREMENT
from custom_components.wyze_scale.wyze_ble import Measurement, UserRecord

from conftest import SCALE_ADDRESS

USER_ID = "aa" * 16
HISTORY_TIMESTAMP = 1_700_000_000


def make_measurement(weight_raw: int = 7000) -> Measurement:
    return Measurement(
        user_id=bytes.fromhex(USER_ID),
        sex=1,
        age=40,
        height=180,
        athlete_mode=0,
        only_weight=0,
        weight_raw=weight_raw,
        impedance=500,
        bfp_raw=250,
        muscle_raw=300,
        bone_raw=30,
        water_raw=550,
        protein_raw=180,
        lbm_raw=525,
        vfal=8,
        bmr=1600,
        body_age=35,
        bmi_raw=216,
        timestamp=HISTORY_TIMESTAMP,
    )


class FakeClient:
    """Stands in for WyzeScaleClient: one user with one history record."""

    def __init__(self, on_live_weight=None, **kwargs: Any) -> None:
        self.on_live_weight = on_live_weight
        self.is_connected = False  # skips the live-data linger loop
        self.synced_epoch: int | None = None

    async def connect(self, device: BLEDevice) -> None:
        pass

    async def sync_time(self, epoch: int) -> None:
        self.synced_epoch = epoch

    async def get_users(self) -> list[UserRecord]:
        return [
            UserRecord(
                user_id=bytes.fromhex(USER_ID),
                weight_raw=7000,
                sex=1,
                age=40,
                height=180,
            )
        ]

    async def drain_history(self, record, on_record) -> list[Measurement]:
        measurement = make_measurement()
        on_record(measurement)
        return [measurement]

    async def disconnect(self) -> None:
        pass


@pytest.fixture
async def loaded_entry(
    hass: HomeAssistant, enable_bluetooth: None, mock_entry: MockConfigEntry
) -> MockConfigEntry:
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    return mock_entry


async def test_sync_session_drains_history_and_imports_user(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """A sync stores the measurement, fires the event, and imports the user."""
    events = async_capture_events(hass, EVENT_MEASUREMENT)
    ble_device = BLEDevice(SCALE_ADDRESS, "WL_SC3", None)

    with (
        patch(
            "homeassistant.components.bluetooth.async_ble_device_from_address",
            return_value=ble_device,
        ),
        patch("custom_components.wyze_scale.coordinator.WyzeScaleClient", FakeClient),
    ):
        await loaded_entry.runtime_data.async_poll_now()
        await hass.async_block_till_done()

    # The unknown scale-side user was imported as a config subentry.
    subentries = list(loaded_entry.subentries.values())
    assert len(subentries) == 1
    assert subentries[0].data["user_id"] == USER_ID
    assert subentries[0].title == f"Scale user {USER_ID[:6].upper()}"

    # The history record landed in coordinator data (the entry reloaded
    # after the import, so read the fresh coordinator).
    coordinator = loaded_entry.runtime_data
    user = coordinator.data.users[USER_ID]
    assert user.last is not None
    assert user.last.weight_kg == 70.0
    assert user.last.source == "history"
    assert user.last.body_fat_pct == 25.0
    assert user.last.time.timestamp() == HISTORY_TIMESTAMP
    assert coordinator.data.last_sync is not None

    # Every measurement fires the durable event.
    assert len(events) == 1
    assert events[0].data["user_id"] == USER_ID
    assert events[0].data["weight_kg"] == 70.0
    assert events[0].data["device_address"] == SCALE_ADDRESS


async def test_manual_poll_fails_when_scale_asleep(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Poll now raises if the scale isn't reachable; fallback stays quiet."""
    with patch(
        "homeassistant.components.bluetooth.async_ble_device_from_address",
        return_value=None,
    ):
        with pytest.raises(UpdateFailed, match="not reachable"):
            await loaded_entry.runtime_data.async_poll_now()

        # A scheduled (non-manual) refresh is a silent no-op.
        await loaded_entry.runtime_data.async_refresh()
        assert loaded_entry.runtime_data.last_update_success is True
