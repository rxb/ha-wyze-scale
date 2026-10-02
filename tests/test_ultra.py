"""Synthetic Ultra fixtures; no household captures or biometric profiles."""

import struct
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from test_coordinator import make_measurement

from custom_components.wyze_scale.users import UserProfile
from custom_components.wyze_scale.wyze_ble.client import WyzeScaleClient, WyzeScaleError
from custom_components.wyze_scale.wyze_ble.protocol import build_request, decrypt_frame
from custom_components.wyze_scale.wyze_ble.ultra import (
    UltraReadOnlyClient,
    match_measurement,
    validate_profile_ids,
)
from custom_components.wyze_scale.wyze_ble.ultra_timing import UltraTimingSession
from custom_components.wyze_scale.wyze_ble.ultra_wakeup import (
    UltraWakeupMonitor,
    advertisement_indexes,
)

ALPHA = UserProfile("11" * 16, "Person A", True, 40, 180, 80)
BETA = UserProfile("22" * 16, "Person B", False, 40, 170, 60)
KNOWN = {"33" * 16, "44" * 16}
ADDRESS = "AA:BB:CC:DD:EE:FF"
TARGET = bytes.fromhex("ffeeddccbbaa")


def profile_reply():
    records = []
    for uid in sorted(KNOWN):
        record = bytearray(48)
        record[:16] = bytes.fromhex(uid)
        record[32] = 1
        record[33] = ord("A")
        records.append(record)
    return bytes([0x22, 1, 0, 0, 0x18, 0xA8, 2]) + b"".join(records)


@pytest.mark.parametrize("state", [2, 3, 4])
@pytest.mark.parametrize("identifier", ["00" * 16, "33" * 16])
def test_completed_weights_match_unique_reference(state, identifier):
    frame = make_measurement(user_id=identifier, weight_raw=6010, measure_state=state)
    reading = match_measurement(frame, [ALPHA, BETA], {}, KNOWN)
    assert reading.user_id_hex == BETA.user_id
    assert reading.weight_kg == 60.1
    assert reading.measure_state == 2
    assert reading.timestamp is None
    assert reading.impedance == reading.bfp_raw == reading.bmi_raw == 0


def test_transient_unknown_out_of_range_and_ambiguous_rejected():
    frame = make_measurement(user_id="00" * 16, weight_raw=6000, measure_state=4)
    assert match_measurement(replace(frame, measure_state=0), [BETA], {}, KNOWN) is None
    assert (
        match_measurement(
            replace(frame, user_id=bytes.fromhex("ff" * 16)), [BETA], {}, KNOWN
        )
        is None
    )
    assert match_measurement(replace(frame, weight_raw=9000), [BETA], {}, KNOWN) is None
    assert (
        match_measurement(frame, [BETA, replace(ALPHA, weight_kg=61)], {}, KNOWN)
        is None
    )


def test_adaptive_reference_and_inclusive_boundary():
    frame = make_measurement(user_id="00" * 16, weight_raw=6500, measure_state=4)
    assert match_measurement(frame, [BETA], {}, KNOWN) is None
    assert (
        match_measurement(frame, [BETA], {BETA.user_id: 62}, KNOWN).user_id_hex
        == BETA.user_id
    )
    assert match_measurement(frame, [BETA], {BETA.user_id: 60.4640763}, KNOWN)
    assert match_measurement(frame, [BETA], {BETA.user_id: 60.46}, KNOWN) is None


def test_profile_validation():
    raw = profile_reply()
    assert validate_profile_ids(raw) == KNOWN
    with pytest.raises(ValueError):
        validate_profile_ids(raw[:-1])
    duplicate = raw[:7] + raw[7:55] * 2
    with pytest.raises(ValueError):
        validate_profile_ids(duplicate)
    bad = bytearray(raw)
    bad[39] = 16
    with pytest.raises(ValueError):
        validate_profile_ids(bad)


async def test_ultra_framing_and_command_restriction():
    client = UltraReadOnlyClient()
    client._key = bytes(16)
    client._write = AsyncMock()
    await client._send(build_request(0x18))
    frame = client._write.call_args.args[0]
    payload = decrypt_frame(frame, bytes(16))
    assert payload == bytes([0x20, 1, 2, 0xC0, 0x18, 0xA8])
    with pytest.raises(WyzeScaleError, match="unsupported"):
        await client._send(build_request(0x19))
    assert client._write.call_count == 1


@pytest.mark.parametrize(
    "client_type,response", [(WyzeScaleClient, True), (UltraReadOnlyClient, False)]
)
async def test_write_mode_is_model_specific(client_type, response):
    client = client_type()
    client._client = AsyncMock()
    await client._write(b"test")
    assert client._client.write_gatt_char.call_args.kwargs["response"] is response


def adv_packet(address=TARGET, kind=0):
    report = bytes([kind, 0]) + address + bytes([0, 190])
    body = bytes([62, 2 + len(report), 2, 1]) + report
    return struct.pack("<HHH", 3, 0, len(body)) + body


def test_burst_monitor_isolates_device_and_rearms():
    activity = []
    monitor = UltraWakeupMonitor(ADDRESS, lambda: activity.append(True))
    for t in range(0, 20, 2):
        monitor.process(adv_packet(), t)
    assert not activity
    for t in (20, 20.1, 20.2, 20.3, 20.4):
        monitor.process(adv_packet(), t)
    assert len(activity) == 1
    for t in (22, 22.1, 22.2, 22.3):
        monitor.process(adv_packet(), t)
    assert len(activity) == 2
    for t in (24, 24.1, 24.2, 24.3):
        monitor.process(adv_packet(bytes(6)), t)
        monitor.process(adv_packet(kind=4), t)
    assert len(activity) == 2
    packet = adv_packet()
    for end in range(len(packet)):
        assert advertisement_indexes(packet[:end], TARGET) == []


