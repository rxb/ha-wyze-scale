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

from custom_components.wyze_scale.const import (
    CONF_ADDRESS,
    CONF_DISPLAY_UNIT,
    DOMAIN,
    EVENT_MEASUREMENT,
    SUBENTRY_TYPE_USER,
    UNIT_OPTION_KG,
)
from custom_components.wyze_scale.wyze_ble import (
    Measurement,
    UserRecord,
    WyzeScaleError,
)
from custom_components.wyze_scale.wyze_ble.protocol import (
    MEASURE_STATE_FINAL,
    UNIT_KG,
)

from conftest import SCALE_ADDRESS, SCALE_UNIQUE_ID

USER_ID = "aa" * 16
OTHER_USER_ID = "bb" * 16
HISTORY_TIMESTAMP = 1_700_000_000


def make_measurement(
    user_id: str = USER_ID,
    weight_raw: int = 7000,
    timestamp: int | None = HISTORY_TIMESTAMP,
    measure_state: int | None = None,
    battery: int | None = None,
    unit: int | None = None,
) -> Measurement:
    return Measurement(
        user_id=bytes.fromhex(user_id),
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
        timestamp=timestamp,
        measure_state=measure_state,
        battery=battery,
        unit=unit,
    )


def make_user_record(
    user_id: str = USER_ID,
    weight_raw: int = 7000,
    sex: int = 1,
    age: int = 40,
    height: int = 180,
) -> UserRecord:
    return UserRecord(
        user_id=bytes.fromhex(user_id),
        weight_raw=weight_raw,
        sex=sex,
        age=age,
        height=height,
    )


class FakeClient:
    """Stands in for WyzeScaleClient; configured via class attributes."""

    users: list[UserRecord] = []
    history: dict[str, list[Measurement]] = {}
    fail_drain_for: set[str] = set()
    fail_connect: bool = False
    fail_set_unit: bool = False
    instances: list["FakeClient"] = []

    def __init__(self, on_live_weight=None, **kwargs: Any) -> None:
        self.on_live_weight = on_live_weight
        self.is_connected = False  # skips the live-data linger loop
        self.calls: list[tuple] = []
        FakeClient.instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.users = []
        cls.history = {}
        cls.fail_drain_for = set()
        cls.fail_connect = False
        cls.fail_set_unit = False
        cls.instances = []

    @classmethod
    def all_calls(cls) -> list[tuple]:
        return [call for client in cls.instances for call in client.calls]

    async def connect(self, device: BLEDevice) -> None:
        self.calls.append(("connect",))
        if FakeClient.fail_connect:
            raise WyzeScaleError("connection refused")

    async def sync_time(self, epoch: int) -> None:
        self.calls.append(("sync_time", epoch))

    async def set_unit(self, unit: int) -> None:
        self.calls.append(("set_unit", unit))
        if FakeClient.fail_set_unit:
            raise WyzeScaleError("set unit rejected")

    async def get_users(self) -> list[UserRecord]:
        return list(FakeClient.users)

    async def select_user(self, record: UserRecord) -> None:
        self.calls.append(("select_user", record.user_id.hex()))

    async def update_user(self, record: UserRecord) -> None:
        self.calls.append(("update_user", record.user_id.hex()))

    async def delete_user(self, user_id: bytes) -> None:
        self.calls.append(("delete_user", user_id.hex()))

    async def drain_history(self, record: UserRecord, on_record) -> list[Measurement]:
        uid = record.user_id_hex
        if uid in FakeClient.fail_drain_for:
            raise WyzeScaleError("drain failed")
        records = FakeClient.history.get(uid, [])
        for measurement in records:
            on_record(measurement)
        return records

    async def disconnect(self) -> None:
        self.calls.append(("disconnect",))


