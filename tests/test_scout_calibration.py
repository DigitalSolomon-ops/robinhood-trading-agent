"""Scout calibration: predicted-vs-realized aggregation + the predictive-feature
round-trip that feeds it.

ANALYSIS ONLY -- these assert aggregation math and field persistence; nothing
here trades or tunes weights.
"""

from __future__ import annotations

from dataclasses import dataclass

import src.entry_alerts.store as store
from src.scout_calibration.calibration import (
    DRIFT_THRESHOLD,
    compute_calibration,
)


def _play(conviction=None, predicted=None, verdict=None, ret=None, source="options"):
    rec = {"source": source, "symbol": "X", "direction": "call",
           "entry": 100, "target": 120, "stop": 90}
    if conviction is not None:
        rec["conviction"] = conviction
    if predicted is not None:
        rec["predicted_hit_rate"] = predicted
    if verdict is not None:
        out = {"verdict": verdict}
        if ret is not None:
            out["return_pct"] = ret
        rec["outcome"] = out
    return rec


# --- compute_calibration -----------------------------------------------------


def test_empty_has_no_settled():
    report = compute_calibration([])
    assert report["settled"] == 0
    assert report["realized_win_rate"] is None
    assert report["tiers"] and all(t["n"] == 0 for t in report["tiers"])


def test_basic_win_rate_and_open_count():
    records = [
        _play(85, 0.7, "WIN", 20),
        _play(82, 0.65, "WIN", 18),
        _play(70, 0.6, "LOSS", -10),
        _play(50, 0.5, "LOSS", -8),
        _play(30, 0.4, "OPEN"),      # not settled
        _play(90, None, None),       # no outcome -> not settled
    ]
    r = compute_calibration(records)
    assert r["settled"] == 4
    assert r["wins"] == 2 and r["losses"] == 2
    assert r["realized_win_rate"] == 0.5
    assert r["open"] == 1
    assert abs(r["avg_predicted_hit_rate"] - 0.6125) < 1e-9
    assert abs(r["predicted_vs_realized_drift"] - (0.5 - 0.6125)) < 1e-9
    # tiers: very-high has the 2 wins
    vh = [t for t in r["tiers"] if t["lo"] == 80.0][0]
    assert vh["n"] == 2 and vh["wins"] == 2 and vh["win_rate"] == 1.0


def test_by_source_split():
    records = [
        _play(80, 0.7, "WIN", source="options"),
        _play(80, 0.7, "LOSS", source="options"),
        _play(None, None, "WIN", source="smallcap"),
    ]
    r = compute_calibration(records)
    assert r["by_source"]["options"] == {"n": 2, "wins": 1, "win_rate": 0.5}
    assert r["by_source"]["smallcap"] == {"n": 1, "wins": 1, "win_rate": 1.0}


def test_monotonic_true_when_conviction_ranks_correctly():
    records = (
        [_play(20, 0.5, "LOSS") for _ in range(3)]     # low tier: 0% win
        + [_play(90, 0.5, "WIN") for _ in range(3)]    # very-high tier: 100% win
    )
    r = compute_calibration(records)
    assert r["conviction_monotonic"] is True


def test_monotonic_false_when_inverted():
    records = (
        [_play(20, 0.5, "WIN") for _ in range(3)]      # low tier wins
        + [_play(90, 0.5, "LOSS") for _ in range(3)]   # very-high tier loses
    )
    r = compute_calibration(records)
    assert r["conviction_monotonic"] is False


def test_drift_flag_trips_with_enough_sample_and_gap():
    # 10 settled, all predicted 0.85 but only 40% realized -> drift ~ -0.45
    records = [_play(75, 0.85, "WIN" if i < 4 else "LOSS") for i in range(10)]
    r = compute_calibration(records)
    assert r["n_with_prediction"] == 10
    assert abs(r["predicted_vs_realized_drift"]) > DRIFT_THRESHOLD
    assert r["drift_flag"] is True


def test_no_drift_flag_when_sample_too_small():
    records = [_play(75, 0.9, "LOSS"), _play(75, 0.9, "LOSS")]  # big gap but n=2
    r = compute_calibration(records)
    assert r["drift_flag"] is False


# --- predictive-feature persistence round-trip -------------------------------


def test_playrecord_features_survive_daystate_roundtrip():
    rec = store.PlayRecord(
        id="options:AAA:call:2026-09-02", source="options", symbol="AAA",
        direction="call", entry=100, target=120, stop=90, date="2026-09-02",
        conviction=72.5, predicted_hit_rate=0.6, hit_rate_occurrences=15, rank=3,
    )
    state = store.DayState(date="2026-09-02", plays={rec.id: rec}, fired=set())
    reloaded = store.DayState.from_json(state.to_json(), "2026-09-02")
    got = reloaded.plays[rec.id]
    # These must round-trip, else a same-day mark_fired re-save would wipe them.
    assert got.conviction == 72.5
    assert got.predicted_hit_rate == 0.6
    assert got.hit_rate_occurrences == 15
    assert got.rank == 3


def test_record_from_options_play_captures_forecast():
    @dataclass
    class _HitRate:
        hit_rate: float
        occurrences: int

    @dataclass
    class _Play:
        symbol: str
        direction: str
        entry: float
        target: float
        stop: float
        conviction: float
        hit_rate: _HitRate

    play = _Play("NVDA", "call", 220.0, 240.0, 210.0, 88.0, _HitRate(0.62, 21))
    rec = store.record_from_options_play(play, "2026-09-02", rank=1)
    assert rec is not None
    assert rec.conviction == 88.0
    assert rec.predicted_hit_rate == 0.62
    assert rec.hit_rate_occurrences == 21
    assert rec.rank == 1


def test_record_from_options_play_tolerates_missing_forecast():
    @dataclass
    class _Bare:
        symbol: str
        direction: str
        entry: float
        target: float
        stop: float

    rec = store.record_from_options_play(_Bare("AAPL", "put", 300, 285, 315), "2026-09-02")
    assert rec is not None
    assert rec.conviction is None and rec.predicted_hit_rate is None and rec.rank is None
