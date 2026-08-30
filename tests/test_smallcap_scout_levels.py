from __future__ import annotations

from src.equity_intelligence.massive_client import Bar
from src.smallcap_scout.levels import compute_levels

CFG = {"horizon_days": 5, "atr_window": 14, "atr_target_mult": 2.0,
       "stop_atr_mult": 1.5, "support_lookback": 10}


def _bar(close: float, high: float | None = None, low: float | None = None) -> Bar:
    return Bar(
        timestamp_ms=0,
        open=close,
        high=high if high is not None else close * 1.02,
        low=low if low is not None else close * 0.98,
        close=close,
        volume=1000.0,
        vwap=close,
        transactions=10,
        ticker="X",
    )


def test_compute_levels_frames_a_long_setup():
    bars = [_bar(c) for c in [4.0, 4.2, 4.5, 4.8, 5.0, 5.4, 6.0]]
    levels = compute_levels(bars, CFG)
    assert levels is not None
    assert levels.entry == 6.0                       # entry = last close
    assert levels.stop < levels.entry < levels.target
    assert levels.atr > 0
    assert levels.reward_risk > 0


def test_breakout_ref_is_the_prior_window_high():
    bars = [_bar(5.0, high=5.5), _bar(5.2, high=6.1), _bar(6.0, high=6.2)]
    levels = compute_levels(bars, CFG)
    assert levels is not None
    # breakout_ref excludes the scan day's own high (6.2); the prior high is 6.1.
    assert levels.breakout_ref == 6.1


def test_stop_is_bounded_by_atr_even_with_a_far_support():
    # A deep early low would put naive support far below entry; the ATR clamp
    # keeps the stop within atr * stop_atr_mult of entry.
    bars = [_bar(6.0, high=6.1, low=2.0)] + [_bar(c) for c in [5.8, 5.9, 6.0, 6.1, 6.0]]
    levels = compute_levels(bars, CFG)
    assert levels is not None
    assert levels.stop < levels.entry
    assert levels.entry - levels.stop <= levels.atr * CFG["stop_atr_mult"] + 1e-6


def test_too_few_bars_returns_none():
    assert compute_levels([_bar(5.0), _bar(5.1)], CFG) is None
