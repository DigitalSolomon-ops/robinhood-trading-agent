"""Walk-forward weight proposer.

ANALYSIS ONLY -- asserts the proposal math + validation; it never writes config.
"""

from __future__ import annotations

from datetime import date, timedelta

from src.scout_backtest.tuning import (
    combined_aligned_score,
    discriminator_gap,
    factor_discrimination,
    propose_weights,
)


def _make_records():
    """40 sequential-date call plays where TREND is the true signal and MOMENTUM
    actively misleads: wins have high trend + negative momentum, losses the
    reverse."""
    d0 = date(2025, 1, 1)
    recs = []
    for i in range(40):
        win = i % 2 == 0
        recs.append({
            "date": (d0 + timedelta(days=i)).isoformat(),
            "direction": "call",
            "factors": {"trend": 0.8 if win else -0.3, "momentum": -0.5 if win else 0.5, "rsi": 0.0},
            "outcome": {"verdict": "WIN" if win else "LOSS"},
        })
    return recs


def test_factor_discrimination_signs():
    recs = _make_records()
    assert factor_discrimination(recs, "trend") > 0.5   # trend separates wins from losses
    assert factor_discrimination(recs, "momentum") < -0.5  # momentum misleads
    assert abs(factor_discrimination(recs, "rsi")) < 1e-9  # rsi is neutral


def test_discriminator_gap_reflects_weighting():
    recs = _make_records()
    trend_heavy = {"trend": 0.9, "momentum": 0.05, "rsi": 0.05}
    momentum_heavy = {"trend": 0.05, "momentum": 0.9, "rsi": 0.05}
    assert discriminator_gap(recs, trend_heavy) > discriminator_gap(recs, momentum_heavy)


def test_propose_shifts_weight_toward_the_predictive_factor():
    """Direction + sum-preservation hold at the conservative default bound."""
    recs = _make_records()
    current = {"trend": 0.2, "momentum": 0.6, "rsi": 0.2}  # over-weights misleading momentum
    result = propose_weights(recs, current, bound=0.35, train_frac=0.7)
    prop = result["proposed"]
    assert prop is not None
    assert prop["trend"] > current["trend"]        # move toward the real signal
    assert prop["momentum"] < current["momentum"]  # away from the misleading one
    assert abs(sum(prop.values()) - sum(current.values())) < 1e-6  # sum preserved
    # validation fields are always computed
    assert "current_holdout_gap" in result["validation"]
    assert "proposed_holdout_gap" in result["validation"]


def test_wide_enough_shift_validates_as_improvement():
    """With a bound wide enough to overcome a badly-wrong config, the holdout
    validation detects the improvement and recommends applying."""
    recs = _make_records()
    current = {"trend": 0.2, "momentum": 0.6, "rsi": 0.2}
    result = propose_weights(recs, current, bound=1.0, train_frac=0.7)
    assert result["validation"]["improved"] is True
    assert result["recommendation"].startswith("apply")


def test_insufficient_data_declines_to_propose():
    recs = _make_records()[:10]
    result = propose_weights(recs, {"trend": 0.4, "momentum": 0.3, "rsi": 0.3})
    assert result["proposed"] is None
    assert result["recommendation"] == "insufficient data"


def test_combined_aligned_score_none_without_factors():
    assert combined_aligned_score({"direction": "call", "outcome": {"verdict": "WIN"}}, {"trend": 1.0}) is None
