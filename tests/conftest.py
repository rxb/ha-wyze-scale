"""Shared fixtures and helpers for the Home Assistant-level tests."""

from __future__ import annotations

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from habluetooth import BluetoothServiceInfoBleak
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wyze_scale.const import CONF_ADDRESS, DOMAIN

SCALE_ADDRESS = "AA:BB:CC:DD:EE:FF"
SCALE_UNIQUE_ID = "aa:bb:cc:dd:ee:ff"
SERVICE_UUID = "0000fd7b-0000-1000-8000-00805f9b34fb"
LOCAL_NAME = "WL_SC3"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Make the custom_components/ directory loadable in every test."""


def make_service_info(
    address: str = SCALE_ADDRESS,
    name: str | None = LOCAL_NAME,
    connectable: bool = True,
) -> BluetoothServiceInfoBleak:
    """Build a BluetoothServiceInfoBleak advertisement for the scale."""
    device = BLEDevice(address, name, None)
    advertisement = AdvertisementData(
        local_name=name,
        manufacturer_data={},
        service_data={},
        service_uuids=[SERVICE_UUID],
        tx_power=-127,
        rssi=-60,
        platform_data=(),
    )
    return BluetoothServiceInfoBleak.from_device_and_advertisement_data(
        device, advertisement, "local", 0.0, connectable
    )


@pytest.fixture
def mock_entry() -> MockConfigEntry:
    """A config entry for the scale, not yet added to hass."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=f"Wyze Scale ({SCALE_ADDRESS})",
        data={CONF_ADDRESS: SCALE_ADDRESS},
        unique_id=SCALE_UNIQUE_ID,
    )
