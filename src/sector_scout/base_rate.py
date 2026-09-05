"""Empirical base rate for a Sector Scout play, replayed over the available
history with the SAME lens functions the live report used.

For every play: at each historical month-end where the fund carried the same
classification (and, for continuation plays, passed the same gates), did the
underlying move the required percent in the required direction within the
matching forward window? The fraction always travels with its sample size and
is shrunk by occurrences / (occurrences + min_occurrences), exactly the
scout_backtest discipline. Below min_occurrences it is labeled LOW CONFIDENCE.

The report prints this NEXT TO the Black-Scholes number, and a divergence
above the configured threshold is called out in the strategy narrative: that
gap is itself a finding, usually the options market pricing something the
history does not contain.

Honesty note: the stocks entitlement caps history (~2 years verified live),
so the sample is what the plan allows and the label says which window it is.

ANALYSIS ONLY -- no order path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .lenses import classify, extremes_read


@dataclass(frozen=True)
class BaseRate:
    occurrences: int
    hits: int
    min_occurrences: int
    window_label: str
    forward_months: int

    @property
    def rate(self) -> float:
        return (self.hits / self.occurrences) if self.occurrences else 0.0

    @property
    def low_confidence(self) -> bool:
        return self.occurrences < self.min_occurrences

    @property
    def confidence_weight(self) -> float:
        if self.occurrences <= 0:
            return 0.0
        return self.occurrences / (self.occurrences + max(self.min_occurrences, 1))

    def summary(self) -> str:
        if self.occurrences == 0:
            return f"no matching historical occurrences in {self.window_label} of history"
        label = " (LOW CONFIDENCE)" if self.low_confidence else ""
        return (
            f"{self.rate * 100:.0f}% over {self.occurrences} matching setups in "
            f"{self.window_label} of history{label}"
        )


def replay_base_rate(
    *,
    closes_daily: list[float],
    highs_daily: list[float],
    lows_daily: list[float],
    closes_monthly: list[float],
    spy_closes_monthly: list[float],
    monthly_indices: list[int],
    target_classification: str,
    bullish: bool,
    required_move_pct: float,
    forward_months: int,
    min_occurrences: int,
    window_label: str,
    cfg: dict[str, Any],
    step_months: int = 1,
) -> BaseRate:
    """Walk month-ends through history; where the SAME classification held,
    test whether the forward window reached the required move. The valuation /
    earnings overlay is unknowable historically on this plan, so the replay
    classifies on price/RS structure alone -- which can only make the match
    LOOSER, never invent hits; the method section states this.

    NO LOOKAHEAD: `monthly_indices` (from data.monthly_end_indices) maps each
    monthly close to the exact daily index of that month's last bar, so the
    truncated daily series ends ON the month-end being classified. Months
    whose index cannot be resolved are SKIPPED, never clamped -- clamping
    substituted the full series (including the graded forward window) and
    inflated the base rate (review finding, 2026-09-04)."""
    n_months = min(len(closes_monthly), len(spy_closes_monthly), len(monthly_indices))
    occurrences = 0
    hits = 0

    # Need enough daily history behind each month-end for SMAs and enough
    # ahead of it for the forward window.
    for m in range(6, n_months - forward_months, step_months):
        daily_idx = monthly_indices[m]
        if daily_idx >= len(closes_daily):
            continue  # unresolvable month: skip, never clamp
        hist_daily = closes_daily[: daily_idx + 1]
        if len(hist_daily) < 60:
            continue
        ext = extremes_read(
            closes_daily=hist_daily,
            highs_daily=highs_daily[: daily_idx + 1],
            lows_daily=lows_daily[: daily_idx + 1],
            closes_monthly=closes_monthly[: m + 1],
            spy_closes_monthly=spy_closes_monthly[: m + 1],
            weekly_closes=hist_daily[::5] or hist_daily,
            window_label=window_label,
            cfg=cfg,
        )
        if ext is None:
            continue
        hist_class = classify(
            ext, valuation_rich=None, valuation_cheap=None, earnings_growing=None, cfg=cfg
        )
        if hist_class != target_classification:
            continue
        base = closes_monthly[m]
        future = closes_monthly[m + 1 : m + 1 + forward_months]
        if base <= 0 or not future:
            continue
        occurrences += 1
        if bullish:
            best = max(future)
            if (best / base - 1.0) * 100.0 >= required_move_pct:
                hits += 1
        else:
            worst = min(future)
            if (1.0 - worst / base) * 100.0 >= required_move_pct:
                hits += 1

    return BaseRate(
        occurrences=occurrences,
        hits=hits,
        min_occurrences=min_occurrences,
        window_label=window_label,
        forward_months=forward_months,
    )
