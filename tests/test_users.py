"""Unit tests for the pure user reconciliation logic."""

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).parent.parent / "custom_components" / "wyze_scale")
)

from users import (  # noqa: E402
    UserProfile,
    default_name,
    merge_import_name,
    reconcile,
)


def _p(user_id: str, **kw) -> UserProfile:
    base = dict(
        name="",
        sex_male=True,
        age=40,
        height_cm=180,
        weight_kg=80.0,
    )
    base.update(kw)
    return UserProfile(user_id=user_id, **base)


def test_new_user_id_is_16_bytes_hex():
    uid = UserProfile.new_user_id()
    assert len(uid) == 32
    assert bytes.fromhex(uid)  # valid hex, 16 bytes
    assert uid != UserProfile.new_user_id()


def test_subentry_roundtrip():
    p = _p("aa" * 16, name="Alice", sex_male=False, athlete_mode=True)
    restored = UserProfile.from_subentry_data(p.to_subentry_data())
    assert restored == p


def test_scale_fields_ignore_name_and_weight_noise():
    a = _p("aa" * 16, name="Alice", weight_kg=80.004)
    b = _p("aa" * 16, name="Bob", weight_kg=80.0)
    # name differs, weight rounds to same kg x100 -> considered equal on scale
    assert a.matches_scale(b)
    c = _p("aa" * 16, weight_kg=80.5)
    assert not a.matches_scale(c)
    d = _p("aa" * 16, age=41)
    assert not a.matches_scale(d)


def test_import_unknown_scale_user():
    scale = {"aa" * 16: _p("aa" * 16)}
    plan = reconcile(scale, subentries={}, tombstones=set())
    assert [u.user_id for u in plan.to_import] == ["aa" * 16]
    assert not plan.to_create and not plan.to_update and not plan.to_delete


def test_create_subentry_missing_from_scale():
    sub = {"bb" * 16: _p("bb" * 16, name="B")}
    plan = reconcile(scale_users={}, subentries=sub, tombstones=set())
    assert [u.user_id for u in plan.to_create] == ["bb" * 16]
    assert not plan.to_import


def test_update_on_profile_drift():
    uid = "cc" * 16
    scale = {uid: _p(uid, age=40)}
    sub = {uid: _p(uid, name="C", age=41)}
    plan = reconcile(scale, sub, tombstones=set())
    assert [u.user_id for u in plan.to_update] == [uid]
    assert not plan.to_import and not plan.to_create


def test_no_update_when_only_name_differs():
    uid = "cc" * 16
    scale = {uid: _p(uid, name="")}
    sub = {uid: _p(uid, name="Renamed")}
    plan = reconcile(scale, sub, tombstones=set())
    assert plan.is_empty


def test_tombstone_deletes_and_beats_import():
    uid = "dd" * 16
    scale = {uid: _p(uid)}
    # tombstoned + not a subentry: must delete from scale, NOT re-import
    plan = reconcile(scale, subentries={}, tombstones={uid})
    assert plan.to_delete == [uid]
    assert not plan.to_import
    assert not plan.tombstones_cleared


def test_tombstone_cleared_when_gone_from_scale():
    uid = "ee" * 16
    plan = reconcile(scale_users={}, subentries={}, tombstones={uid})
    assert plan.tombstones_cleared == [uid]
    assert not plan.to_delete


def test_merge_import_name():
    p = _p("ff" * 16, name="")
    named = merge_import_name(p)
    assert named.name == default_name("ff" * 16)
    # existing name preserved
    keep = _p("ff" * 16, name="Keep")
    assert merge_import_name(keep).name == "Keep"


def test_steady_state_is_empty():
    uid = "12" * 16
    scale = {uid: _p(uid, name="")}
    sub = {uid: _p(uid, name="Alice")}  # name-only diff, no scale change
    plan = reconcile(scale, sub, tombstones=set())
    assert plan.is_empty