def hci_packet(opcode, body, index=0):
    return struct.pack("<HHH", opcode, index, len(body)) + body


async def timing_sequence(session):
    # Synthetic connection, original BlueZ parameters, then interval update.
    connection = bytes([62, 12, 1, 0]) + struct.pack("<H", 7) + bytes([0, 0]) + TARGET
    original = (
        bytes(4)
        + struct.pack("<HH", 0x35, 1)
        + TARGET
        + bytes([1])
        + struct.pack("<HHHH", 7, 9, 0, 800)
    )
    update = bytes([62, 10, 3, 0]) + struct.pack("<HHHH", 7, 9, 0, 800)
    for op, body in [(3, connection), (16, original), (3, update)]:
        await session._process(hci_packet(op, body))


async def test_timing_applies_and_restores_only_target():
    session = UltraTimingSession(ADDRESS)
    session._load = AsyncMock()
    await timing_sequence(session)
    await session.close()
    assert [call.args[0] for call in session._load.call_args_list] == [
        (24, 36, 2, 500),
        (7, 9, 0, 800),
    ]
    other = UltraTimingSession("11:22:33:44:55:66")
    other._load = AsyncMock()
    await timing_sequence(other)
    await other.close()
    other._load.assert_not_called()


async def test_lost_timing_ack_still_restores():
    session = UltraTimingSession(ADDRESS)
    session._load = AsyncMock(side_effect=[RuntimeError("lost ack"), None])
    with pytest.raises(RuntimeError):
        await timing_sequence(session)
    await session.close()
    assert session._load.call_count == 2
    assert session._load.call_args.args[0] == (7, 9, 0, 800)


async def test_session_buffers_completion_updates_reference_and_saves(hass, mock_entry):
    from types import SimpleNamespace

    from homeassistant.util import dt as dt_util

    from custom_components.wyze_scale.coordinator import WyzeScaleCoordinator

    mock_entry.add_to_hass(hass)
    coordinator = WyzeScaleCoordinator(hass, mock_entry)
    coordinator._subentry_profiles = lambda: {BETA.user_id: ("person_b", BETA)}
    frame = make_measurement(user_id="00" * 16, weight_raw=6010, measure_state=4)

    class Client:
        commands = []
        is_connected = False

        def __init__(self, on_live_weight, on_message):
            self.on_live_weight = on_live_weight
            self.on_message = on_message

        async def connect(self, device):
            self.is_connected = True

        async def sync_time(self, timestamp):
            self.commands.append(1)

        async def send_raw(self, command):
            self.commands.append(command)
            self.on_live_weight(frame)
            self.on_message(SimpleNamespace(cmd=24, raw=profile_reply()))
            self.is_connected = False

        async def disconnect(self):
            self.is_connected = False

    class Timing:
        def __init__(self, address):
            pass

        async def start(self):
            pass

        def check(self):
            pass

        async def close(self):
            pass

    with (
        patch("custom_components.wyze_scale.coordinator.UltraReadOnlyClient", Client),
        patch("custom_components.wyze_scale.coordinator.UltraTimingSession", Timing),
    ):
        await coordinator._async_sync_ultra(object())
    assert Client.commands == [1, 24]
    assert coordinator._scale_data.users[BETA.user_id].last.weight_kg == 60.1
    assert coordinator._ultra_reference_weights == {BETA.user_id: 60.1}
    assert (
        dt_util.utcnow() - coordinator._scale_data.users[BETA.user_id].last.time
    ).total_seconds() < 2
    stored = await coordinator._store.async_load()
    assert stored["ultra_reference_weights"] == {BETA.user_id: 60.1}


@pytest.mark.parametrize("failures,success", [(1, True), (2, False)])
async def test_time_sync_retry_uses_fresh_client_and_is_bounded(
    hass, mock_entry, failures, success
):
    from types import SimpleNamespace

    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.wyze_scale.coordinator import WyzeScaleCoordinator

    mock_entry.add_to_hass(hass)
    coordinator = WyzeScaleCoordinator(hass, mock_entry)
    instances = []

    class Client:
        def __init__(self, on_live_weight, on_message):
            self.on_message = on_message
            self.is_connected = False
            self.closed = False
            instances.append(self)

        async def connect(self, device):
            self.is_connected = True

        async def sync_time(self, timestamp):
            if len(instances) <= failures:
                raise WyzeScaleError("timeout waiting for reply to 0x01")

        async def send_raw(self, command):
            self.on_message(SimpleNamespace(cmd=24, raw=profile_reply()))
            self.is_connected = False

        async def disconnect(self):
            self.is_connected = False
            self.closed = True

    class Timing:
        def __init__(self, address):
            pass

        async def start(self):
            pass

        def check(self):
            pass

        async def close(self):
            pass

    with (
        patch("custom_components.wyze_scale.coordinator.UltraReadOnlyClient", Client),
        patch("custom_components.wyze_scale.coordinator.UltraTimingSession", Timing),
    ):
        if success:
            await coordinator._async_sync_ultra(object())
        else:
            with pytest.raises(UpdateFailed):
                await coordinator._async_sync_ultra(object())
    assert len(instances) == 2
    assert all(client.closed for client in instances)
