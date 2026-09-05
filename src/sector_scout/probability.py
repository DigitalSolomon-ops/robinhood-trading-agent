"""Black-Scholes probability of profit and expected value for debit/credit
vertical spreads. Pure stdlib math -- no scipy, no numpy, no I/O.

The probability is computed for the SPREAD, not per leg: the chance the
underlying finishes past the structure's breakeven at expiry under a lognormal
terminal distribution parameterized by the legs' LIVE implied volatility.
Expected value integrates the intermediate region in closed form (undiscounted
Black-Scholes expectations), so EV is exact under the model rather than the
two-point approximation.

Both numbers ALWAYS travel with the empirical base rate elsewhere in the
report -- see base_rate.py -- and a divergence above the configured threshold
is called out as a finding, not smoothed over.

ANALYSIS ONLY -- no order path anywhere in this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

SQRT2 = math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erf (double precision, no tables)."""
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def _d2(spot: float, level: float, sigma: float, t_years: float, rate: float) -> float:
    """d2 for P(S_T > level) = N(d2) under risk-neutral lognormal dynamics."""
    if spot <= 0 or level <= 0 or sigma <= 0 or t_years <= 0:
        raise ValueError("spot, level, sigma and t must be positive")
    num = math.log(spot / level) + (rate - 0.5 * sigma * sigma) * t_years
    return num / (sigma * math.sqrt(t_years))


def prob_finish_above(spot: float, level: float, sigma: float, t_years: float, rate: float) -> float:
    """P(S_T > level) at expiry."""
    return norm_cdf(_d2(spot, level, sigma, t_years, rate))


def expected_terminal_call_value(
    spot: float, strike: float, sigma: float, t_years: float, rate: float
) -> float:
    """E[(S_T - K)^+], UNDISCOUNTED (we compare to the debit paid today over a
    horizon where discounting is a second-order effect; the rate still shapes
    the terminal distribution's drift)."""
    d2 = _d2(spot, strike, sigma, t_years, rate)
    d1 = d2 + sigma * math.sqrt(t_years)
    forward = spot * math.exp(rate * t_years)
    return forward * norm_cdf(d1) - strike * norm_cdf(d2)


def blended_sigma(
    long_iv: float | None, short_iv: float | None, long_strike: float, short_strike: float,
    breakeven: float,
) -> float | None:
    """Sigma at the breakeven level, linearly interpolated between the two
    legs' live IVs by strike (the closest honest read of the smile with two
    points). Falls back to whichever leg has an IV; None when neither does --
    the caller reports n/a rather than fabricating a probability."""
    if long_iv is not None and short_iv is not None and short_strike != long_strike:
        frac = (breakeven - long_strike) / (short_strike - long_strike)
        frac = max(0.0, min(1.0, frac))
        return long_iv + frac * (short_iv - long_iv)
    return long_iv if long_iv is not None else short_iv


@dataclass(frozen=True)
class SpreadProbability:
    """The model-based numbers for one vertical spread."""

    prob_profit: float          # P(underlying finishes past breakeven)
    prob_max_gain: float        # P(finishes past the far strike)
    prob_max_loss: float        # P(finishes past the near strike, wrong way)
    expected_value: float       # dollars per spread (x100 multiplier applied)
    max_gain: float             # dollars per spread
    max_loss: float             # dollars per spread
    sigma_used: float
    t_years: float

    @property
    def reward_to_risk(self) -> float | None:
        return round(self.max_gain / self.max_loss, 2) if self.max_loss > 0 else None


def debit_call_spread(
    *,
    spot: float,
    long_strike: float,
    short_strike: float,
    debit: float,
    long_iv: float | None,
    short_iv: float | None,
    dte_calendar_days: int,
    rate: float = 0.04,
    multiplier: float = 100.0,
) -> SpreadProbability | None:
    """Long call at long_strike, short call at short_strike (> long), net debit
    per share. Returns None when no leg IV is available -- n/a, never invented."""
    if short_strike <= long_strike or debit <= 0 or spot <= 0 or dte_calendar_days <= 0:
        return None
    if debit >= (short_strike - long_strike):
        return None  # paying >= the width: max gain <= 0, not a structure
    breakeven = long_strike + debit
    sigma = blended_sigma(long_iv, short_iv, long_strike, short_strike, breakeven)
    if sigma is None or sigma <= 0:
        return None
    t = dte_calendar_days / 365.0

    p_profit = prob_finish_above(spot, breakeven, sigma, t, rate)
    p_max_gain = prob_finish_above(spot, short_strike, sigma, t, rate)
    p_max_loss = 1.0 - prob_finish_above(spot, long_strike, sigma, t, rate)

    # E[payoff] = E[(S-K1)+] - E[(S-K2)+]; EV = (E[payoff] - debit) per share.
    ev_share = (
        expected_terminal_call_value(spot, long_strike, sigma, t, rate)
        - expected_terminal_call_value(spot, short_strike, sigma, t, rate)
        - debit
    )
    width = short_strike - long_strike
    return SpreadProbability(
        prob_profit=p_profit,
        prob_max_gain=p_max_gain,
        prob_max_loss=p_max_loss,
        expected_value=round(ev_share * multiplier, 2),
        max_gain=round((width - debit) * multiplier, 2),
        max_loss=round(debit * multiplier, 2),
        sigma_used=sigma,
        t_years=t,
    )


