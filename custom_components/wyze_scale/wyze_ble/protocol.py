"""Wyze Scale X (WL_SC3) BLE protocol: frames, key exchange, messages.

Pure functions and dataclasses only — no I/O. See PROTOCOL.md for the wire
format this implements.
"""

from __future__ import annotations

import secrets
import struct
from dataclasses import dataclass

from .xxtea import decrypt_payload, encrypt_payload

SERVICE_UUID = "0000fd7b-0000-1000-8000-00805f9b34fb"
CHAR_UUID = "00000001-0000-1000-8000-00805f9b34fb"
LOCAL_NAME = "WL_SC3"
MANUFACTURER_ID = 0x0870

_DH_BASE = 5
_DH_MODULUS = 0xFFFFFFC5

# Frame type high nibbles
FRAME_KEX_REQUEST = 0x0
FRAME_ENC_REQUEST = 0x1
FRAME_KEX_REPLY = 0x4
FRAME_ENC_REPLY = 0x5

# Application-layer command IDs
CMD_SYNC_TIME = 0x01
CMD_CURRENT_USER_LEGACY = 0x03
CMD_SET_UNIT = 0x04
CMD_SET_HELLO = 0x05
CMD_RESET = 0x06
CMD_CUR_WEIGHT_DATA = 0x08
CMD_HISTORY_WEIGHT_DATA = 0x09
CMD_UPDATE_USER = 0x0A
CMD_DEL_USER = 0x0B
CMD_USER_LIST_NEW = 0x0D
CMD_CURRENT_USER_NEW = 0x0E

# Commands whose reply is a 7-byte ack with status 0 = success
ACK_COMMANDS = frozenset(
    {
        CMD_SYNC_TIME,
        CMD_CURRENT_USER_LEGACY,
        CMD_SET_UNIT,
        CMD_SET_HELLO,
        CMD_RESET,
        CMD_UPDATE_USER,
        CMD_DEL_USER,
        CMD_CURRENT_USER_NEW,
    }
)

UNIT_KG = 0
UNIT_LB = 1

MEASURE_STATE_FINAL = 2

USER_RECORD_SIZE = 25


class ProtocolError(Exception):
    """Malformed or unexpected protocol data."""


# ---------------------------------------------------------------------------
# Key exchange / session key
# ---------------------------------------------------------------------------


def generate_private_key() -> int:
    """Random 32-bit DH private key (never zero)."""
    return secrets.randbits(32) or 1


def public_key(private: int) -> int:
    return pow(_DH_BASE, private, _DH_MODULUS)


def shared_secret(peer_public: int, private: int) -> int:
    return pow(peer_public, private, _DH_MODULUS)


def derive_session_key(secret: int) -> bytes:
    """XXTEA key: shared secret as 8 lowercase hex ASCII chars + 8 zero bytes."""
    return f"{secret:08x}".encode("ascii") + b"\x00" * 8


# ---------------------------------------------------------------------------
# Transport frames
# ---------------------------------------------------------------------------


def build_kex_frame(counter: int, client_public: int) -> bytes:
    return (
        bytes([counter & 0x0F, 0xF0, 0x00, 0x08])
        + struct.pack("<I", client_public)
        + b"\x00" * 4
    )


def parse_kex_reply(frame: bytes) -> int:
    """Return the scale's DH public key from a key-exchange reply frame."""
    if len(frame) < 12:
        raise ProtocolError(f"key-exchange reply too short: {frame.hex()}")
    if (frame[0] >> 4) != FRAME_KEX_REPLY or frame[1] != 0xF0:
        raise ProtocolError(f"not a key-exchange reply: {frame.hex()}")
    return struct.unpack_from("<I", frame, 4)[0]


def build_encrypted_frame(counter: int, payload: bytes, key: bytes) -> bytes:
    return (
        bytes([0x10 | (counter & 0x0F), 0x01])
        + struct.pack(">H", len(payload))
        + encrypt_payload(payload, key)
    )


def decrypt_frame(frame: bytes, key: bytes) -> bytes:
    """Decrypt an encrypted scale->client frame, returning the plaintext."""
    if len(frame) < 4 + 8:
        raise ProtocolError(f"encrypted frame too short: {frame.hex()}")
    length = struct.unpack_from(">H", frame, 2)[0]
    return decrypt_payload(frame[4:], length, key)


