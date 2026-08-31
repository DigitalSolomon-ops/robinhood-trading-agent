"""Per-lane arm/disarm toggle in the 007 dashboard.

Disarm halts a lane in one click; arm enables it and REQUIRES an explicit confirm.
Only a human request through the (IAP-gated) dashboard reaches these routes -- the
agent never flips them. Crypto and equities track arm state via their stop file
(disarm writes it, arm removes it); the leveraged OPTIONS lane is POSITIVE and
fail-closed -- disarm removes its ARM_STATE marker, arm writes it, and the marker
is a DIFFERENT file from the kill-switch stop file.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from src.dashboard import _lane_stop_paths, dashboard_app
from src.shared_state import build_arm_store, lane_arm_marker_path
from tests.test_dashboard import make_dashboard_root


def _client(root: Path) -> TestClient:
    return TestClient(dashboard_app(root))


def _rules(root: Path) -> dict:
    return yaml.safe_load((root / "config" / "trading_rules.yaml").read_text(encoding="utf-8"))


def test_arm_panel_lists_all_three_lanes(tmp_path: Path) -> None:
    r = _client(make_dashboard_root(tmp_path)).get("/arm")
    assert r.status_code == 200
    for label in ("Crypto", "Equities", "Options"):
        assert label in r.text


def test_disarm_writes_the_lane_stop_file(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    _, eq_stop = _lane_stop_paths(root, _rules(root))["equities"]
    assert not eq_stop.exists()
    _client(root).post("/kill/equities/disarm", follow_redirects=False)
    assert eq_stop.exists()  # disarmed = halted


def test_arm_without_confirm_does_not_enable_the_lane(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    _, eq_stop = _lane_stop_paths(root, _rules(root))["equities"]
    c = _client(root)
    c.post("/kill/equities/disarm", follow_redirects=False)
    assert eq_stop.exists()
    # arming is deliberate: no confirm box -> the stop file stays, lane stays halted
    c.post("/kill/equities/arm", data={}, follow_redirects=False)
    assert eq_stop.exists()


def test_arm_with_confirm_enables_the_lane(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    _, eq_stop = _lane_stop_paths(root, _rules(root))["equities"]
    c = _client(root)
    c.post("/kill/equities/disarm", follow_redirects=False)
    assert eq_stop.exists()
    c.post("/kill/equities/arm", data={"confirm_arm": "on"}, follow_redirects=False)
    assert not eq_stop.exists()  # armed = stop file removed


def test_options_is_disarmed_by_default_and_arms_via_a_positive_marker(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    rules = _rules(root)
    _, opt_stop = _lane_stop_paths(root, rules)["options"]
    marker = lane_arm_marker_path(root, "options")
    c = _client(root)
    # Default: DISARMED (no marker). Under the old absence==armed rule this was
    # armed, so the assertion fails if the fail-closed options gate is reverted.
    assert build_arm_store(root, rules).is_armed("options") is False
    assert not marker.exists()
    # Arm with confirm -> writes the POSITIVE marker, never the kill-switch file.
    c.post("/kill/options/arm", data={"confirm_arm": "on"}, follow_redirects=False)
    assert marker.exists()
    assert not opt_stop.exists()
    assert build_arm_store(root, rules).is_armed("options") is True
    # Disarm -> removes the marker.
    c.post("/kill/options/disarm", follow_redirects=False)
    assert not marker.exists()
    assert build_arm_store(root, rules).is_armed("options") is False


def test_disarming_options_leaves_equities_alone(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    paths = _lane_stop_paths(root, _rules(root))
    c = _client(root)
    c.post("/kill/options/disarm", follow_redirects=False)
    assert not paths["equities"][1].exists()  # disarming options left equities alone


def test_unknown_lane_is_a_safe_noop(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    r = _client(root).post("/kill/bogus/disarm", follow_redirects=False)
    assert r.status_code == 303  # redirects back, no crash, nothing written
