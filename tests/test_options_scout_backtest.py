from __future__ import annotations

import math

from src.options_scout.backtest import backtest_setup
from src.options_scout.indicators import build_series

IND_CFG = {
    "ema_fast": 20, "ema_slow": 50, "sma_long": 200,
    "rsi_window": 14, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
    "atr_window": 14,
}
WEIGHTS = {"trend": 0.4, "momentum": 0.3, "rsi": 0.3}


def _series(closes):
    highs = [c * 1.008 for c in closes]
    lows = [c * 0.992 for c in closes]
    return build_series(highs, lows, closes, IND_CFG)


def uptrend(n=300, drift=0.003):
    return [100.0 * (1.0 + drift) ** i + 0.5 * math.sin(i) for i in range(n)]


def flat(n=300):
    return [100.0 + 0.05 * math.sin(i) for i in range(n)]


def test_uptrend_call_setup_has_occurrences_and_a_bounded_hit_rate():
    hit = backtest_setup(_series(uptrend()), "call", horizon_days=10, vol_lookback=20,
                         weights=WEIGHTS, min_occurrences=10)
    assert hit.direction == "call"
    assert hit.occurrences > 0
    assert 0 <= hit.hits <= hit.occurrences
    assert 0.0 <= hit.hit_rate <= 1.0
    # A steady uptrend reaches a vol-scaled upside target often.
    assert hit.hit_rate > 0.5


def test_sample_size_drives_the_low_confidence_flag():
    series = _series(uptrend())
    generous = backtest_setup(series, "call", 10, 20, WEIGHTS, min_occurrences=1)
    strict = backtest_setup(series, "call", 10, 20, WEIGHTS, min_occurrences=10_000)
    assert generous.occurrences == strict.occurrences  # same replay
    assert generous.low_confidence is False
    assert strict.low_confidence is True  # same data, higher bar -> flagged
    # Confidence weight shrinks the hit-rate when the sample is small vs the bar.
    assert strict.confidence_weight < generous.confidence_weight


def test_flat_series_produces_no_occurrences():
    hit = backtest_setup(_series(flat()), "call", 10, 20, WEIGHTS, min_occurrences=10)
    assert hit.occurrences == 0
    assert hit.hit_rate == 0.0
    assert hit.low_confidence is True  # zero occurrences is the weakest possible
    assert hit.confidence_weight == 0.0


def test_fresh_edge_only_never_inflates_occurrences():
    series = _series(uptrend())
    fresh = backtest_setup(series, "call", 10, 20, WEIGHTS, 10, fresh_edge_only=True)
    every_day = backtest_setup(series, "call", 10, 20, WEIGHTS, 10, fresh_edge_only=False)
    # Counting only rising edges yields no MORE occurrences than counting every day.
    assert fresh.occurrences <= every_day.occurrences
    assert fresh.occurrences > 0


def test_summary_carries_the_sample_size_and_never_claims_certainty():
    hit = backtest_setup(_series(uptrend()), "call", 10, 20, WEIGHTS, min_occurrences=10)
    summary = hit.summary()
    assert "hit rate" in summary
    assert str(hit.occurrences) in summary
    for banned in ("guarantee", "certain", "sure thing"):
        assert banned not in summary.lower()
