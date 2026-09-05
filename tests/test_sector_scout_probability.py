"""Black-Scholes spread probability and EV sanity for the Sector Scout."""

from __future__ import annotations

from src.sector_scout.probability import (
    credit_vertical,
    debit_call_spread,
    debit_put_spread,
    norm_cdf,
    prob_finish_above,
)


def test_norm_cdf_anchors() -> None:
    assert abs(norm_cdf(0.0) - 0.5) < 1e-12
    assert norm_cdf(3.0) > 0.99
    assert norm_cdf(-3.0) < 0.01


def test_prob_monotonic_in_level() -> None:
    lo = prob_finish_above(100.0, 90.0, 0.25, 0.5, 0.04)
    hi = prob_finish_above(100.0, 120.0, 0.25, 0.5, 0.04)
    assert lo > hi


def test_debit_call_spread_shape() -> None:
    sp = debit_call_spread(
        spot=100.0, long_strike=100.0, short_strike=110.0, debit=3.0,
        long_iv=0.25, short_iv=0.24, dte_calendar_days=180, rate=0.04,
    )
    assert sp is not None
    assert sp.max_loss == 300.0
    assert sp.max_gain == 700.0
    assert 0.0 < sp.prob_max_gain < sp.prob_profit < 1.0
    assert sp.reward_to_risk == round(700.0 / 300.0, 2)
    # EV must sit inside the payoff bounds.
    assert -sp.max_loss <= sp.expected_value <= sp.max_gain


def test_debit_put_spread_mirrors() -> None:
    sp = debit_put_spread(
        spot=100.0, long_strike=100.0, short_strike=90.0, debit=3.0,
        long_iv=0.25, short_iv=0.26, dte_calendar_days=180, rate=0.04,
    )
    assert sp is not None
    assert sp.max_loss == 300.0
    assert sp.max_gain == 700.0
    assert 0.0 < sp.prob_max_gain < sp.prob_profit < 1.0


def test_credit_bullish_put_spread() -> None:
    sp = credit_vertical(
        spot=100.0, short_strike=95.0, long_strike=85.0, credit=2.0,
        short_iv=0.30, long_iv=0.32, dte_calendar_days=180,
        direction="bullish", rate=0.04,
    )
    assert sp is not None
    assert sp.max_gain == 200.0
    assert sp.max_loss == 800.0
    # Selling a below-spot put spread wins more often than not.
    assert sp.prob_profit > 0.5


def test_missing_iv_returns_none_not_fabricated() -> None:
    sp = debit_call_spread(
        spot=100.0, long_strike=100.0, short_strike=110.0, debit=3.0,
        long_iv=None, short_iv=None, dte_calendar_days=180,
    )
    assert sp is None


def test_degenerate_inputs_rejected() -> None:
    assert debit_call_spread(
        spot=100.0, long_strike=110.0, short_strike=100.0, debit=3.0,
        long_iv=0.2, short_iv=0.2, dte_calendar_days=180,
    ) is None
    assert credit_vertical(
        spot=100.0, short_strike=95.0, long_strike=85.0, credit=11.0,
        short_iv=0.3, long_iv=0.3, dte_calendar_days=180, direction="bullish",
    ) is None
