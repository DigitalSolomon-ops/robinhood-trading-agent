"""Settlement replay: an open six-month call is graded correctly when its
falsifier level is breached in a replayed historical scenario, when its
target is reached, and at the roll-or-close deadline."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.equity_intelligence.massive_client import Bar
from src.sector_scout.settlement import append_new_calls, evaluate_open_calls, record_call
from src.sector_scout.state import StateStore


def _bar(day: date, high: float, low: float, close: float) -> Bar:
    ts = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)
    return Bar(timestamp_ms=ts, open=close, high=high, low=low, close=close, volume=1.0)


class FakeBarsClient:
    """Read-only fake: get_daily_bars only. No order methods exist at all."""

    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars

    def get_daily_bars(self, symbol: str, from_date: str, to_date: str) -> list[Bar]:
        return [
            b
            for b in self._bars
            if from_date <= datetime.fromtimestamp(b.timestamp_ms / 1000, timezone.utc).date().isoformat() <= to_date
        ]


def _play(fund: str = "XLE") -> dict:
    return {
        "fund": fund,
        "direction": "bullish",
        "spot": 64.0,
        "win_level": 80.0,
        "falsifier_level": 57.0,
        "prob_profit_bs": 0.35,
        "prob_profit_empirical": 0.5,
        "factor_values": {"iv_rank": 25.0},
        "ticket": {
            "structure": "long call debit spread",
            "limit_price": 2.65,
            "breakeven": 72.65,
            "roll_or_close_date": "2026-12-01",
            "legs": [{"expiry": "2027-01-15"}],
        },
    }


def _store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state", bucket_name=None, gcs_prefix="sector-scout")


def test_record_and_idempotent_append(tmp_path: Path) -> None:
    store = _store(tmp_path)
    run = date(2026, 9, 4)
    assert append_new_calls(store, [_play()], run) == 1
    assert append_new_calls(store, [_play()], run) == 0  # same day re-run: no dupe
    calls = store.load_open_calls()
    assert len(calls) == 1
    assert calls[0]["id"] == "sector:XLE:call:2026-09-04"
    assert calls[0]["outcome"]["verdict"] == "OPEN"


def test_no_structure_play_records_nothing(tmp_path: Path) -> None:
    play = _play()
    play.pop("ticket")
    assert record_call(play, date(2026, 9, 4)) is None


def test_falsifier_breach_grades_loss(tmp_path: Path) -> None:
    store = _store(tmp_path)
    append_new_calls(store, [_play()], date(2026, 9, 4))
    bars = [
        _bar(date(2026, 9, 8), 66.0, 62.0, 65.0),
        _bar(date(2026, 9, 9), 60.0, 56.5, 57.5),   # low 56.5 <= falsifier 57
    ]
    summary = evaluate_open_calls(store, FakeBarsClient(bars), date(2026, 9, 10))
    assert summary.settled_loss == 1 and summary.settled_win == 0
    outcome = store.load_open_calls()[0]["outcome"]
    assert outcome["verdict"] == "LOSS"
    assert outcome["hit_date"] == "2026-09-09"


def test_target_touch_grades_win(tmp_path: Path) -> None:
    store = _store(tmp_path)
    append_new_calls(store, [_play()], date(2026, 9, 4))
    bars = [
        _bar(date(2026, 9, 8), 70.0, 63.0, 69.0),
        _bar(date(2026, 9, 9), 81.0, 68.0, 80.5),   # high 81 >= win level 80
    ]
    summary = evaluate_open_calls(store, FakeBarsClient(bars), date(2026, 9, 10))
    assert summary.settled_win == 1
    assert store.load_open_calls()[0]["outcome"]["verdict"] == "WIN"


def test_deadline_grades_vs_breakeven(tmp_path: Path) -> None:
    store = _store(tmp_path)
    append_new_calls(store, [_play()], date(2026, 9, 4))
    # Never touches 80 or 57; sits at 75 (> breakeven 72.65) at the deadline.
    bars = [_bar(date(2026, 11, 30), 76.0, 74.0, 75.0)]
    summary = evaluate_open_calls(store, FakeBarsClient(bars), date(2026, 12, 2))
    assert summary.settled_win == 1
    outcome = store.load_open_calls()[0]["outcome"]
    assert outcome["metric"] == "breakeven_at_deadline"


def test_frozen_verdicts_stay_frozen(tmp_path: Path) -> None:
    store = _store(tmp_path)
    append_new_calls(store, [_play()], date(2026, 9, 4))
    bars = [_bar(date(2026, 9, 9), 60.0, 56.5, 57.5)]
    evaluate_open_calls(store, FakeBarsClient(bars), date(2026, 9, 10))
    # A later run with bars that WOULD win must not flip the frozen LOSS.
    later = [_bar(date(2026, 9, 11), 81.0, 70.0, 80.5)]
    summary = evaluate_open_calls(store, FakeBarsClient(bars + later), date(2026, 9, 12))
    assert summary.settled_loss == 1 and summary.settled_win == 0
