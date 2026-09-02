"""Scout Reports + Report Recipients dashboard tabs.

DISPLAY / CONFIG ONLY. These tabs render persisted scout plays and manage the
report distribution list. No order path, arm panel, or kill switch is touched.
Every test forces the LOCAL file fallback (bucket -> None) so nothing reaches
GCS or the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import dashboard
from src.dashboard import dashboard_app
from src.entry_alerts import store


@pytest.fixture(autouse=True)
def local_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "LOCAL_DIR", tmp_path / "entry_alerts")
    # Force the local fallback deterministically regardless of any live ADC.
    monkeypatch.setattr(dashboard, "_entry_alerts_bucket", lambda: None)
    return tmp_path / "entry_alerts"


def _client(tmp_path: Path) -> TestClient:
    return TestClient(dashboard_app(tmp_path))


def _write_day(day: str, plays: dict) -> None:
    path = store._local_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": day, "plays": plays, "fired": []}), encoding="utf-8")


def test_nav_shows_the_two_new_tabs(tmp_path):
    html = _client(tmp_path).get("/").text
    assert "Scout Reports" in html
    assert "Report Recipients" in html


def test_scout_reports_empty_state_never_crashes(tmp_path):
    resp = _client(tmp_path).get("/scout-reports")
    assert resp.status_code == 200
    assert "No scout report days found" in resp.text


def test_scout_reports_lists_days_and_renders_plan_columns(tmp_path):
    _write_day(
        "2026-09-02",
        {
            "options:AAPL:call:2026-09-02": {
                "id": "options:AAPL:call:2026-09-02",
                "source": "options",
                "symbol": "AAPL",
                "direction": "call",
                "entry": 316.85,
                "target": 328.21,
                "stop": 310.04,
                "date": "2026-09-02",
            }
        },
    )
    resp = _client(tmp_path).get("/scout-reports")
    assert resp.status_code == 200
    body = resp.text
    assert "2026-09-02" in body
    assert "AAPL" in body and "CALL" in body
    assert "316.85" in body and "328.21" in body and "310.04" in body
    # Plan-vs-actual scaffold with no outcome yet reads pending.
    assert "pending" in body
    assert "accuracy: pending first settled runs" in body


def test_scout_reports_lights_up_when_outcome_present(tmp_path):
    _write_day(
        "2026-09-02",
        {
            "a": {"id": "a", "source": "options", "symbol": "NVDA", "direction": "call",
                  "entry": 100, "target": 110, "stop": 95,
                  "outcome": {"verdict": "WIN", "actual": 111.2, "return_pct": 8.5}},
        },
    )
    body = _client(tmp_path).get("/scout-reports?day=2026-09-02").text
    assert "WIN" in body
    assert "+8.5%" in body
    assert "accuracy: 100.0%" in body


def test_scout_reports_bad_day_param_falls_back_to_latest(tmp_path):
    _write_day("2026-09-02", {"a": {"id": "a", "symbol": "X", "direction": "call",
                                    "entry": 1, "target": 2, "stop": 0.5, "source": "options"}})
    body = _client(tmp_path).get("/scout-reports?day=not-a-day").text
    assert "2026-09-02" in body and "No plays recorded" not in body


def test_recipients_tab_empty_state(tmp_path):
    resp = _client(tmp_path).get("/scout-recipients")
    assert resp.status_code == 200
    assert "No additional recipients yet" in resp.text


def test_recipients_add_then_shows_and_can_remove(tmp_path):
    client = _client(tmp_path)
    add = client.post("/scout-recipients", data={"action": "add", "email": "trader@example.com"})
    assert add.status_code == 200
    assert "Added trader@example.com" in add.text
    assert "trader@example.com" in add.text
    assert store.load_recipients(bucket=None) == ["trader@example.com"]

    remove = client.post("/scout-recipients", data={"action": "remove", "email": "trader@example.com"})
    assert "Removed trader@example.com" in remove.text
    assert store.load_recipients(bucket=None) == []


def test_recipients_add_invalid_is_rejected_with_message(tmp_path):
    resp = _client(tmp_path).post("/scout-recipients", data={"action": "add", "email": "nope"})
    assert resp.status_code == 200
    assert "valid" in resp.text.lower()
    assert store.load_recipients(bucket=None) == []


def test_recipients_add_duplicate_is_rejected(tmp_path):
    client = _client(tmp_path)
    client.post("/scout-recipients", data={"action": "add", "email": "a@x.com"})
    resp = client.post("/scout-recipients", data={"action": "add", "email": "A@X.com"})
    assert "already" in resp.text.lower()
    assert store.load_recipients(bucket=None) == ["a@x.com"]
