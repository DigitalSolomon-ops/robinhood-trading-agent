"""Shared arm/disarm state: the local FileArmStore backs the same interface the
Firestore backend will, so the dashboard toggle and trader agree either way."""
from __future__ import annotations

from pathlib import Path

import src.shared_state as shared_state
from src.shared_state import (
    DisarmedArmStore,
    FileArmStore,
    build_arm_store,
    lane_arm_marker_path,
    lane_stop_path,
)


def _rules() -> dict:
    return {
        "kill_switch": {"stop_file": "STOP_TRADING"},
        "equities": {"kill_switch": {"stop_file": "STOP_TRADING_EQUITIES"}},
        "options": {"kill_switch": {"stop_file": "STOP_TRADING_OPTIONS"}},
    }


def test_arm_is_absence_of_stop_file(tmp_path: Path) -> None:
    s = FileArmStore(tmp_path, _rules())
    assert s.is_armed("equities") is True  # no stop file = armed


def test_disarm_then_arm_roundtrip(tmp_path: Path) -> None:
    s = FileArmStore(tmp_path, _rules())
    s.set_armed("equities", False)
    assert s.is_armed("equities") is False
    assert lane_stop_path(tmp_path, _rules(), "equities").exists()
    s.set_armed("equities", True)
    assert s.is_armed("equities") is True
    assert not lane_stop_path(tmp_path, _rules(), "equities").exists()


def test_lanes_are_independent(tmp_path: Path) -> None:
    s = FileArmStore(tmp_path, _rules())
    s.set_armed("options", False)
    assert s.is_armed("options") is False
    assert s.is_armed("equities") is True  # untouched


def test_unknown_lane_reads_disarmed_and_write_is_a_noop(tmp_path: Path) -> None:
    s = FileArmStore(tmp_path, _rules())
    assert s.is_armed("bogus") is False  # fail safe
    s.set_armed("bogus", True)  # no crash, no file


def test_build_arm_store_defaults_to_file_backend(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TRADER_ARM_FIRESTORE_PROJECT", raising=False)
    assert isinstance(build_arm_store(tmp_path, _rules()), FileArmStore)


# --- the OPTIONS lane is POSITIVE + fail-closed (absence == DISARMED) ---------
# The leveraged options lane must never read ARMED from the ABSENCE of a file.
# Its arm state is a positive marker, separate from the kill-switch stop file.


def test_options_is_disarmed_by_default(tmp_path: Path) -> None:
    s = FileArmStore(tmp_path, _rules())
    # No marker present -> DISARMED. Under the old absence==armed rule this was
    # True, so this assertion fails if the fail-closed options gate is reverted.
    assert s.is_armed("options") is False
    marker = lane_arm_marker_path(tmp_path, "options")
    assert marker is not None
    assert not marker.exists()


def test_options_arm_writes_a_positive_marker_not_the_stop_file(tmp_path: Path) -> None:
    rules = _rules()
    s = FileArmStore(tmp_path, rules)
    s.set_armed("options", True, by="test")
    assert s.is_armed("options") is True
    assert lane_arm_marker_path(tmp_path, "options").exists()
    # The kill-switch stop file is a DIFFERENT file and is never created by arming.
    assert not lane_stop_path(tmp_path, rules, "options").exists()
    s.set_armed("options", False)
    assert s.is_armed("options") is False
    assert not lane_arm_marker_path(tmp_path, "options").exists()


def test_crypto_and_equities_keep_absence_equals_armed(tmp_path: Path) -> None:
    s = FileArmStore(tmp_path, _rules())
    # Unchanged semantics: no stop file == armed, and no positive marker is used.
    assert s.is_armed("crypto") is True
    assert s.is_armed("equities") is True
    assert lane_arm_marker_path(tmp_path, "crypto") is None
    assert lane_arm_marker_path(tmp_path, "equities") is None


# --- build_arm_store fails CLOSED when Firestore is requested but unavailable --


def test_firestore_failure_returns_an_always_disarmed_store_never_local(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TRADER_ARM_FIRESTORE_PROJECT", "some-project")

    def _boom(*args, **kwargs):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr(shared_state, "FirestoreArmStore", _boom)
    store = build_arm_store(tmp_path, _rules())
    # NOT a local fail-open fallback -- an always-disarmed store instead.
    assert isinstance(store, DisarmedArmStore)
    assert not isinstance(store, FileArmStore)
    for lane in ("crypto", "equities", "options"):
        assert store.is_armed(lane) is False
    store.set_armed("options", True)  # no-op, cannot arm a broken cloud backend
    assert store.is_armed("options") is False