@pytest.fixture
def fake_client():
    """Patch the coordinator's BLE client and make the scale reachable."""
    FakeClient.reset()
    ble_device = BLEDevice(SCALE_ADDRESS, "WL_SC3", None)
    with (
        patch(
            "homeassistant.components.bluetooth.async_ble_device_from_address",
            return_value=ble_device,
        ),
        patch("custom_components.wyze_scale.coordinator.WyzeScaleClient", FakeClient),
    ):
        yield FakeClient


@pytest.fixture
async def loaded_entry(
    hass: HomeAssistant, enable_bluetooth: None, mock_entry: MockConfigEntry
) -> MockConfigEntry:
    mock_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    return mock_entry


USER_FORM_INPUT: dict[str, Any] = {
    "name": "Alice",
    "sex": "female",
    "age": 30,
    "height_cm": 170,
    "weight_kg": 70,
    "athlete_mode": False,
    "weight_only": False,
}


async def _add_user_via_flow(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Add a scale user subentry through the UI flow; return its user_id."""
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_USER), context={"source": "user"}
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"], USER_FORM_INPUT
    )
    await hass.async_block_till_done()
    return next(iter(entry.subentries.values())).data["user_id"]


async def test_sync_session_drains_history_and_imports_user(
    hass: HomeAssistant, fake_client, loaded_entry: MockConfigEntry
) -> None:
    """A sync stores the measurement, fires the event, and imports the user."""
    fake_client.users = [make_user_record()]
    fake_client.history = {USER_ID: [make_measurement()]}
    events = async_capture_events(hass, EVENT_MEASUREMENT)

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
    """Poll now raises a translated error; the fallback stays quiet."""
    with patch(
        "homeassistant.components.bluetooth.async_ble_device_from_address",
        return_value=None,
    ):
        with pytest.raises(UpdateFailed) as excinfo:
            await loaded_entry.runtime_data.async_poll_now()
        assert excinfo.value.translation_key == "scale_not_reachable"
        assert excinfo.value.translation_domain == DOMAIN

        # A scheduled (non-manual) refresh is a silent no-op.
        await loaded_entry.runtime_data.async_refresh()
        assert loaded_entry.runtime_data.last_update_success is True


async def test_sync_failure_raises_translated_error(
    hass: HomeAssistant, fake_client, loaded_entry: MockConfigEntry
) -> None:
    """A session failure surfaces as a translated UpdateFailed."""
    fake_client.fail_connect = True

    with pytest.raises(UpdateFailed) as excinfo:
        await loaded_entry.runtime_data.async_poll_now()

    assert excinfo.value.translation_key == "sync_failed"
    assert "connection refused" in excinfo.value.translation_placeholders["error"]
    assert loaded_entry.runtime_data.last_update_success is False
    # The session always disconnects, even on failure.
    assert ("disconnect",) in fake_client.all_calls()


async def test_display_unit_pushed_and_failure_tolerated(
    hass: HomeAssistant, enable_bluetooth: None, fake_client
) -> None:
    """The display unit option is pushed; a set_unit failure isn't fatal."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ADDRESS: SCALE_ADDRESS},
        unique_id=SCALE_UNIQUE_ID,
        options={CONF_DISPLAY_UNIT: UNIT_OPTION_KG},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await entry.runtime_data.async_poll_now()
    assert ("set_unit", UNIT_KG) in fake_client.all_calls()

    fake_client.fail_set_unit = True
    await entry.runtime_data.async_poll_now()
    assert entry.runtime_data.last_update_success is True


async def test_user_created_and_updated_on_scale(
    hass: HomeAssistant, fake_client, loaded_entry: MockConfigEntry
) -> None:
    """Adding/editing a user in HA pushes a create/update to the scale."""
    # Adding a user reloads the entry; the fresh coordinator sees pending
    # work and syncs on its own.
    user_id = await _add_user_via_flow(hass, loaded_entry)
    calls = fake_client.all_calls()
    assert ("select_user", user_id) in calls  # create = select then store
    assert ("update_user", user_id) in calls
    assert user_id in loaded_entry.runtime_data.data.users

    # The scale now has the user; an edited profile is pushed as an
    # update (no select).
    fake_client.users = [
        make_user_record(user_id=user_id, weight_raw=7000, sex=0, age=30, height=170)
    ]
    fake_client.instances.clear()
    subentry = next(iter(loaded_entry.subentries.values()))
    result = await hass.config_entries.subentries.async_init(
        (loaded_entry.entry_id, SUBENTRY_TYPE_USER),
        context={"source": "reconfigure", "subentry_id": subentry.subentry_id},
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**USER_FORM_INPUT, "age": 31}
    )
    await hass.async_block_till_done()

    calls = fake_client.all_calls()
    assert ("update_user", user_id) in calls
    assert ("select_user", user_id) not in calls


