"""Recipient list + raw report reads in the entry-alerts store.

DISPLAY / CONFIG ONLY. These helpers feed the dashboard's Scout Reports and
Report Recipients tabs and widen the scouts' send list. No order path is
exercised anywhere here. Every test runs against the LOCAL file fallback
(bucket=None), so nothing touches GCS or the network.
"""

from __future__ import annotations

import json

import pytest

from src.entry_alerts import store


@pytest.fixture(autouse=True)
def local_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "LOCAL_DIR", tmp_path / "entry_alerts")
    return tmp_path / "entry_alerts"


def _write_day(day: str, plays: dict) -> None:
    path = store._local_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": day, "plays": plays, "fired": []}), encoding="utf-8")


# --- email validation --------------------------------------------------------


@pytest.mark.parametrize("addr", ["a@b.co", "First.Last@example.com", "x+tag@sub.domain.io"])
def test_valid_email_accepts_reasonable_addresses(addr):
    assert store.valid_email(addr)


@pytest.mark.parametrize("addr", ["", "nope", "no@tld", "a b@x.com", "@x.com", "a@@x.com", None])
def test_valid_email_rejects_garbage(addr):
    assert not store.valid_email(addr)


# --- recipients: add / remove / dedupe ---------------------------------------


def test_add_recipient_persists_and_is_readable():
    ok, msg = store.add_recipient("trader@example.com", bucket=None)
    assert ok and "Added" in msg
    assert store.load_recipients(bucket=None) == ["trader@example.com"]


def test_add_recipient_rejects_invalid_without_writing():
    ok, msg = store.add_recipient("not-an-email", bucket=None)
    assert not ok and "valid" in msg.lower()
    assert store.load_recipients(bucket=None) == []


def test_add_recipient_is_case_insensitively_deduped():
    store.add_recipient("Trader@Example.com", bucket=None)
    ok, msg = store.add_recipient("trader@example.com", bucket=None)
    assert not ok and "already" in msg.lower()
    assert store.load_recipients(bucket=None) == ["Trader@Example.com"]


def test_remove_recipient_removes_case_insensitively():
    store.add_recipient("a@x.com", bucket=None)
    store.add_recipient("b@x.com", bucket=None)
    ok, msg = store.remove_recipient("A@X.COM", bucket=None)
    assert ok and "Removed" in msg
    assert store.load_recipients(bucket=None) == ["b@x.com"]


def test_remove_recipient_absent_is_reported_not_crashed():
    ok, msg = store.remove_recipient("ghost@x.com", bucket=None)
    assert not ok and "not on the list" in msg


def test_load_recipients_on_corrupt_file_is_empty():
    store._recipients_local_path().parent.mkdir(parents=True, exist_ok=True)
    store._recipients_local_path().write_text("{not json", encoding="utf-8")
    assert store.load_recipients(bucket=None) == []


def test_recipients_for_send_puts_default_first_and_dedupes():
    store.add_recipient("extra@x.com", bucket=None)
    store.add_recipient("Default@x.com", bucket=None)  # same as default, different case
    result = store.recipients_for_send("default@x.com", bucket=None)
    assert result[0] == "default@x.com"
    assert "extra@x.com" in result
    # the default appears exactly once despite the case-variant on the list
    assert sum(1 for r in result if r.lower() == "default@x.com") == 1


def test_recipients_for_send_falls_back_to_default_on_read_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bucket down")

    monkeypatch.setattr(store, "load_recipients", boom)
    assert store.recipients_for_send("only@x.com", bucket=None) == ["only@x.com"]


# --- raw report reads --------------------------------------------------------


def test_list_report_days_newest_first_and_capped():
    for day in ["2026-08-30", "2026-09-01", "2026-08-31", "2026-09-02"]:
        _write_day(day, {})
    days = store.list_report_days(3, bucket=None)
    assert days == ["2026-09-02", "2026-09-01", "2026-08-31"]


def test_list_report_days_ignores_recipients_file():
    _write_day("2026-09-02", {})
    store.save_recipients(["a@x.com"], bucket=None)  # writes recipients.json in LOCAL_DIR
    assert store.list_report_days(14, bucket=None) == ["2026-09-02"]


def test_load_report_raw_preserves_optional_fields():
    _write_day(
        "2026-09-02",
        {
            "options:AAPL:call:2026-09-02": {
                "id": "options:AAPL:call:2026-09-02",
                "source": "options",
                "symbol": "AAPL",
                "direction": "call",
                "entry": 100.0,
                "target": 110.0,
                "stop": 95.0,
                "date": "2026-09-02",
                "conviction": 72,
                "contract": "O:AAPL...C",
                "rank": 1,
                "outcome": {"verdict": "WIN", "actual": 111.2, "return_pct": 8.5},
            }
        },
    )
    raw = store.load_report_raw("2026-09-02", bucket=None)
    plays = store.report_plays(raw)
    assert len(plays) == 1
    rec = plays[0]
    assert rec["conviction"] == 72 and rec["contract"] == "O:AAPL...C"
    outcome = store.play_outcome(rec)
    assert outcome["verdict"] == "WIN" and outcome["return_pct"] == 8.5


def test_load_report_raw_missing_day_is_none():
    assert store.load_report_raw("2026-01-01", bucket=None) is None


def test_report_summary_pending_when_no_outcomes():
    _write_day(
        "2026-09-02",
        {"p": {"id": "p", "symbol": "X", "direction": "call", "entry": 1, "target": 2, "stop": 0.5}},
    )
    raw = store.load_report_raw("2026-09-02", bucket=None)
    summary = store.report_summary(raw)
    assert summary["plays"] == 1 and summary["settled"] == 0
    assert "accuracy_pct" not in summary


def test_report_summary_accuracy_once_outcomes_exist():
    _write_day(
        "2026-09-02",
        {
            "a": {"id": "a", "symbol": "A", "direction": "call", "entry": 1, "target": 2, "stop": 0.5, "outcome": {"verdict": "WIN"}},
            "b": {"id": "b", "symbol": "B", "direction": "call", "entry": 1, "target": 2, "stop": 0.5, "outcome": {"verdict": "LOSS"}},
            "c": {"id": "c", "symbol": "C", "direction": "call", "entry": 1, "target": 2, "stop": 0.5, "outcome": "WIN"},
            "d": {"id": "d", "symbol": "D", "direction": "call", "entry": 1, "target": 2, "stop": 0.5},
        },
    )
    raw = store.load_report_raw("2026-09-02", bucket=None)
    summary = store.report_summary(raw)
    assert summary["plays"] == 4 and summary["settled"] == 3
    assert summary["accuracy_pct"] == pytest.approx(66.7, abs=0.1)


def test_play_outcome_absent_is_none_and_bare_string_normalized():
    assert store.play_outcome({"symbol": "X"}) is None
    assert store.play_outcome({"actual": "OPEN"}) == {"verdict": "OPEN"}