# ---------------------------------------------------------------------------
# Application messages
# ---------------------------------------------------------------------------


def build_request(cmd: int, args: bytes = b"") -> bytes:
    return bytes([0x16, 0x00]) + struct.pack("<H", len(args) + 2) + bytes([cmd, 0xA8]) + args


@dataclass
class Message:
    """A decoded scale->client application message."""

    cmd: int
    raw: bytes

    @property
    def status(self) -> int | None:
        """Status byte at offset 6, absent on live-weight messages."""
        if self.cmd == CMD_CUR_WEIGHT_DATA or len(self.raw) < 7:
            return None
        return self.raw[6]


def parse_message(plain: bytes) -> Message:
    if len(plain) < 6 or plain[0] != 0x22:
        raise ProtocolError(f"not an application message: {plain.hex()}")
    length = struct.unpack_from("<H", plain, 2)[0]
    if len(plain) < 4 + length:
        raise ProtocolError(
            f"application message truncated ({len(plain)} < {4 + length}): {plain.hex()}"
        )
    return Message(cmd=plain[4], raw=plain[: 4 + length])


# ---------------------------------------------------------------------------
# User records
# ---------------------------------------------------------------------------


@dataclass
class UserRecord:
    """25-byte stored user profile (PROTOCOL.md §5.1)."""

    user_id: bytes
    weight_raw: int = 0  # kg x 100
    sex: int = 0  # 1 = male, 0 = female
    age: int = 0
    height: int = 0  # cm
    athlete_mode: int = 0
    only_weight: int = 0
    last_impedance: int = 0

    @property
    def user_id_hex(self) -> str:
        return self.user_id.hex()

    def pack(self) -> bytes:
        if len(self.user_id) != 16:
            raise ProtocolError("user_id must be 16 bytes")
        return self.user_id + struct.pack(
            "<HBBBBBH",
            self.weight_raw,
            self.sex,
            self.age,
            self.height,
            self.athlete_mode,
            self.only_weight,
            self.last_impedance,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "UserRecord":
        if len(data) < USER_RECORD_SIZE:
            raise ProtocolError(f"user record too short: {data.hex()}")
        weight, sex, age, height, athlete, only_w, imp = struct.unpack_from(
            "<HBBBBBH", data, 16
        )
        return cls(
            user_id=bytes(data[:16]),
            weight_raw=weight,
            sex=sex,
            age=age,
            height=height,
            athlete_mode=athlete,
            only_weight=only_w,
            last_impedance=imp,
        )


def parse_user_list(msg: Message) -> list[UserRecord]:
    """Parse one USER_LIST_NEW reply message into user records."""
    if msg.status != 1:
        return []
    body = msg.raw[7:]
    records = []
    for i in range(0, len(body) - len(body) % USER_RECORD_SIZE, USER_RECORD_SIZE):
        records.append(UserRecord.unpack(body[i : i + USER_RECORD_SIZE]))
    return records


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------


@dataclass
class Measurement:
    """A weight/body-composition reading (live or historical).

    All *_raw fields are the device's fixed-point integers. Scaling was
    verified against a real measurement (weight kg x 100; percentages,
    BMI and the mass fields x 10): the final frame satisfied
    lbm = weight x (1 - bfp) and muscle = lbm - bone exactly.
    """

    user_id: bytes
    sex: int
    age: int
    height: int
    athlete_mode: int
    only_weight: int
    weight_raw: int
    impedance: int
    bfp_raw: int
    muscle_raw: int
    bone_raw: int
    water_raw: int
    protein_raw: int
    lbm_raw: int
    vfal: int
    bmr: int
    body_age: int
    bmi_raw: int
    timestamp: int | None = None  # history records only (device epoch)
    measure_state: int | None = None  # live messages only
    battery: int | None = None  # live messages only
    unit: int | None = None  # live messages only

    @property
    def user_id_hex(self) -> str:
        return self.user_id.hex()

    @property
    def is_final(self) -> bool:
        return self.measure_state is None or self.measure_state == MEASURE_STATE_FINAL

    @property
    def weight_kg(self) -> float:
        return self.weight_raw / 100

    def _tenth(self, raw: int) -> float | None:
        return raw / 10 if raw else None

    @property
    def body_fat_pct(self) -> float | None:
        return self._tenth(self.bfp_raw)

    @property
    def muscle_mass_kg(self) -> float | None:
        return self._tenth(self.muscle_raw)

    @property
    def bone_mass_kg(self) -> float | None:
        return self._tenth(self.bone_raw)

    @property
    def water_pct(self) -> float | None:
        return self._tenth(self.water_raw)

    @property
    def protein_pct(self) -> float | None:
        return self._tenth(self.protein_raw)

    @property
    def lean_body_mass_kg(self) -> float | None:
        return self._tenth(self.lbm_raw)

    @property
    def bmi(self) -> float | None:
        return self._tenth(self.bmi_raw)


def _parse_body(raw: bytes, off: int) -> dict:
    """Parse the common 27-byte user+metrics block starting at `off`."""
    (
        sex,
        age,
        height,
        athlete,
        only_w,
    ) = struct.unpack_from("<5B", raw, off + 16)
    return {
        "user_id": bytes(raw[off : off + 16]),
        "sex": sex,
        "age": age,
        "height": height,
        "athlete_mode": athlete,
        "only_weight": only_w,
    }


def parse_live_weight(msg: Message) -> Measurement:
    """Parse a CUR_WEIGHT_DATA (0x08) message (PROTOCOL.md §5.7)."""
    raw = msg.raw
    if len(raw) < 51:
        raise ProtocolError(f"live weight message too short: {raw.hex()}")
    fields = _parse_body(raw, 8)
    weight, impedance, bfp, muscle = struct.unpack_from("<4H", raw, 30)
    bone = raw[38]
    water, protein, lbm = struct.unpack_from("<3H", raw, 39)
    vfal = raw[45]
    bmr = struct.unpack_from("<H", raw, 46)[0]
    body_age = raw[48]
    bmi = struct.unpack_from("<H", raw, 49)[0]
    return Measurement(
        battery=raw[6],
        unit=raw[7],
        measure_state=raw[29],
        weight_raw=weight,
        impedance=impedance,
        bfp_raw=bfp,
        muscle_raw=muscle,
        bone_raw=bone,
        water_raw=water,
        protein_raw=protein,
        lbm_raw=lbm,
        vfal=vfal,
        bmr=bmr,
        body_age=body_age,
        bmi_raw=bmi,
        **fields,
    )


def parse_history_record(msg: Message) -> Measurement | None:
    """Parse a HISTORY_WEIGHT_DATA (0x09) message; None if status != valid."""
    raw = msg.raw
    if msg.status != 1 or len(raw) < 53:
        return None
    timestamp = struct.unpack_from("<I", raw, 7)[0]
    fields = _parse_body(raw, 11)
    weight, impedance, bfp, muscle = struct.unpack_from("<4H", raw, 32)
    bone = raw[40]
    water, protein, lbm = struct.unpack_from("<3H", raw, 41)
    vfal = raw[47]
    bmr = struct.unpack_from("<H", raw, 48)[0]
    body_age = raw[50]
    bmi = struct.unpack_from("<H", raw, 51)[0]
    return Measurement(
        timestamp=timestamp,
        weight_raw=weight,
        impedance=impedance,
        bfp_raw=bfp,
        muscle_raw=muscle,
        bone_raw=bone,
        water_raw=water,
        protein_raw=protein,
        lbm_raw=lbm,
        vfal=vfal,
        bmr=bmr,
        body_age=body_age,
        bmi_raw=bmi,
        **fields,
    )


# ---------------------------------------------------------------------------
# Request builders
# ---------------------------------------------------------------------------


def build_sync_time(timestamp: int) -> bytes:
    return build_request(CMD_SYNC_TIME, struct.pack("<I", timestamp) + b"\x01")


def build_set_unit(unit: int) -> bytes:
    return build_request(CMD_SET_UNIT, bytes([unit]))


def build_set_hello(enabled: bool) -> bytes:
    return build_request(CMD_SET_HELLO, bytes([1 if enabled else 0]))


def build_user_list() -> bytes:
    return build_request(CMD_USER_LIST_NEW)


def build_current_user(record: UserRecord) -> bytes:
    return build_request(CMD_CURRENT_USER_NEW, record.pack())


def build_update_user(record: UserRecord) -> bytes:
    return build_request(CMD_UPDATE_USER, record.pack())


def build_delete_user(user_id: bytes) -> bytes:
    return build_request(CMD_DEL_USER, user_id)


def build_history_ack() -> bytes:
    return build_request(CMD_HISTORY_WEIGHT_DATA, b"\x00")
