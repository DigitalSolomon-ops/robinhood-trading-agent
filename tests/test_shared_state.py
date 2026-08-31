"""Shared arm/disarm state: the local FileArmStore backs the same interface the
Firestore backend will, so the dashboard toggle and trader agree either way."""
from __future__ import annotations

from pathlib import Path

from src.shared_state import FileArmStore, build_arm_store, lane_stop_path


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
