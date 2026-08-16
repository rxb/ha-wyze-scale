"""Unit tests for the pure user reconciliation logic."""

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).parent.parent / "custom_components" / "wyze_scale")
)

from users import (  # noqa: E402
    UserProfile,
    cm_to_ft_in,
    default_name,
    ft_in_to_cm,
    kg_to_lb,
    lb_to_kg,
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


def test_weight_conversions():
    assert lb_to_kg(kg_to_lb(80.0)) == pytest.approx(80.0)
    assert kg_to_lb(80.0) == pytest.approx(176.37, abs=0.01)
    assert lb_to_kg(180.0) == pytest.approx(81.6466, abs=0.001)


def test_height_conversions():
    assert cm_to_ft_in(178) == (5, 10)  # 70.08 in -> 5'10"
    assert cm_to_ft_in(152.4) == (5, 0)
    assert ft_in_to_cm(5, 10) == 178  # 70 in -> 177.8 -> 178
    assert ft_in_to_cm(6, 0) == 183
    # inches carry into feet at 12
    assert cm_to_ft_in(182.9)[1] < 12


def test_steady_state_is_empty():
    uid = "12" * 16
    scale = {uid: _p(uid, name="")}
    sub = {uid: _p(uid, name="Alice")}  # name-only diff, no scale change
    plan = reconcile(scale, sub, tombstones=set())
    assert plan.is_empty
