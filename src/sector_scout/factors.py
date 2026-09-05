"""The Sector Scout selection factors. Pure math over pre-fetched data.

Priority order (the brief's, cut from the bottom if ever cut):
  1 breadth  2 IV rank  3 empirical base rate (base_rate.py)  4 correlation
  guard  5 rate beta  6 short interest  7 earnings revisions (trailing
  fallback)  8 term structure & skew  9 seasonality

FACTOR DISCIPLINE: a factor carries scoring weight ONLY once the backtest
shows it improves the hit rate on a sample above min_occurrences; until then
it ships as reported context with zero weight. Which factors have earned
weight lives in config (factor_weights_earned) and is recorded in
docs/sector-scout.md. Nothing in this module silently promotes itself.

ANALYSIS ONLY -- no order path anywhere in this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..options_scout.indicators import sma as sma_series


# --- 1. segment breadth ---------------------------------------------------------


@dataclass(frozen=True)
class BreadthRead:
    pct_above_200d: float | None       # share of constituents above their own 200d SMA
    median_pos_minus_fund: float | None  # median constituent 52w pos minus fund's own
    constituents_used: int
    coverage: float                    # cache coverage 0..1
    demoted: bool                      # True when breadth demotes Extended/Continuation

    def label(self) -> str:
        if self.pct_above_200d is None:
            return f"n/a (breadth cache at {self.coverage * 100:.0f}% coverage)"
        return (
            f"{self.pct_above_200d * 100:.0f}% of constituents above their 200 day average"
            f" ({self.constituents_used} names, cache {self.coverage * 100:.0f}%)"
        )


def breadth_read(
    constituent_series: dict[str, list[tuple[date, float]]],
    seeds: list[str],
    fund_pos_52w: float,
    *,
    coverage: float,
    classification: str,
    demotion_pct: float,
    continuation_score: int = 0,
    continuation_demotion_score: int = 6,
) -> BreadthRead:
    """Two numbers per fund from its constituent list: the share above their
    own 200-day SMA, and the median constituent 52-week range position minus
    the fund's own. A broad move persists; a narrow one is a top forming."""
    above = 0
    used = 0
    positions: list[float] = []
    for ticker in seeds:
        pts = constituent_series.get(ticker.upper())
        if not pts or len(pts) < 200:
            continue
        closes = [c for _, c in pts]
        current = closes[-1]
        sma200 = sma_series(closes, 200)[-1]
        if sma200 is None or current <= 0:
            continue
        used += 1
        if current >= sma200:
            above += 1
        yr = closes[-252:] if len(closes) >= 252 else closes
        hi, lo = max(yr), min(yr)
        if hi > lo:
            positions.append((current - lo) / (hi - lo))
    if used == 0:
        return BreadthRead(None, None, 0, round(coverage, 3), demoted=False)
    pct = above / used
    positions.sort()
    median_pos = positions[len(positions) // 2] if positions else None
    delta = (median_pos - fund_pos_52w) if median_pos is not None else None
    # Thin breadth demotes height AND persistence: the spec demotes Extended
    # and CONTINUATION alike (a narrow move is a top forming either way).
    # Continuation was originally exempt (review finding, 2026-09-04).
    demoted = pct < (demotion_pct / 100.0) and (
        classification in ("Extended", "Leading and earning it")
        or continuation_score >= continuation_demotion_score
    )
    return BreadthRead(
        pct_above_200d=round(pct, 3),
        median_pos_minus_fund=round(delta, 3) if delta is not None else None,
        constituents_used=used,
        coverage=round(coverage, 3),
        demoted=demoted,
    )


# --- 2. IV rank ------------------------------------------------------------------


@dataclass(frozen=True)
class IVRankRead:
    iv_rank: float | None      # 0..100 within its own trailing window
    current_iv: float | None
    days_collected: int
    window_days: int
    regime: str                # "buy_premium" | "sell_premium" | "either" | "collecting"

    def label(self) -> str:
        if self.iv_rank is None:
            return f"collecting ({self.days_collected}/{self.window_days} days)"
        return f"IV rank {self.iv_rank:.0f} (ATM IV {self.current_iv * 100:.1f}%)"


def iv_rank_read(
    iv_history: dict[str, float],
    current_iv: float | None,
    *,
    window_days: int,
    min_days: int,
    buy_max: float,
    sell_min: float,
) -> IVRankRead:
    """Current ATM IV's rank within its own self-collected trailing history.
    No historical IV exists on the plan, so the daily cadence builds the
    window; below min_days the read is 'collecting' and the structure choice
    falls back to classification alone (stated in the report)."""
    days = len(iv_history)
    if current_iv is None or days < min_days:
        return IVRankRead(None, current_iv, days, window_days, "collecting")
    # Trailing window = the most RECENT window_days by DATE key (ISO dates
    # sort chronologically). Sorting by value selected the largest IVs ever
    # retained instead of the trailing year (review finding, 2026-09-04).
    recent_keys = sorted(iv_history)[-window_days:]
    values = [iv_history[k] for k in recent_keys]
    below = sum(1 for v in values if v < current_iv)
    rank = 100.0 * below / len(values)
    if rank < buy_max:
        regime = "buy_premium"
    elif rank > sell_min:
        regime = "sell_premium"
    else:
        regime = "either"
    return IVRankRead(round(rank, 1), current_iv, days, window_days, regime)


# --- 4. correlation guard ----------------------------------------------------------


def daily_returns(closes: list[float]) -> list[float]:
    return [
        (closes[i] / closes[i - 1]) - 1.0
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    ]


def correlation(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 40:
        return None
    xa, xb = a[-n:], b[-n:]
    ma = sum(xa) / n
    mb = sum(xb) / n
    cov = sum((xa[i] - ma) * (xb[i] - mb) for i in range(n))
    va = sum((x - ma) ** 2 for x in xa)
    vb = sum((x - mb) ** 2 for x in xb)
    if va <= 0 or vb <= 0:
        return None
    return cov / math.sqrt(va * vb)


def correlation_matrix(
    closes_by_symbol: dict[str, list[float]], lookback_days: int
) -> dict[tuple[str, str], float]:
    """Pairwise correlation of daily returns over the trailing window."""
    rets = {
        sym: daily_returns(closes[-(lookback_days + 1):])
        for sym, closes in closes_by_symbol.items()
    }
    out: dict[tuple[str, str], float] = {}
    symbols = sorted(rets)
    for i, a in enumerate(symbols):
        for b in symbols[i + 1:]:
            c = correlation(rets[a], rets[b])
            if c is not None:
                out[(a, b)] = round(c, 3)
    return out


def collapse_correlated(
    ranked_symbols: list[str],
    matrix: dict[tuple[str, str], float],
    threshold: float,
) -> dict[str, str]:
    """Any pair above the threshold is ONE position: the higher-ranked symbol
    is primary, the other maps to it as an alternative expression. Returns
    {secondary: primary} for every collapsed symbol."""
    collapsed: dict[str, str] = {}
    kept: list[str] = []
    for sym in ranked_symbols:
        primary = None
        for k in kept:
            pair = (min(k, sym), max(k, sym))
            if abs(matrix.get(pair, 0.0)) > threshold:
                primary = collapsed.get(k, k)
                break
        if primary is None:
            kept.append(sym)
        else:
            collapsed[sym] = primary
    return collapsed


# --- 5. rate sensitivity --------------------------------------------------------------


def rate_beta(fund_closes: list[float], tlt_closes: list[float], lookback_days: int) -> float | None:
    """Beta of the fund's daily returns to TLT's over the trailing window.
    Where three or more selected funds share a beta above the threshold, the
    report says plainly they are one duration trade."""
    fr = daily_returns(fund_closes[-(lookback_days + 1):])
    tr = daily_returns(tlt_closes[-(lookback_days + 1):])
    n = min(len(fr), len(tr))
    if n < 40:
        return None
    fr, tr = fr[-n:], tr[-n:]
    mt = sum(tr) / n
    mf = sum(fr) / n
    var_t = sum((x - mt) ** 2 for x in tr)
    if var_t <= 0:
        return None
    cov = sum((fr[i] - mf) * (tr[i] - mt) for i in range(n))
    return round(cov / var_t, 2)


# --- 6. short interest (context only) ---------------------------------------------------


def short_interest_context(readings: list[dict[str, Any]]) -> str:
    """Direction over the last readings, as narrative context. Percent of
    float is NOT derivable on this plan (no float field here); days-to-cover
    and the trend are reported instead, labeled as what they are."""
    if not readings:
        return "n/a"
    si = [r.get("short_interest") for r in readings if r.get("short_interest") is not None]
    dtc = readings[0].get("days_to_cover")
    if len(si) >= 2:
        direction = "rising" if si[0] > si[-1] else ("falling" if si[0] < si[-1] else "flat")
    else:
        direction = "insufficient history"
    head = f"{si[0]:,.0f} shares short" if si else "n/a"
    tail = f", {float(dtc):.1f} days to cover" if dtc is not None else ""
    return f"{head} ({direction} over last {len(readings)} readings{tail})"


# --- 8. term structure & skew (context only) ----------------------------------------------


def term_structure_note(
    front_iv: float | None, target_iv: float | None, *, cheap_rich_pts: float = 2.0
) -> str:
    if front_iv is None or target_iv is None:
        return "n/a"
    diff = (target_iv - front_iv) * 100.0
    if diff <= -cheap_rich_pts:
        judgement = "long-dated optionality comparatively cheap (argues for the horizon)"
    elif diff >= cheap_rich_pts:
        judgement = "long-dated premium rich vs the front month"
    else:
        judgement = "flat term structure"
    return (
        f"target-expiry ATM IV {target_iv * 100:.1f}% vs front-month {front_iv * 100:.1f}%"
        f" ({diff:+.1f} pts): {judgement}"
    )


def skew_note(
    put_25d_iv: float | None, call_25d_iv: float | None, classification: str,
    *, heavy_put_skew_pts: float = 4.0,
) -> str:
    if put_25d_iv is None or call_25d_iv is None:
        return "n/a"
    skew_pts = (put_25d_iv - call_25d_iv) * 100.0
    base = f"25-delta skew {skew_pts:+.1f} pts (put {put_25d_iv * 100:.1f}% / call {call_25d_iv * 100:.1f}%)"
    if skew_pts >= heavy_put_skew_pts and classification == "Coiled":
        return base + " -- heavy put skew: the options market actively disagrees with the recovery thesis"
    return base


# --- 9. seasonality (context only, tiny sample by construction) ----------------------------


def seasonality_note(
    closes_monthly: list[float], months_dates: list[str], run_month: int, forward_months: int
) -> str:
    """Average and hit rate of the same forward window across the available
    monthly history. The sample is at most a handful of observations -- it is
    a footnote and never a scoring input, and the note carries its N."""
    idx_by_month: list[int] = [
        i for i, ym in enumerate(months_dates) if int(ym.split("-")[1]) == run_month
    ]
    rets: list[float] = []
    for i in idx_by_month:
        j = i + forward_months
        if j < len(closes_monthly) and closes_monthly[i] > 0:
            rets.append((closes_monthly[j] / closes_monthly[i]) - 1.0)
    if not rets:
        return "no completed same-month forward windows in the available history"
    avg = sum(rets) / len(rets)
    hits = sum(1 for r in rets if r > 0)
    return (
        f"same {forward_months}m window from this month: avg {avg * 100:+.1f}%, "
        f"positive {hits}/{len(rets)} times (n={len(rets)} -- context only)"
    )
