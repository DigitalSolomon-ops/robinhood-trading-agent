"""Empirical base rate for a Sector Scout play, replayed over the available
history with the SAME lens functions the live report used.

WEEKLY RESOLUTION (2026-09-07): percentiles moved to weekly bars, so the
replay walks WEEK-ends -- ~104 observations across the 2-year window instead
of ~24 month-ends. `weekly_indices` (from data.weekly_end_indices) maps each
weekly close to the exact daily index of that week's last bar, preserving
the no-lookahead guarantee: the truncated daily series ends ON the week-end
being classified, and unresolvable weeks are SKIPPED, never clamped.

The fraction always travels with its sample size and is shrunk by
occurrences / (occurrences + min_occurrences), exactly the scout_backtest
discipline. Below min_occurrences it is labeled LOW CONFIDENCE.

Honesty notes: the stocks entitlement caps history (~2 years verified live),
so the sample is what the plan allows and the label says which window it is.
The replay classifies on price/RS structure alone (valuation is unknowable
historically on this plan), which can only loosen the match, never invent
hits. Steps default to every 4th week so adjacent, near-identical setups do
not inflate the sample.

ANALYSIS ONLY -- no order path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .lenses import classify, extremes_read

WEEKS_PER_MONTH = 13.0 / 3.0  # 52 weeks / 12 months


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
    closes_weekly: list[float],
    spy_closes_weekly: list[float],
    weekly_indices: list[int],
    target_classification: str,
    bullish: bool,
    required_move_pct: float,
    forward_months: int,
    min_occurrences: int,
    window_label: str,
    cfg: dict[str, Any],
    step_weeks: int = 4,
) -> BaseRate:
    """Walk week-ends through history; where the SAME classification held,
    test whether the forward window reached the required move."""
    n_weeks = min(len(closes_weekly), len(spy_closes_weekly), len(weekly_indices))
    forward_weeks = max(1, round(forward_months * WEEKS_PER_MONTH))
    occurrences = 0
    hits = 0

    for w in range(26, n_weeks - forward_weeks, max(step_weeks, 1)):
        daily_idx = weekly_indices[w]
        if daily_idx >= len(closes_daily):
            continue  # unresolvable week: skip, never clamp
        hist_daily = closes_daily[: daily_idx + 1]
        if len(hist_daily) < 60:
            continue
        ext = extremes_read(
            closes_daily=hist_daily,
            highs_daily=highs_daily[: daily_idx + 1],
            lows_daily=lows_daily[: daily_idx + 1],
            closes_weekly_pct=closes_weekly[: w + 1],
            spy_closes_weekly_pct=spy_closes_weekly[: w + 1],
            weekly_closes=closes_weekly[: w + 1],
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
        base = closes_weekly[w]
        future = closes_weekly[w + 1 : w + 1 + forward_weeks]
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
