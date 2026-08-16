"""Unit tests for the wyze_ble protocol layer (no BLE hardware needed)."""

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).parent.parent / "custom_components" / "wyze_scale")
)

from wyze_ble import protocol  # noqa: E402
from wyze_ble.xxtea import (  # noqa: E402
    decrypt_block,
    decrypt_payload,
    encrypt_block,
    encrypt_payload,
)

KEY = protocol.derive_session_key(0x89ABCDEF)


def test_session_key_derivation():
    assert protocol.derive_session_key(0x89ABCDEF) == (
        b"89abcdef" + b"\x00" * 8
    )
    assert protocol.derive_session_key(0x0AB3) == b"00000ab3" + b"\x00" * 8


def test_xxtea_roundtrip():
    block = bytes(range(8))
    enc = encrypt_block(block, KEY)
    assert enc != block
    assert decrypt_block(enc, KEY) == block


def test_xxtea_known_vector():
    # Reference vector from the original XXTEA paper implementation:
    # v = {0,0}, key = {0,0,0,0} -> encrypted {0x053704ab, 0x575d8c80} for n=2
    key = b"\x00" * 16
    enc = encrypt_block(b"\x00" * 8, key)
    assert struct.unpack("<2I", enc) == (0x053704AB, 0x575D8C80)


def test_payload_padding_roundtrip():
    payload = b"\x16\x00\x03\x00\x04\xa8\x01"  # 7 bytes -> padded to 8
    enc = encrypt_payload(payload, KEY)
    assert len(enc) == 8
    assert decrypt_payload(enc, len(payload), KEY) == payload


def test_dh_exchange():
    a = protocol.generate_private_key()
    b = protocol.generate_private_key()
    pub_a = protocol.public_key(a)
    pub_b = protocol.public_key(b)
    assert protocol.shared_secret(pub_b, a) == protocol.shared_secret(pub_a, b)


def test_kex_frame_roundtrip():
    frame = protocol.build_kex_frame(0, 0x12345678)
    assert frame == bytes.fromhex("00f0000878563412") + b"\x00" * 4
    reply = bytes([0x40]) + frame[1:]
    assert protocol.parse_kex_reply(reply) == 0x12345678


def test_encrypted_frame_roundtrip():
    payload = protocol.build_sync_time(0x64000000)
    frame = protocol.build_encrypted_frame(3, payload, KEY)
    assert frame[0] == 0x13
    assert frame[1] == 0x01
    assert struct.unpack_from(">H", frame, 2)[0] == len(payload)
    assert (len(frame) - 4) % 8 == 0
    assert protocol.decrypt_frame(frame, KEY) == payload


def test_build_request_layout():
    req = protocol.build_sync_time(1723000000)
    assert req[0] == 0x16
    assert req[1] == 0x00
    assert struct.unpack_from("<H", req, 2)[0] == 7  # cmd + flag + 5 args
    assert req[4] == protocol.CMD_SYNC_TIME
    assert req[5] == 0xA8
    assert struct.unpack_from("<I", req, 6)[0] == 1723000000
    assert req[10] == 0x01

    unit = protocol.build_set_unit(protocol.UNIT_LB)
    assert struct.unpack_from("<H", unit, 2)[0] == 3
    assert unit[6] == 1

    user_list = protocol.build_user_list()
    assert struct.unpack_from("<H", user_list, 2)[0] == 2
    assert len(user_list) == 6

    ack = protocol.build_history_ack()
    assert struct.unpack_from("<H", ack, 2)[0] == 3
    assert ack[4] == protocol.CMD_HISTORY_WEIGHT_DATA
    assert ack[6] == 0


def test_user_record_roundtrip():
    record = protocol.UserRecord(
        user_id=bytes(range(16)),
        weight_raw=8050,
        sex=1,
        age=40,
        height=180,
        athlete_mode=0,
        only_weight=0,
        last_impedance=512,
    )
    packed = record.pack()
    assert len(packed) == 25
    assert protocol.UserRecord.unpack(packed) == record


def _make_message(cmd: int, body: bytes) -> protocol.Message:
    length = len(body) + 2
    raw = bytes([0x22, 0x01]) + struct.pack("<H", length) + bytes([cmd, 0xA8]) + body
    return protocol.parse_message(raw)


def test_parse_user_list():
    rec = protocol.UserRecord(
        user_id=b"\x11" * 16, weight_raw=7000, sex=0, age=30, height=165
    )
    # The byte at offset 6 is the record count (observed on hardware),
    # not a 1 = success flag.
    msg = _make_message(protocol.CMD_USER_LIST_NEW, b"\x02" + rec.pack() * 2)
    users = protocol.parse_user_list(msg)
    assert len(users) == 2
    assert users[0] == protocol.UserRecord.unpack(rec.pack())

    # Empty list
    msg = _make_message(protocol.CMD_USER_LIST_NEW, b"\x00")
    assert protocol.parse_user_list(msg) == []


def test_parse_live_weight():
    body = bytearray(45)  # status-less: battery..bmi = offsets 6..50 -> 45 bytes
    body[0] = 88  # battery
    body[1] = protocol.UNIT_KG
    body[2:18] = b"\xaa" * 16  # user id
    body[18] = 1  # sex
    body[19] = 40  # age
    body[20] = 180  # height
    body[23] = protocol.MEASURE_STATE_FINAL
    struct.pack_into("<H", body, 24, 8050)  # weight
    struct.pack_into("<H", body, 26, 500)  # impedance
    struct.pack_into("<H", body, 28, 225)  # bfp
    struct.pack_into("<H", body, 33, 550)  # water
    struct.pack_into("<H", body, 40, 1800)  # bmr
    struct.pack_into("<H", body, 43, 248)  # bmi

    msg = _make_message(protocol.CMD_CUR_WEIGHT_DATA, bytes(body))
    assert len(msg.raw) == 51
    m = protocol.parse_live_weight(msg)
    assert m.battery == 88
    assert m.user_id == b"\xaa" * 16
    assert m.sex == 1
    assert m.age == 40
    assert m.height == 180
    assert m.is_final
    assert m.weight_kg == pytest.approx(80.50)
    assert m.impedance == 500
    assert m.body_fat_pct == pytest.approx(22.5)
    assert m.water_pct == pytest.approx(55.0)
    assert m.bmr == 1800
    assert m.bmi == pytest.approx(24.8)


