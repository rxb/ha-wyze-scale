"""Experimental Scale Ultra framing and conservative weight assignment."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from ..users import UserProfile
from .client import WyzeScaleClient, WyzeScaleError
from .protocol import MEASURE_STATE_FINAL, Measurement, build_encrypted_frame

MATCH_TOLERANCE_KG = 4.5359237  # 10 lb
COMPLETED_STATES = (2, 3, 4)


def validate_profile_ids(raw: bytes) -> set[str]:
    """Validate the observed single-message, 48-byte Ultra profile list."""
    if len(raw) < 7 or len(raw) != 7 + raw[6] * 48:
        raise ValueError("invalid Ultra profile-list length")
    identifiers: set[str] = set()
    for offset in range(7, len(raw), 48):
        record = raw[offset : offset + 48]
        identifier = record[:16].hex()
        name_length = record[32]
        if not record[:16].strip(b"\x00") or name_length > 15:
            raise ValueError("invalid Ultra profile record")
        if identifier in identifiers:
            raise ValueError("duplicate Ultra profile identifier")
        try:
            record[33 : 33 + name_length].decode("utf-8")
        except UnicodeError as err:
            raise ValueError("invalid Ultra profile name") from err
        identifiers.add(identifier)
    return identifiers


def match_measurement(
    measurement: Measurement,
    profiles: Sequence[UserProfile],
    references: Mapping[str, float],
    known_ids: set[str],
) -> Measurement | None:
    """Assign a completed weight only when exactly one HA user is in range.

    Ultra frames can report the primary profile or an empty ID even when
    the display identifies another person. The ID validates the frame's
    context; the unique reference-weight match selects the HA person.
    """
    if not known_ids:
        return None
    if measurement.measure_state not in COMPLETED_STATES or not measurement.weight_raw:
        return None
    if measurement.user_id.strip(b"\x00") and measurement.user_id_hex not in known_ids:
        return None
    candidates = [
        profile
        for profile in profiles
        if abs(
            measurement.weight_kg - references.get(profile.user_id, profile.weight_kg)
        )
        <= MATCH_TOLERANCE_KG
    ]
    if len(candidates) != 1:
        return None
    profile = candidates[0]
    # Composition and device timestamps have not been validated for Ultra.
    return replace(
        measurement,
        measure_state=MEASURE_STATE_FINAL,
        user_id=bytes.fromhex(profile.user_id),
        sex=int(profile.sex_male),
        age=profile.age,
        height=profile.height_cm,
        athlete_mode=int(profile.athlete_mode),
        only_weight=int(profile.weight_only),
        impedance=0,
        bfp_raw=0,
        muscle_raw=0,
        bone_raw=0,
        water_raw=0,
        protein_raw=0,
        lbm_raw=0,
        vfal=0,
        bmr=0,
        body_age=0,
        bmi_raw=0,
        timestamp=None,
    )


class UltraReadOnlyClient(WyzeScaleClient):
    """Ultra request framing; only time sync and profile reads are permitted."""

    def __init__(self, **kwargs) -> None:
        super().__init__(write_response=False, **kwargs)

    async def _send(self, payload: bytes) -> None:
        if self._key is None:
            raise WyzeScaleError("not connected")
        if len(payload) < 6 or payload[:2] != bytes([0x16, 0]):
            raise WyzeScaleError("unexpected Ultra request")
        body = payload[4:]
        if body[0] not in (0x01, 0x18):
            raise WyzeScaleError("unsupported Ultra command")
        if len(body) > 255:
            raise WyzeScaleError("Ultra request too long")
        counter = self._next_counter()
        request = bytes([0x20 | counter, 1, len(body), sum(body) & 0xFF]) + body
        await self._write(build_encrypted_frame(counter, request, self._key))
