"""Pure settlement logic: did the underlying reach the play's TARGET before its
STOP, within a bounded horizon?

ANALYSIS ONLY. This module has no I/O -- it decides a verdict from price bars a
caller supplies. The operator-chosen accuracy metric is "underlying hit target
before stop." Every level is on the UNDERLYING, exactly as the scout emitted it.

Direction semantics (confirmed against the persisted play records):

  bullish (call / long): target ABOVE entry, stop BELOW entry.
      target hit  <=>  session HIGH >= target
      stop   hit  <=>  session LOW  <= stop
  bearish (put):         target BELOW entry, stop ABOVE entry.
      target hit  <=>  session LOW  <= target
      stop   hit  <=>  session HIGH >= stop

The daily walk starts the session AFTER the play date and runs up to
``horizon_days`` trading sessions, in order; the FIRST session that touches a
level decides the verdict. If a single session's [low, high] spans BOTH the
target and the stop, that session is ambiguous from daily bars alone: a
minute-bar provider (when supplied) resolves which came first, otherwise the tie
is scored conservatively as a LOSS -- never over-claim a win. Reaching the end of
the horizon with no touch is OPEN (uncounted in accuracy).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Sequence

# A call or a long share position is bullish; a put is the only bearish shape the
# scouts emit. Matches store.BULLISH_DIRECTIONS.
BULLISH_DIRECTIONS = frozenset({"call", "long"})

# What every outcome records as its measurement, so the dashboard/report can tell
# these apart from any future metric.
METRIC = "target_before_stop"


@dataclass(frozen=True)
class BarLite:
    """The only fields settlement needs from a price bar: the session date and the
    session's price range. The engine adapts Massive ``Bar`` objects to these."""

    date: str  # YYYY-MM-DD (trading session)
    high: float
    low: float


def bar_date(timestamp_ms: int) -> str:
    """The calendar date of a bar timestamp. Daily Massive/Polygon bars are
    stamped at midnight ET, which is the same calendar date in UTC, so a UTC-date
    conversion yields the trading session's date."""
    return datetime.fromtimestamp(int(timestamp_ms) / 1000, UTC).date().isoformat()


def is_bullish(direction: str) -> bool:
    return str(direction).strip().lower() in BULLISH_DIRECTIONS


def _touches(bar: BarLite, *, bullish: bool, target: float, stop: float) -> tuple[bool, bool]:
    """(hit_target, hit_stop) for one session, per the direction semantics."""
    if bullish:
        return (bar.high >= target, bar.low <= stop)
    return (bar.low <= target, bar.high >= stop)


def _return_pct(level: float, entry: float, bullish: bool) -> float | None:
    """The underlying move to ``level`` from entry, signed for the PLAY so that a
    favorable move is POSITIVE: bullish measures (level-entry)/entry; a put
    measures (entry-level)/entry, so a fall to a lower target reads positive."""
    if not entry:
        return None
    move = (level - entry) if bullish else (entry - level)
    return round(100.0 * move / entry, 2)


def _decided(
    verdict: str,
    level: float,
    *,
    entry: float,
    bullish: bool,
    hit_date: str,
    sessions: int,
    resolved: str,
) -> dict[str, Any]:
    return {
        "verdict": verdict,  # "WIN" | "LOSS"
        "actual": round(float(level), 4),  # the level touched (target or stop)
        "return_pct": _return_pct(level, entry, bullish),  # signed %, play POV
        "hit_date": hit_date,
        "sessions_to_hit": sessions,
        "resolved": resolved,  # "daily" | "minute" | "*conservative_loss"
        "metric": METRIC,
    }


def _resolve_same_day(
    bar: BarLite,
    *,
    bullish: bool,
    target: float,
    stop: float,
    minute_bars_for: Callable[[str], Sequence[BarLite]] | None,
) -> tuple[str, float, str]:
    """A session whose range spans BOTH levels. Return (verdict, level, resolved).
    Walk that session's minute bars in order and take whichever level is touched
    first. Absent/inconclusive minute data is scored conservatively as a LOSS --
    the risk-first choice never inflates the win rate."""
    if minute_bars_for is not None:
        try:
            minutes = list(minute_bars_for(bar.date))
        except Exception:
            minutes = []
        for minute in minutes:
            hit_target, hit_stop = _touches(minute, bullish=bullish, target=target, stop=stop)
            if hit_target and hit_stop:
                # Even a single minute spans both -> cannot order it; be conservative.
                return "LOSS", stop, "minute_ambiguous_conservative_loss"
            if hit_target:
                return "WIN", target, "minute"
            if hit_stop:
                return "LOSS", stop, "minute"
    return "LOSS", stop, "ambiguous_conservative_loss"


def settle(
    *,
    direction: str,
    entry: float,
    target: float,
    stop: float,
    daily_bars: Sequence[BarLite],
    horizon_days: int = 10,
    minute_bars_for: Callable[[str], Sequence[BarLite]] | None = None,
) -> dict[str, Any]:
    """Decide a play's outcome from the daily bars AFTER its report date.

    ``daily_bars`` must be ascending (oldest first) and already start the session
    after the play date; only the first ``horizon_days`` are considered. Returns a
    dict shaped for the dashboard (``store.play_outcome`` / ``report_summary``):
    a decided WIN/LOSS carries actual/return_pct/hit_date, while no touch inside
    the horizon is OPEN (uncounted)."""
    bullish = is_bullish(direction)
    considered = list(daily_bars)[: max(int(horizon_days), 0)]
    for index, bar in enumerate(considered, start=1):
        hit_target, hit_stop = _touches(bar, bullish=bullish, target=target, stop=stop)
        if hit_target and hit_stop:
            verdict, level, resolved = _resolve_same_day(
                bar, bullish=bullish, target=target, stop=stop, minute_bars_for=minute_bars_for
            )
            return _decided(
                verdict, level, entry=entry, bullish=bullish,
                hit_date=bar.date, sessions=index, resolved=resolved,
            )
        if hit_target:
            return _decided(
                "WIN", target, entry=entry, bullish=bullish,
                hit_date=bar.date, sessions=index, resolved="daily",
            )
        if hit_stop:
            return _decided(
                "LOSS", stop, entry=entry, bullish=bullish,
                hit_date=bar.date, sessions=index, resolved="daily",
            )
    return {
        "verdict": "OPEN",
        "resolved": "horizon" if considered else "no_data",
        "sessions_checked": len(considered),
        "metric": METRIC,
    }