def test_measurement_scaling():
    """Field scaling as verified against a real weigh-in.

    The values here are synthetic but satisfy the same internal
    consistency the verifying frame did: lbm = weight * (1 - bfp) and
    muscle = lbm - bone.
    """
    body = bytearray(45)
    body[0] = 100  # battery
    body[2:18] = b"\xcc" * 16
    body[23] = protocol.MEASURE_STATE_FINAL
    struct.pack_into("<H", body, 24, 8000)  # weight: 80.00 kg
    struct.pack_into("<H", body, 26, 500)  # impedance
    struct.pack_into("<H", body, 28, 250)  # bfp: 25.0 %
    struct.pack_into("<H", body, 30, 570)  # muscle: 57.0 kg
    body[32] = 30  # bone: 3.0 kg
    struct.pack_into("<H", body, 33, 550)  # water: 55.0 %
    struct.pack_into("<H", body, 35, 180)  # protein: 18.0 %
    struct.pack_into("<H", body, 37, 600)  # lbm: 60.0 kg
    body[39] = 9  # vfal
    struct.pack_into("<H", body, 40, 1650)  # bmr
    body[42] = 35  # body_age
    struct.pack_into("<H", body, 43, 247)  # bmi: 24.7

    m = protocol.parse_live_weight(
        _make_message(protocol.CMD_CUR_WEIGHT_DATA, bytes(body))
    )
    assert m.weight_kg == pytest.approx(80.00)
    assert m.body_fat_pct == pytest.approx(25.0)
    assert m.muscle_mass_kg == pytest.approx(57.0)
    assert m.bone_mass_kg == pytest.approx(3.0)
    assert m.water_pct == pytest.approx(55.0)
    assert m.protein_pct == pytest.approx(18.0)
    assert m.lean_body_mass_kg == pytest.approx(60.0)
    assert m.bmi == pytest.approx(24.7)
    assert m.vfal == 9
    assert m.bmr == 1650
    assert m.body_age == 35
    # internal consistency of the confirmed scalings
    assert m.lean_body_mass_kg == pytest.approx(
        m.weight_kg * (1 - m.body_fat_pct / 100), abs=0.06
    )
    assert m.muscle_mass_kg == pytest.approx(
        m.lean_body_mass_kg - m.bone_mass_kg, abs=0.01
    )


def test_parse_history_record():
    body = bytearray(47)  # status + payload = offsets 6..52 -> 47 bytes
    body[0] = 1  # status: valid
    struct.pack_into("<I", body, 1, 1723000000)  # timestamp
    body[5:21] = b"\xbb" * 16  # user id
    body[21] = 0  # sex
    body[22] = 35  # age
    body[23] = 170  # height
    struct.pack_into("<H", body, 26, 6825)  # weight
    struct.pack_into("<H", body, 30, 300)  # bfp

    msg = _make_message(protocol.CMD_HISTORY_WEIGHT_DATA, bytes(body))
    assert len(msg.raw) == 53
    m = protocol.parse_history_record(msg)
    assert m is not None
    assert m.timestamp == 1723000000
    assert m.user_id == b"\xbb" * 16
    assert m.weight_kg == pytest.approx(68.25)
    assert m.body_fat_pct == pytest.approx(30.0)

    # invalid status -> None
    body[0] = 0
    msg = _make_message(protocol.CMD_HISTORY_WEIGHT_DATA, bytes(body))
    assert protocol.parse_history_record(msg) is None


def test_heart_mode_and_result():
    # HEART_MODE / WEIGHT_MODE are no-argument commands (encodeCommonData).
    hm = protocol.build_heart_mode()
    assert hm[4] == protocol.CMD_HEART_MODE
    assert struct.unpack_from("<H", hm, 2)[0] == 2  # cmd + 0xA8, no args
    assert protocol.build_weight_mode()[4] == protocol.CMD_WEIGHT_MODE

    # HEART_RESULT payload: offset 6 on_scale, 7 measure_state, 8 bpm.
    msg = _make_message(protocol.CMD_HEART_RESULT, bytes([1, 1, 72]))
    hr = protocol.parse_heart_result(msg)
    assert hr.on_scale == 1
    assert hr.measure_state == 1
    assert hr.is_complete
    assert hr.bpm == 72
    assert hr.heart_rate == 72

    # In-progress: measure_state != 1 -> no final heart rate
    msg = _make_message(protocol.CMD_HEART_RESULT, bytes([1, 0, 0]))
    hr = protocol.parse_heart_result(msg)
    assert not hr.is_complete
    assert hr.heart_rate is None


def test_ack_status():
    msg = _make_message(protocol.CMD_SYNC_TIME, b"\x00")
    assert msg.status == 0
    msg = _make_message(protocol.CMD_SYNC_TIME, b"\x02")
    assert msg.status == 2


def test_parse_message_rejects_garbage():
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_message(b"\x00\x01\x02")
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_message(bytes([0x23, 0x01, 0x03, 0x00, 0x01, 0xA8, 0x00]))