async def test_user_deleted_from_scale_after_subentry_removed(
    hass: HomeAssistant, fake_client, loaded_entry: MockConfigEntry
) -> None:
    """Removing a user's subentry tombstones and deletes it from the scale."""
    fake_client.users = [make_user_record()]
    await loaded_entry.runtime_data.async_poll_now()
    await hass.async_block_till_done()

    subentry = next(iter(loaded_entry.subentries.values()))
    assert subentry.data["user_id"] == USER_ID
    fake_client.instances.clear()

    # Removing the subentry reloads the entry; the fresh coordinator
    # detects the disappearance, tombstones the user, and syncs to
    # delete it from the scale.
    hass.config_entries.async_remove_subentry(loaded_entry, subentry.subentry_id)
    await hass.async_block_till_done()

    assert ("delete_user", USER_ID) in fake_client.all_calls()
    assert not loaded_entry.subentries
    assert USER_ID not in loaded_entry.runtime_data.data.users


async def test_drain_failure_for_one_user_does_not_strand_others(
    hass: HomeAssistant, fake_client, loaded_entry: MockConfigEntry
) -> None:
    """One user's failed history drain doesn't abort the session."""
    fake_client.users = [make_user_record(), make_user_record(user_id=OTHER_USER_ID)]
    fake_client.history = {
        USER_ID: [make_measurement()],
        OTHER_USER_ID: [make_measurement(user_id=OTHER_USER_ID, weight_raw=8000)],
    }
    fake_client.fail_drain_for = {USER_ID}
    events = async_capture_events(hass, EVENT_MEASUREMENT)

    await loaded_entry.runtime_data.async_poll_now()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["user_id"] == OTHER_USER_ID
    assert loaded_entry.runtime_data.last_update_success is True
    # Both users were still imported.
    assert len(loaded_entry.subentries) == 2


async def test_live_weight_recording_and_dedup(
    hass: HomeAssistant, loaded_entry: MockConfigEntry
) -> None:
    """Final live frames are recorded once; noise is ignored."""
    coordinator = loaded_entry.runtime_data
    events = async_capture_events(hass, EVENT_MEASUREMENT)

    final = make_measurement(
        timestamp=None, measure_state=MEASURE_STATE_FINAL, battery=80, unit=UNIT_KG
    )
    coordinator._handle_live_weight(final)
    # The scale repeats the settled frame; only the first one counts.
    coordinator._handle_live_weight(final)
    # Non-final frames and empty user ids are never recorded.
    coordinator._handle_live_weight(make_measurement(timestamp=None, measure_state=0))
    coordinator._handle_live_weight(
        make_measurement(
            user_id="00" * 16, timestamp=None, measure_state=MEASURE_STATE_FINAL
        )
    )
    await hass.async_block_till_done()

    assert len(events) == 1
    assert coordinator.data.battery == 80
    assert coordinator.data.unit == UNIT_KG
    user = coordinator.data.users[USER_ID]
    assert user.last is not None
    assert user.last.source == "live"
    assert user.last.weight_kg == 70.0
