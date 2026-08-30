"""Honest empirical hit-rate for a directional setup.

Replays TODAY's trigger rule over the symbol's daily history and reports, for
the setup's direction, the fraction of past occurrences where the underlying
reached its (vol-scaled) target within the horizon -- together with the sample
size that fraction rests on.

The honesty rules, made explicit:
  * The historical trigger is the SAME `directional_score_at` the live thesis
    uses -- no separate, kinder rule for the backtest.
  * The target at each historical day is derived from THAT day's realized vol
    (no lookahead): target = close_t +/- close_t * rv_t * sqrt(horizon).
  * A hit means the underlying's high (call) or low (put) reached the target in
    the horizon window -- a real, checkable price event, not a model estimate.
  * The fraction ALWAYS travels with its occurrence count. Below
    `min_occurrences` it is flagged low-confidence. It is a base rate, never a
    guarantee, and the email says so.

ANALYSIS ONLY -- no order path.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .indicators import IndicatorSeries, directional_score_at, realized_vol_at


@dataclass(frozen=True)
class HitRate:
    """A backtested base rate with the sample it rests on."""

    direction: str  # "call" | "put"
    horizon_days: int
    occurrences: int
    hits: int
    low_confidence: bool
    min_occurrences: int

    @property
    def hit_rate(self) -> float:
        return (self.hits / self.occurrences) if self.occurrences else 0.0

    @property
    def confidence_weight(self) -> float:
        """Sample-size shrinkage in [0, 1): occurrences / (occurrences + min).
        A play with few occurrences keeps most of its hit-rate discounted so it
        cannot out-rank a well-sampled setup on a lucky small-N fraction."""
        if self.occurrences <= 0:
            return 0.0
        return self.occurrences / (self.occurrences + max(self.min_occurrences, 1))

    def summary(self) -> str:
        if self.occurrences == 0:
            return f"no historical occurrences of this {self.direction} setup"
        label = " (LOW CONFIDENCE)" if self.low_confidence else ""
        return (
            f"{self.hit_rate * 100:.0f}% hit rate over {self.occurrences} past "
            f"occurrences{label}"
        )


def _triggered(read: Any, direction: str, trigger_min: float) -> bool:
    return (
        read is not None
        and read.direction == direction
        and abs(read.score) >= trigger_min
    )


def backtest_setup(
    series: IndicatorSeries,
    direction: str,
    horizon_days: int,
    vol_lookback: int,
    weights: dict[str, Any],
    min_occurrences: int,
    *,
    trigger_min_abs_score: float = 0.15,
    fresh_edge_only: bool = True,
) -> HitRate:
    """Replay the trigger over history and count target-reaches within horizon."""
    n = series.length
    occurrences = 0
    hits = 0
    prev_triggered = False

    # Stop early enough that a full horizon window of future bars exists.
    last_idx = n - horizon_days - 1
    for idx in range(n):
        read = directional_score_at(series, idx, weights)
        now_trig = _triggered(read, direction, trigger_min_abs_score)

        if idx > last_idx:
            prev_triggered = now_trig
            continue
        if not now_trig:
            prev_triggered = now_trig
            continue
        if fresh_edge_only and prev_triggered:
            prev_triggered = now_trig
            continue

        rv = realized_vol_at(series.closes, idx, vol_lookback)
        if rv is None or rv <= 0:
            prev_triggered = now_trig
            continue

        close_t = series.closes[idx]
        move = close_t * rv * math.sqrt(horizon_days)
        occurrences += 1
        window_hi = series.highs[idx + 1 : idx + 1 + horizon_days]
        window_lo = series.lows[idx + 1 : idx + 1 + horizon_days]
        if direction == "call":
            target = close_t + move
            if any(h >= target for h in window_hi):
                hits += 1
        else:
            target = close_t - move
            if any(lo <= target for lo in window_lo):
                hits += 1
        prev_triggered = now_trig

    low_conf = occurrences < min_occurrences
    return HitRate(
        direction=direction,
        horizon_days=horizon_days,
        occurrences=occurrences,
        hits=hits,
        low_confidence=low_conf,
        min_occurrences=min_occurrences,
    )
