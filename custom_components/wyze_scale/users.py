"""Pure user-profile model and scale/HA reconciliation logic.

No Home Assistant or BLE imports here so it can be unit tested in isolation.

The integration models each scale user as a config subentry (the desired
state, edited in the UI). The scale itself also stores users (created here
or in the Wyze app). This module computes what has to happen to make the
two agree:

- users on the scale that HA doesn't know about yet -> import as subentries
- subentries missing from the scale -> create on the scale
- subentries whose profile differs from the scale -> update on the scale
- users the person deleted in HA (tombstoned) -> delete from the scale
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class UserProfile:
    """A scale user's biometric profile (the editable subentry data)."""

    user_id: str  # 32-hex-char (16-byte) identifier
    name: str
    sex_male: bool
    age: int
    height_cm: int
    weight_kg: float
    athlete_mode: bool = False
    weight_only: bool = False

    @staticmethod
    def new_user_id() -> str:
        """Generate a fresh random 16-byte user id as hex."""
        return secrets.token_bytes(16).hex()

    def to_subentry_data(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "sex": "male" if self.sex_male else "female",
            "age": self.age,
            "height_cm": self.height_cm,
            "weight_kg": self.weight_kg,
            "athlete_mode": self.athlete_mode,
            "weight_only": self.weight_only,
        }

    @classmethod
    def from_subentry_data(cls, data: dict[str, Any]) -> "UserProfile":
        return cls(
            user_id=data["user_id"],
            name=data.get("name", ""),
            sex_male=data.get("sex", "female") == "male",
            age=int(data.get("age", 0)),
            height_cm=int(data.get("height_cm", 0)),
            weight_kg=float(data.get("weight_kg", 0.0)),
            athlete_mode=bool(data.get("athlete_mode", False)),
            weight_only=bool(data.get("weight_only", False)),
        )

    def scale_fields(self) -> tuple:
        """The fields the scale actually stores (used to detect drift).

        Excludes the HA-only display name. Weight is compared in the
        scale's kg x100 integer units so float noise doesn't cause churn.
        """
        return (
            1 if self.sex_male else 0,
            int(self.age),
            int(self.height_cm),
            1 if self.athlete_mode else 0,
            1 if self.weight_only else 0,
            round(self.weight_kg * 100),
        )

    def matches_scale(self, other: "UserProfile") -> bool:
        return fields_match(self.scale_fields(), other.scale_fields())


# kg x100 units (~0.03 kg). Absorbs the rounding when a weight is shown and
# re-entered in pounds, so an unchanged US-customary profile doesn't look
# like a change and trigger a needless push to the scale.
WEIGHT_MATCH_TOLERANCE = 3


def fields_match(a, b) -> bool:
    """Compare two scale_fields sequences: profile fields exact, weight close.

    The weight (last element) is an approximate matching value, so it's
    compared within WEIGHT_MATCH_TOLERANCE rather than exactly.
    """
    a = list(a)
    b = list(b)
    if not a or len(a) != len(b):
        return False
    return a[:-1] == b[:-1] and abs(a[-1] - b[-1]) <= WEIGHT_MATCH_TOLERANCE


def default_name(user_id: str) -> str:
    """Human-ish label for an imported user with no name yet."""
    return f"Scale user {user_id[:6].upper()}"


# ---------------------------------------------------------------------------
# Unit conversions for localized entry forms.
#
# The scale (and UserProfile) always stores height in cm and weight in kg;
# these convert to/from US-customary units for display in the config UI.
# ---------------------------------------------------------------------------

_KG_PER_LB = 0.45359237
_CM_PER_IN = 2.54


def kg_to_lb(kg: float) -> float:
    return kg / _KG_PER_LB


def lb_to_kg(lb: float) -> float:
    return lb * _KG_PER_LB


def cm_to_ft_in(cm: float) -> tuple[int, int]:
    """Convert centimeters to (feet, inches), inches rounded to nearest."""
    total_in = round(cm / _CM_PER_IN)
    feet, inches = divmod(total_in, 12)
    return feet, inches


def ft_in_to_cm(feet: int, inches: int) -> int:
    """Convert (feet, inches) to centimeters, rounded to nearest."""
    return round((feet * 12 + inches) * _CM_PER_IN)


@dataclass
class ReconcilePlan:
    """What to do to make the scale and HA subentries agree."""

    to_import: list[UserProfile]  # add these as HA subentries
    to_create: list[UserProfile]  # create these on the scale
    to_update: list[UserProfile]  # update these on the scale
    to_delete: list[str]  # delete these user_ids from the scale
    tombstones_cleared: list[str]  # tombstoned ids now gone from the scale

    @property
    def is_empty(self) -> bool:
        return not (
            self.to_import
            or self.to_create
            or self.to_update
            or self.to_delete
        )


def reconcile(
    scale_users: dict[str, UserProfile],
    subentries: dict[str, UserProfile],
    tombstones: set[str],
) -> ReconcilePlan:
    """Diff scale users against HA subentries.

    Args:
        scale_users: profiles currently stored on the scale, by user_id.
        subentries: desired profiles from HA config subentries, by user_id.
        tombstones: user_ids the person deleted in HA that may still be on
            the scale.

    A tombstoned id takes priority over import: a user the person removed is
    deleted from the scale rather than re-imported.
    """
    to_import: list[UserProfile] = []
    to_create: list[UserProfile] = []
    to_update: list[UserProfile] = []
    to_delete: list[str] = []
    tombstones_cleared: list[str] = []

    for user_id, scale_profile in scale_users.items():
        if user_id in tombstones:
            to_delete.append(user_id)
            continue
        if user_id not in subentries:
            to_import.append(scale_profile)

    for user_id in tombstones:
        if user_id not in scale_users:
            tombstones_cleared.append(user_id)

    for user_id, desired in subentries.items():
        scale_profile = scale_users.get(user_id)
        if scale_profile is None:
            to_create.append(desired)
        elif not desired.matches_scale(scale_profile):
            to_update.append(desired)

    return ReconcilePlan(
        to_import=to_import,
        to_create=to_create,
        to_update=to_update,
        to_delete=to_delete,
        tombstones_cleared=tombstones_cleared,
    )


def merge_import_name(profile: UserProfile) -> UserProfile:
    """Give an imported profile a display name if it lacks one."""
    if profile.name:
        return profile
    return replace(profile, name=default_name(profile.user_id))
