"""The two Sector Scout lenses: Extremes (mean reversion) and Continuation
(trend persistence), plus the classification that drives structure choice.

Pure functions over price arrays -- no I/O, no network, no config mutation.
The live report and the historical base-rate replay both call these SAME
functions, which is what keeps the empirical base rate honest (the
indicator-parity discipline options_scout documents).

Calibration (validated 2026-09-04, not optional): with the index near highs
almost nothing clears the absolute price test -- 1 of 34 funds did, while 19
of 34 sat at RS percentile 30 or below. RELATIVE STRENGTH is therefore the
leading axis and absolute price percentile the secondary filter; the board
sorts by RS ascending and the report states which axis did the work.

Acceleration (the continuation measure that does the most work, implemented
exactly as validated): the 3-month return annualised (x4) minus the realised
12-month return. Positive means the trend is getting faster. On the
validation run this ranked XOP/XLE top at 7/8 and XBI last at 1/8 despite XBI
having the largest 12-month move -- its pace was no longer increasing.

ANALYSIS ONLY -- no order path anywhere in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..options_scout.indicators import rsi as rsi_series
from ..options_scout.indicators import sma as sma_series

# Classifications
COILED = "Coiled"
FALLING_KNIFE = "Falling knife"
EXTENDED = "Extended"
LEADING = "Leading and earning it"
MID_RANGE = "Mid range"

CLASSIFICATIONS = (COILED, FALLING_KNIFE, EXTENDED, LEADING, MID_RANGE)


def rank_percentile(history: list[float], current: float) -> float:
    """Rank-based percentile: the share of historical values strictly below
    `current`, in 0..100. Rank percentile, NOT range position."""
    if not history:
        return 50.0
    below = sum(1 for v in history if v < current)
    return 100.0 * below / len(history)


def pct_return(closes: list[float], periods_back: int) -> float | None:
    """Simple return over the last `periods_back` bars of `closes` (which end
    at 'now'). None when history is too short."""
    if len(closes) <= periods_back or periods_back <= 0:
        return None
    base = closes[-1 - periods_back]
    if base <= 0:
        return None
    return (closes[-1] / base) - 1.0


def acceleration(closes_daily: list[float]) -> float | None:
    """3-month return annualised (x4) minus the realised 12-month return.
    ~21 trading days/month: 3m = 63 bars, 12m = 252 bars."""
    r3 = pct_return(closes_daily, 63)
    r12 = pct_return(closes_daily, 252)
    if r3 is None or r12 is None:
        return None
    return (r3 * 4.0) - r12


@dataclass(frozen=True)
class ExtremesRead:
    """Lens one: where the fund sits in its own history."""

    price_pctile: float          # rank pct of current price vs monthly closes
    rs_pctile: float             # rank pct of fund/SPY ratio vs its history
    drawdown_from_high: float    # negative fraction, e.g. -0.18
    runup_from_low: float        # positive fraction
    pos_52w: float               # 0..1 position in the 52-week range
    ret_3m: float | None
    ret_6m: float | None
    ret_12m: float | None
    above_sma50: bool
    above_sma200: bool
    weekly_rsi: float | None
    stabilising: bool            # price above 50d SMA OR 3m return positive
    window_label: str            # e.g. "2.0y" -- the ACTUAL window used


@dataclass(frozen=True)
class ContinuationRead:
    """Lens two: three pass/fail gates then a score out of 8."""

    gate_momentum: bool          # 3m, 6m, 12m returns all positive
    gate_structure: bool         # above both 50d and 200d SMAs
    gate_no_exhaustion: bool     # weekly RSI < max AND <= max% above 200d
    gates_passed: int
    score: int                   # 0..8
    components: dict[str, int]   # each named component's earned points
    accel: float | None
    beat_spy_12m: bool | None
    pct_above_sma200: float | None


def extremes_read(
    *,
    closes_daily: list[float],
    highs_daily: list[float],
    lows_daily: list[float],
    closes_weekly_pct: list[float],
    spy_closes_weekly_pct: list[float],
    weekly_closes: list[float],
    window_label: str,
    cfg: dict[str, Any],
) -> ExtremesRead | None:
    """Compute lens one. PERCENTILES ARE ON WEEKLY BARS: two years of weekly
    closes is ~104 observations against ~24 monthly ones, four times the
    resolution at no extra cost (2026-09-07 fix). Every percentile this
    produces is a 2-YEAR percentile and must be labeled as such downstream --
    the window excludes the 2022 drawdown entirely, which the method section
    states as a plan limitation. Weekly arrays must be tail-aligned between
    the fund and SPY; the data layer guarantees that."""
    if len(closes_daily) < 60 or len(closes_weekly_pct) < 26:
        return None
    current = closes_daily[-1]

    price_pct = rank_percentile(closes_weekly_pct[:-1], current)

    n = min(len(closes_weekly_pct), len(spy_closes_weekly_pct))
    ratio = [
        closes_weekly_pct[-n + i] / spy_closes_weekly_pct[-n + i]
        for i in range(n)
        if spy_closes_weekly_pct[-n + i] > 0
    ]
    rs_pct = rank_percentile(ratio[:-1], ratio[-1]) if len(ratio) >= 26 else 50.0

    hi = max(highs_daily)
    lo = min(lows_daily)
    dd = (current / hi) - 1.0 if hi > 0 else 0.0
    ru = (current / lo) - 1.0 if lo > 0 else 0.0

    yr_hi = max(highs_daily[-252:])
    yr_lo = min(lows_daily[-252:])
    pos = (current - yr_lo) / (yr_hi - yr_lo) if yr_hi > yr_lo else 0.5

    sma50 = sma_series(closes_daily, 50)[-1]
    sma200 = sma_series(closes_daily, 200)[-1]
    above50 = sma50 is not None and current >= sma50
    above200 = sma200 is not None and current >= sma200

    wk_rsi_arr = rsi_series(weekly_closes, 14)
    weekly_rsi = wk_rsi_arr[-1] if wk_rsi_arr else None

    r3 = pct_return(closes_daily, 63)
    stab = above50 or (r3 is not None and r3 > 0)

    return ExtremesRead(
        price_pctile=round(price_pct, 1),
        rs_pctile=round(rs_pct, 1),
        drawdown_from_high=round(dd, 4),
        runup_from_low=round(ru, 4),
        pos_52w=round(pos, 3),
        ret_3m=r3,
        ret_6m=pct_return(closes_daily, 126),
        ret_12m=pct_return(closes_daily, 252),
        above_sma50=above50,
        above_sma200=above200,
        weekly_rsi=round(weekly_rsi, 1) if weekly_rsi is not None else None,
        stabilising=stab,
        window_label=window_label,
    )


def continuation_read(
    *,
    closes_daily: list[float],
    spy_closes_daily: list[float],
    weekly_closes: list[float],
    fund_pe: float | None,
    universe_median_pe: float | None,
    leader_earnings_improving: bool | None,
    cfg: dict[str, Any],
    rh_sma50: float | None = None,
    rh_sma200: float | None = None,
    rh_weekly_rsi: float | None = None,
) -> ContinuationRead | None:
    """Compute lens two: gates then the score out of 8. THE GATES ARE A HARD
    FILTER: the analyzer refuses a long candidacy to any fund failing one
    (2026-09-07 fix -- SMH ranked bullish while failing two of three).

    Gate inputs prefer Robinhood's server-side SMA/RSI when a snapshot
    carries them (operator direction: the gate reads a trustworthy number);
    the local computation is the fallback and the historical replay's only
    option. Score components come from config so weights are never hardcoded."""
    if len(closes_daily) < 260:
        return None
    cont = cfg.get("continuation", {}) or {}
    score_cfg = cont.get("score", {}) or {}
    current = closes_daily[-1]

    r3 = pct_return(closes_daily, 63)
    r6 = pct_return(closes_daily, 126)
    r12 = pct_return(closes_daily, 252)
    gate1 = all(r is not None and r > 0 for r in (r3, r6, r12))

    sma50 = rh_sma50 if rh_sma50 is not None else sma_series(closes_daily, 50)[-1]
    sma200 = rh_sma200 if rh_sma200 is not None else sma_series(closes_daily, 200)[-1]
    gate2 = (
        sma50 is not None and sma200 is not None
        and current >= sma50 and current >= sma200
    )

    if rh_weekly_rsi is not None:
        weekly_rsi = rh_weekly_rsi
    else:
        wk_rsi_arr = rsi_series(weekly_closes, 14)
        weekly_rsi = wk_rsi_arr[-1] if wk_rsi_arr else None
    pct_above = ((current / sma200) - 1.0) * 100.0 if sma200 else None
    rsi_max = float(cont.get("rsi_weekly_max", 75))
    above_max = float(cont.get("max_pct_above_sma200", 25))
    gate3 = (
        weekly_rsi is not None and weekly_rsi < rsi_max
        and pct_above is not None and pct_above <= above_max
    )

    accel = acceleration(closes_daily)
    spy_r12 = pct_return(spy_closes_daily, 252)
    beat_spy = (r12 > spy_r12) if (r12 is not None and spy_r12 is not None) else None

    components: dict[str, int] = {}
    components["beat_spy_12m"] = int(score_cfg.get("beat_spy_12m", 2)) if beat_spy else 0
    components["positive_acceleration"] = (
        int(score_cfg.get("positive_acceleration", 2)) if (accel is not None and accel > 0) else 0
    )
    pe_ok = (
        fund_pe is not None and universe_median_pe is not None
        and fund_pe <= universe_median_pe
    )
    components["pe_at_or_below_universe_median"] = (
        int(score_cfg.get("pe_at_or_below_universe_median", 2)) if pe_ok else 0
    )
    within = float(cont.get("within_sma200_pct", 15))
    components["within_15pct_of_sma200"] = (
        int(score_cfg.get("within_15pct_of_sma200", 1))
        if (pct_above is not None and pct_above < within)
        else 0
    )
    components["improving_leader_earnings"] = (
        int(score_cfg.get("improving_leader_earnings", 1)) if leader_earnings_improving else 0
    )

    return ContinuationRead(
        gate_momentum=gate1,
        gate_structure=gate2,
        gate_no_exhaustion=gate3,
        gates_passed=sum((gate1, gate2, gate3)),
        score=sum(components.values()),
        components=components,
        accel=accel,
        beat_spy_12m=beat_spy,
        pct_above_sma200=round(pct_above, 2) if pct_above is not None else None,
    )


def classify(
    extremes: ExtremesRead,
    *,
    valuation_rich: bool | None,
    valuation_cheap: bool | None,
    earnings_growing: bool | None,
    cfg: dict[str, Any],
) -> str:
    """Map the extremes read (plus the valuation/earnings overlay) onto one of
    the five classifications. Relative strength leads (calibration fix); the
    absolute price percentile is the secondary filter."""
    ext = cfg.get("extremes", {}) or {}
    lo_p = float(ext.get("coiled_price_pctile_max", 25))
    lo_rs = float(ext.get("coiled_rs_pctile_max", 30))
    hi_p = float(ext.get("extended_price_pctile_min", 85))
    hi_rs = float(ext.get("extended_rs_pctile_min", 80))
    rs_leads = bool(ext.get("rs_is_leading_axis", True))

    # Depth test: RS leads, price percentile confirms (calibration fix).
    if rs_leads:
        deep = extremes.rs_pctile <= lo_rs and extremes.price_pctile <= max(lo_p, 50.0)
        high = extremes.rs_pctile >= hi_rs and extremes.price_pctile >= min(hi_p, 60.0)
    else:
        deep = extremes.price_pctile <= lo_p and extremes.rs_pctile <= lo_rs
        high = extremes.price_pctile >= hi_p and extremes.rs_pctile >= hi_rs

    if deep:
        # Not-expensive is the bar (unknown counts as not-rich): a fairly
        # valued, stabilising deep fund IS Coiled. Testing valuation_cheap
        # here silently reclassified fairly-valued recoveries as Falling
        # knife and dropped their structures (review finding, 2026-09-04).
        cheap_enough = valuation_rich is not True
        if extremes.stabilising and cheap_enough:
            return COILED
        return FALLING_KNIFE
    if high:
        in_line = valuation_rich is not True
        if in_line and earnings_growing:
            return LEADING
        return EXTENDED
    return MID_RANGE
