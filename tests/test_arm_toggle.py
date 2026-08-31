"""Per-lane arm/disarm toggle in the 007 dashboard.

Disarm halts a lane (writes its stop file) in one click; arm enables it (removes
the stop file) and REQUIRES an explicit confirm. Only a human request through the
(IAP-gated) dashboard reaches these routes -- the agent never flips them.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from src.dashboard import _lane_stop_paths, dashboard_app
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


def test_lanes_are_independent(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    paths = _lane_stop_paths(root, _rules(root))
    c = _client(root)
    c.post("/kill/options/disarm", follow_redirects=False)
    assert paths["options"][1].exists()
    assert not paths["equities"][1].exists()  # disarming options left equities alone


def test_unknown_lane_is_a_safe_noop(tmp_path: Path) -> None:
    root = make_dashboard_root(tmp_path)
    r = _client(root).post("/kill/bogus/disarm", follow_redirects=False)
    assert r.status_code == 303  # redirects back, no crash, nothing written
