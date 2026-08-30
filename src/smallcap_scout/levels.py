"""Entry / target / stop for a small-cap momentum LONG, from daily bars.

ANALYSIS ONLY. These are levels ON THE SHARES for the reader to consider; this
module has no order path and prices nothing at execution time. Small-cap
momentum names are traded long off a continuation, so the levels are long-only:
an entry near the last price / breakout, a measured-move target from realized
vol and ATR, and a stop below recent support.

The bars come from the grouped-daily baseline window the scanner already pulled,
so computing levels needs NO extra per-ticker history call.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class Levels:
    """A long setup's reference levels on the underlying shares."""

    entry: float
    target: float
    stop: float
    breakout_ref: float  # highest high of the window before today
    atr: float
    realized_vol: float  # daily-return stdev over the window
    horizon_days: int
    method: str

    @property
    def reward(self) -> float:
        return max(self.target - self.entry, 0.0)

    @property
    def risk(self) -> float:
        return max(self.entry - self.stop, 0.0)

    @property
    def reward_risk(self) -> float:
        return (self.reward / self.risk) if self.risk > 0 else 0.0


def _atr(bars: Sequence[Any], window: int) -> float:
    """Average true range over the last `window` bars (Wilder's TR, simple mean).
    Falls back to whatever history exists when the window is longer than it."""
    trs: list[float] = []
    for i in range(1, len(bars)):
        high = float(bars[i].high)
        low = float(bars[i].low)
        prev_close = float(bars[i - 1].close)
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if not trs:
        return 0.0
    tail = trs[-window:] if window > 0 else trs
    return sum(tail) / len(tail)


def _realized_vol(bars: Sequence[Any]) -> float:
    """Population stdev of daily simple returns across the window."""
    closes = [float(b.close) for b in bars]
    returns: list[float] = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            returns.append(closes[i] / closes[i - 1] - 1.0)
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    return math.sqrt(variance)


def compute_levels(bars: Sequence[Any], config: dict[str, Any] | None = None) -> Levels | None:
    """Long entry/target/stop from a chronological (oldest-first) run of daily
    bars whose LAST element is the scan session. Returns None with too little
    history to frame a stop.

    * entry  = last close (a momentum-continuation entry near the last print;
               the breakout reference -- the prior window high -- is reported too)
    * target = entry + max(ATR * atr_target_mult, entry * realized_vol * sqrt(h))
    * stop   = below support: the recent support low, tightened so it is never
               further than ATR * stop_atr_mult below entry (bounds the risk)
    """
    cfg = config or {}
    if len(bars) < 3:
        return None

    atr_window = int(cfg.get("atr_window", 14))
    atr_target_mult = float(cfg.get("atr_target_mult", 2.0))
    stop_atr_mult = float(cfg.get("stop_atr_mult", 1.5))
    support_lookback = int(cfg.get("support_lookback", 10))
    horizon_days = int(cfg.get("horizon_days", 5))

    entry = float(bars[-1].close)
    if entry <= 0:
        return None

    atr = _atr(bars, atr_window)
    rv = _realized_vol(bars)

    prior = bars[:-1] or bars
    breakout_ref = max(float(b.high) for b in prior)

    vol_move = entry * rv * math.sqrt(max(horizon_days, 1))
    atr_move = atr * atr_target_mult
    move = max(vol_move, atr_move)
    if move <= 0:
        move = entry * 0.05  # degenerate flat window: a nominal 5% frame
    target = entry + move

    support_bars = bars[-support_lookback:] if support_lookback > 0 else bars
    support = min(float(b.low) for b in support_bars)
    atr_floor = entry - atr * stop_atr_mult if atr > 0 else support
    # Stop below support, but no further than ATR*mult from entry -> the higher
    # (closer-to-entry) of the two, so a wide window cannot open unbounded risk.
    stop = max(support, atr_floor)
    if stop >= entry:  # pathological; keep the stop strictly below entry
        stop = entry - (atr if atr > 0 else entry * 0.05)

    return Levels(
        entry=round(entry, 2),
        target=round(target, 2),
        stop=round(stop, 2),
        breakout_ref=round(breakout_ref, 2),
        atr=round(atr, 4),
        realized_vol=rv,
        horizon_days=horizon_days,
        method=(
            f"entry=last close; target=entry+max(ATR*{atr_target_mult:g}, "
            f"vol*sqrt({horizon_days})); stop=below {support_lookback}d support, "
            f"tightened to <= ATR*{stop_atr_mult:g}"
        ),
    )