def debit_put_spread(
    *,
    spot: float,
    long_strike: float,
    short_strike: float,
    debit: float,
    long_iv: float | None,
    short_iv: float | None,
    dte_calendar_days: int,
    rate: float = 0.04,
    multiplier: float = 100.0,
) -> SpreadProbability | None:
    """Long put at long_strike, short put at short_strike (< long), net debit
    per share. Profit when the underlying finishes BELOW breakeven."""
    if short_strike >= long_strike or debit <= 0 or spot <= 0 or dte_calendar_days <= 0:
        return None
    if debit >= (long_strike - short_strike):
        return None  # paying >= the width: max gain <= 0, not a structure
    breakeven = long_strike - debit
    sigma = blended_sigma(long_iv, short_iv, long_strike, short_strike, breakeven)
    if sigma is None or sigma <= 0:
        return None
    t = dte_calendar_days / 365.0

    p_profit = 1.0 - prob_finish_above(spot, breakeven, sigma, t, rate)
    p_max_gain = 1.0 - prob_finish_above(spot, short_strike, sigma, t, rate)
    p_max_loss = prob_finish_above(spot, long_strike, sigma, t, rate)

    # Put values by parity of the undiscounted expectations:
    # E[(K-S)+] = K - F + E[(S-K)+], with F = spot*e^{rt}.
    forward = spot * math.exp(rate * (dte_calendar_days / 365.0))
    e_long = long_strike - forward + expected_terminal_call_value(spot, long_strike, sigma, t, rate)
    e_short = short_strike - forward + expected_terminal_call_value(spot, short_strike, sigma, t, rate)
    ev_share = e_long - e_short - debit

    width = long_strike - short_strike
    return SpreadProbability(
        prob_profit=p_profit,
        prob_max_gain=p_max_gain,
        prob_max_loss=p_max_loss,
        expected_value=round(ev_share * multiplier, 2),
        max_gain=round((width - debit) * multiplier, 2),
        max_loss=round(debit * multiplier, 2),
        sigma_used=sigma,
        t_years=t,
    )


def credit_vertical(
    *,
    spot: float,
    short_strike: float,
    long_strike: float,
    credit: float,
    short_iv: float | None,
    long_iv: float | None,
    dte_calendar_days: int,
    direction: str,
    rate: float = 0.04,
    multiplier: float = 100.0,
) -> SpreadProbability | None:
    """Credit vertical expressing the same directional view when IV rank says
    sell premium: bullish -> short put spread (short higher put, long lower
    put); bearish -> short call spread. `credit` is net premium received per
    share. Max gain = credit; max loss = width - credit."""
    if credit <= 0 or spot <= 0 or dte_calendar_days <= 0:
        return None
    t = dte_calendar_days / 365.0
    width = abs(short_strike - long_strike)
    if width <= 0 or credit >= width:
        return None

    if direction == "bullish":
        # Short put spread: keep full credit above short_strike; breakeven at
        # short_strike - credit; max loss below long_strike (< short_strike).
        if not long_strike < short_strike:
            return None
        breakeven = short_strike - credit
        sigma = blended_sigma(short_iv, long_iv, short_strike, long_strike, breakeven)
        if sigma is None or sigma <= 0:
            return None
        p_profit = prob_finish_above(spot, breakeven, sigma, t, rate)
        p_max_gain = prob_finish_above(spot, short_strike, sigma, t, rate)
        p_max_loss = 1.0 - prob_finish_above(spot, long_strike, sigma, t, rate)
        forward = spot * math.exp(rate * t)
        e_short = short_strike - forward + expected_terminal_call_value(spot, short_strike, sigma, t, rate)
        e_long = long_strike - forward + expected_terminal_call_value(spot, long_strike, sigma, t, rate)
        ev_share = credit - (e_short - e_long)
    elif direction == "bearish":
        # Short call spread: keep full credit below short_strike; breakeven at
        # short_strike + credit; max loss above long_strike (> short_strike).
        if not long_strike > short_strike:
            return None
        breakeven = short_strike + credit
        sigma = blended_sigma(short_iv, long_iv, short_strike, long_strike, breakeven)
        if sigma is None or sigma <= 0:
            return None
        p_profit = 1.0 - prob_finish_above(spot, breakeven, sigma, t, rate)
        p_max_gain = 1.0 - prob_finish_above(spot, short_strike, sigma, t, rate)
        p_max_loss = prob_finish_above(spot, long_strike, sigma, t, rate)
        ev_share = credit - (
            expected_terminal_call_value(spot, short_strike, sigma, t, rate)
            - expected_terminal_call_value(spot, long_strike, sigma, t, rate)
        )
    else:
        return None

    return SpreadProbability(
        prob_profit=p_profit,
        prob_max_gain=p_max_gain,
        prob_max_loss=p_max_loss,
        expected_value=round(ev_share * multiplier, 2),
        max_gain=round(credit * multiplier, 2),
        max_loss=round((width - credit) * multiplier, 2),
        sigma_used=sigma,
        t_years=t,
    )
