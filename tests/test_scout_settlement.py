"""Scout settlement / accuracy engine.

ANALYSIS ONLY -- these assert an after-the-fact verdict (did the underlying hit
target before stop) and the storage merge that records it. Nothing here trades.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import src.entry_alerts.store as store
from src.scout_settlement.engine import run_settlement
from src.scout_settlement.settlement import BarLite, bar_date, settle


# --- helpers -----------------------------------------------------------------


def _ts(day: str) -> int:
    """Epoch ms for midnight-UTC of a date (bar_date round-trips it to that date)."""
    y, m, d = (int(x) for x in day.split("-"))
    return int(datetime(y, m, d, tzinfo=UTC).timestamp() * 1000)


def _daily(day: str, high: float, low: float) -> BarLite:
    return BarLite(date=day, high=high, low=low)


# --- pure settlement: direction + ordering -----------------------------------


def test_bullish_win_on_second_session():
    bars = [_daily("2026-09-02", 105, 98), _daily("2026-09-03", 121, 101)]
    out = settle(direction="call", entry=100, target=120, stop=90, daily_bars=bars, horizon_days=10)
    assert out["verdict"] == "WIN"
    assert out["hit_date"] == "2026-09-03"
    assert out["sessions_to_hit"] == 2
    assert out["actual"] == 120
    assert out["return_pct"] == 20.0  # (120-100)/100
    assert out["resolved"] == "daily"
    assert out["metric"] == "target_before_stop"


def test_bullish_loss_on_first_session():
    bars = [_daily("2026-09-02", 108, 89)]
    out = settle(direction="long", entry=100, target=120, stop=90, daily_bars=bars, horizon_days=10)
    assert out["verdict"] == "LOSS"
    assert out["sessions_to_hit"] == 1
    assert out["actual"] == 90
    assert out["return_pct"] == -10.0  # (90-100)/100


def test_bearish_put_win_low_reaches_target():
    # put: target BELOW entry, stop ABOVE entry
    bars = [_daily("2026-09-02", 102, 88)]
    out = settle(direction="put", entry=100, target=90, stop=110, daily_bars=bars, horizon_days=10)
    assert out["verdict"] == "WIN"
    assert out["actual"] == 90
    assert out["return_pct"] == 10.0  # (entry-target)/entry, favorable = positive


def test_bearish_put_loss_high_reaches_stop():
    bars = [_daily("2026-09-02", 111, 95)]
    out = settle(direction="put", entry=100, target=90, stop=110, daily_bars=bars, horizon_days=10)
    assert out["verdict"] == "LOSS"
    assert out["actual"] == 110
    assert out["return_pct"] == -10.0  # (entry-stop)/entry = negative


def test_open_when_nothing_touched_in_horizon():
    bars = [_daily("2026-09-02", 105, 96), _daily("2026-09-03", 108, 97)]
    out = settle(direction="call", entry=100, target=120, stop=90, daily_bars=bars, horizon_days=10)
    assert out["verdict"] == "OPEN"
    assert out["resolved"] == "horizon"
    assert out["sessions_checked"] == 2


def test_open_when_no_bars_yet():
    out = settle(direction="call", entry=100, target=120, stop=90, daily_bars=[], horizon_days=10)
    assert out["verdict"] == "OPEN"
    assert out["resolved"] == "no_data"


def test_horizon_caps_the_walk():
    # target only hit on the 3rd session, but horizon is 2 -> OPEN
    bars = [_daily("2026-09-02", 105, 96), _daily("2026-09-03", 108, 97), _daily("2026-09-04", 130, 99)]
    out = settle(direction="call", entry=100, target=120, stop=90, daily_bars=bars, horizon_days=2)
    assert out["verdict"] == "OPEN"


def test_first_touch_wins_even_if_stop_comes_later():
    bars = [_daily("2026-09-02", 121, 101), _daily("2026-09-03", 100, 85)]
    out = settle(direction="call", entry=100, target=120, stop=90, daily_bars=bars, horizon_days=10)
    assert out["verdict"] == "WIN"
    assert out["sessions_to_hit"] == 1


# --- same-session ambiguity (both levels in one daily range) ------------------


def test_same_day_tie_minute_resolves_win():
    bars = [_daily("2026-09-02", 125, 85)]  # spans both 120 and 90
    minutes = {"2026-09-02": [_daily("2026-09-02", 121, 118)]}  # target first
    out = settle(
        direction="call", entry=100, target=120, stop=90, daily_bars=bars, horizon_days=10,
        minute_bars_for=lambda d: minutes.get(d, []),
    )
    assert out["verdict"] == "WIN"
    assert out["resolved"] == "minute"


def test_same_day_tie_minute_resolves_loss():
    bars = [_daily("2026-09-02", 125, 85)]
    minutes = {"2026-09-02": [_daily("2026-09-02", 95, 88)]}  # stop first (low 88 <= 90)
    out = settle(
        direction="call", entry=100, target=120, stop=90, daily_bars=bars, horizon_days=10,
        minute_bars_for=lambda d: minutes.get(d, []),
    )
    assert out["verdict"] == "LOSS"
    assert out["resolved"] == "minute"


def test_same_day_tie_no_provider_is_conservative_loss():
    bars = [_daily("2026-09-02", 125, 85)]
    out = settle(direction="call", entry=100, target=120, stop=90, daily_bars=bars, horizon_days=10)
    assert out["verdict"] == "LOSS"
    assert out["resolved"] == "ambiguous_conservative_loss"


def test_same_day_tie_empty_minutes_is_conservative_loss():
    bars = [_daily("2026-09-02", 125, 85)]
    out = settle(
        direction="call", entry=100, target=120, stop=90, daily_bars=bars, horizon_days=10,
        minute_bars_for=lambda d: [],
    )
    assert out["verdict"] == "LOSS"
    assert out["resolved"] == "ambiguous_conservative_loss"


def test_bar_date_maps_timestamp_to_session_date():
    assert bar_date(_ts("2026-09-02")) == "2026-09-02"


# --- storage merge: outcome never clobbers other fields ----------------------


def _seed_day(tmpdir, day: str, doc: dict) -> None:
    (tmpdir / f"{day}.json").write_text(json.dumps(doc), encoding="utf-8")


def test_save_outcomes_preserves_levels_fired_and_siblings(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "LOCAL_DIR", tmp_path)
    day = "2026-09-02"
    doc = {
        "date": day,
        "fired": ["options:AAA:call:2026-09-02"],
        "plays": {
            "options:AAA:call:2026-09-02": {
                "id": "options:AAA:call:2026-09-02", "source": "options", "symbol": "AAA",
                "direction": "call", "entry": 100, "target": 120, "stop": 90, "date": day,
                "rank": 1,
            },
            "options:BBB:put:2026-09-02": {
                "id": "options:BBB:put:2026-09-02", "source": "options", "symbol": "BBB",
                "direction": "put", "entry": 50, "target": 45, "stop": 55, "date": day,
            },
        },
    }
    _seed_day(tmp_path, day, doc)

    backend = store.save_outcomes(
        day, {"options:AAA:call:2026-09-02": {"verdict": "WIN", "actual": 120}}, bucket=None
    )
    assert backend == "local"

    raw = store.load_report_raw(day, bucket=None)
    aaa = raw["plays"]["options:AAA:call:2026-09-02"]
    bbb = raw["plays"]["options:BBB:put:2026-09-02"]
    assert aaa["outcome"] == {"verdict": "WIN", "actual": 120}
    assert aaa["rank"] == 1 and aaa["target"] == 120  # untouched
    assert raw["fired"] == ["options:AAA:call:2026-09-02"]  # fired set preserved
    assert "outcome" not in bbb  # sibling untouched


def test_save_outcomes_skips_missing_day_and_unknown_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "LOCAL_DIR", tmp_path)
    # No document at all -> skipped.
    assert store.save_outcomes("2026-01-01", {"x": {"verdict": "WIN"}}, bucket=None) == "skipped"
    # Document exists but the id isn't in it -> unchanged (nothing to clobber).
    _seed_day(tmp_path, "2026-09-02", {"date": "2026-09-02", "plays": {}, "fired": []})
    assert store.save_outcomes("2026-09-02", {"nope": {"verdict": "WIN"}}, bucket=None) == "unchanged"


# --- engine end-to-end with a fake Massive client ----------------------------


@dataclass
class _FakeBar:
    timestamp_ms: int
    high: float
    low: float


class _FakeClient:
    """Canned daily bars per symbol; minute bars per (symbol, day)."""

    def __init__(self, daily=None, minute=None):
        self._daily = daily or {}
        self._minute = minute or {}

    def get_daily_bars(self, ticker, from_date, to_date, adjusted=True):
        return [_FakeBar(_ts(d), h, lo) for (d, h, lo) in self._daily.get(ticker.upper(), [])]

    def get_aggs_range(self, ticker, multiplier, timespan, from_date, to_date, adjusted=True, sort="asc", limit=5000):
        return [_FakeBar(_ts(day), h, lo) for (h, lo) in self._minute.get((ticker.upper(), from_date), [])]


def test_engine_settles_writes_and_skips_frozen(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "LOCAL_DIR", tmp_path)
    day = "2026-09-02"
    doc = {
        "date": day, "fired": [],
        "plays": {
            "options:WIN:call:2026-09-02": {
                "id": "options:WIN:call:2026-09-02", "source": "options", "symbol": "WIN",
                "direction": "call", "entry": 100, "target": 120, "stop": 90, "date": day,
            },
            "options:DONE:call:2026-09-02": {  # already final -> must be skipped
                "id": "options:DONE:call:2026-09-02", "source": "options", "symbol": "DONE",
                "direction": "call", "entry": 10, "target": 12, "stop": 9, "date": day,
                "outcome": {"verdict": "LOSS", "actual": 9},
            },
        },
    }
    _seed_day(tmp_path, day, doc)
    client = _FakeClient(daily={"WIN": [("2026-09-03", 105, 98), ("2026-09-04", 121, 101)]})

    result = run_settlement(today="2026-09-15", bucket=None, client=client)
    assert result.settled_win == 1
    assert result.skipped_frozen == 1
    assert result.days_processed == 1

    raw = store.load_report_raw(day, bucket=None)
    assert raw["plays"]["options:WIN:call:2026-09-02"]["outcome"]["verdict"] == "WIN"
    # the already-final play is untouched
    assert raw["plays"]["options:DONE:call:2026-09-02"]["outcome"] == {"verdict": "LOSS", "actual": 9}


def test_engine_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "LOCAL_DIR", tmp_path)
    day = "2026-09-02"
    doc = {
        "date": day, "fired": [],
        "plays": {
            "options:WIN:call:2026-09-02": {
                "id": "options:WIN:call:2026-09-02", "source": "options", "symbol": "WIN",
                "direction": "call", "entry": 100, "target": 120, "stop": 90, "date": day,
            }
        },
    }
    _seed_day(tmp_path, day, doc)
    client = _FakeClient(daily={"WIN": [("2026-09-03", 121, 101)]})

    first = run_settlement(today="2026-09-15", bucket=None, client=client)
    second = run_settlement(today="2026-09-15", bucket=None, client=client)
    assert first.settled_win == 1
    assert second.settled_win == 0 and second.skipped_frozen == 1  # frozen after first run


def test_engine_ignores_today_and_future_days(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "LOCAL_DIR", tmp_path)
    today = "2026-09-02"
    _seed_day(tmp_path, today, {
        "date": today, "fired": [],
        "plays": {"options:X:call:2026-09-02": {
            "id": "options:X:call:2026-09-02", "source": "options", "symbol": "X",
            "direction": "call", "entry": 100, "target": 120, "stop": 90, "date": today,
        }},
    })
    client = _FakeClient(daily={"X": [("2026-09-03", 130, 101)]})
    result = run_settlement(today=today, bucket=None, client=client)
    assert result.days_processed == 0  # today is not settle-eligible
    raw = store.load_report_raw(today, bucket=None)
    assert "outcome" not in raw["plays"]["options:X:call:2026-09-02"]
