"""Point-in-time replay of the options scout + settlement, for calibration.

For each past "report date" D we run scout_plays(today=D) using ONLY data up to D
(the analyzer bounds its bars to `today`; the news factor is disabled here because
its latest-headlines fetch is not point-in-time), then settle each play against
the REAL bars after D with the same settle() the live engine uses. The resulting
records are shaped exactly like the store's play dicts, so compute_calibration
consumes them unchanged.

ANALYSIS ONLY. Read-only; no order path.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Callable

from ..options_scout.analyzer import scout_plays
from ..scout_settlement.settlement import BarLite, bar_date, settle


def news_off(config: dict[str, Any]) -> dict[str, Any]:
    """A shallow copy of config with the news factor disabled -- news is the only
    non-point-in-time input, so a replay must not use it."""
    cfg = dict(config)
    news = dict(cfg.get("news") or {})
    news["enabled"] = False
    cfg["news"] = news
    return cfg


def weekly_dates(end: date, months: int, *, step_days: int = 7) -> list[date]:
    """Weekday report dates every `step_days`, spanning `months` back from `end`
    (exclusive of `end` itself so there is always a completed window to settle
    against), oldest first."""
    start = end - timedelta(days=int(round(months * 30.44)))
    out: list[date] = []
    day = start
    last = end - timedelta(days=1)
    while day <= last:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=step_days)
    return out


def _daily_barlites(client: Any, symbol: str, from_date: str, to_date: str) -> list[BarLite]:
    bars = client.get_daily_bars(symbol, from_date, to_date)
    return [BarLite(bar_date(b.timestamp_ms), float(b.high), float(b.low)) for b in bars]


def _minute_barlites(client: Any, symbol: str, day: str) -> list[BarLite]:
    bars = client.get_aggs_range(symbol, 1, "minute", day, day, sort="asc")
    return [BarLite(day, float(b.high), float(b.low)) for b in bars]


def settle_play(client: Any, play: Any, day: date, horizon_days: int) -> dict[str, Any] | None:
    """Settle one replayed play against the REAL bars AFTER `day`. Returns a
    calibration-shaped record (source/conviction/predicted_hit_rate/outcome), or
    None when the play is unusable or its forward bars are unavailable."""
    try:
        entry = float(play.entry)
        target = float(play.target)
        stop = float(play.stop)
        symbol = str(play.symbol).upper()
        direction = str(play.direction)
    except (AttributeError, TypeError, ValueError):
        return None
    from_date = (day + timedelta(days=1)).isoformat()
    to_date = (day + timedelta(days=horizon_days * 2 + 7)).isoformat()
    try:
        daily = _daily_barlites(client, symbol, from_date, to_date)
    except Exception:
        return None
    if not daily:
        return None
    outcome = settle(
        direction=direction, entry=entry, target=target, stop=stop,
        daily_bars=daily, horizon_days=horizon_days,
        minute_bars_for=lambda d, s=symbol: _minute_barlites(client, s, d),
    )
    hit_rate = getattr(play, "hit_rate", None)
    return {
        "source": "options",
        "symbol": symbol,
        "direction": direction,
        "date": day.isoformat(),
        "conviction": getattr(play, "conviction", None),
        "predicted_hit_rate": getattr(hit_rate, "hit_rate", None),
        "entry": entry,
        "target": target,
        "stop": stop,
        "outcome": outcome,
    }


def capture_factors(client: Any, config: dict[str, Any], symbol: str, day: date) -> dict[str, float]:
    """Re-derive the directional-score factor RAW reads (trend/momentum/rsi) as-of
    `day`, for weight tuning. Reuses the exact daily-bars call scout_plays already
    made (same args -> cache hit), so this is nearly free within a replay."""
    try:
        from ..options_scout.indicators import build_series, directional_score_at

        ind_cfg = config.get("indicators", {}) or {}
        weights = config.get("weights", {}) or {}
        hist = int(config.get("history_days", 730))
        bars = client.get_daily_bars(symbol, (day - timedelta(days=hist)).isoformat(), day.isoformat())
        if len(bars) < int(ind_cfg.get("sma_long", 200)) + 5:
            return {}
        series = build_series([b.high for b in bars], [b.low for b in bars], [b.close for b in bars], ind_cfg)
        read = directional_score_at(series, series.length - 1, weights)
        if read is None:
            return {}
        return {f.name: round(float(f.raw), 4) for f in read.factors}
    except Exception:
        return {}


def run_backtest(
    client: Any,
    config: dict[str, Any],
    *,
    dates: list[date],
    horizon_days: int | None = None,
    progress: Callable[[date, int], None] | None = None,
) -> dict[str, Any]:
    """Replay the scout across `dates` and settle every play. Returns the settled
    records (for compute_calibration) plus counts. Never raises on a single date's
    hiccup -- it is skipped and the replay continues."""
    cfg = news_off(config)
    horizon = int(horizon_days if horizon_days is not None else cfg.get("horizon_days", 10))
    records: list[dict[str, Any]] = []
    days_run = 0
    plays_seen = 0
    for day in dates:
        try:
            plays = scout_plays(
                client, cfg, today=day, now=datetime(day.year, day.month, day.day, tzinfo=UTC)
            )
        except Exception:
            continue
        days_run += 1
        for play in plays:
            plays_seen += 1
            rec = settle_play(client, play, day, horizon)
            if rec is not None:
                rec["factors"] = capture_factors(client, cfg, rec["symbol"], day)
                records.append(rec)
        if progress is not None:
            progress(day, len(records))
    return {
        "records": records,
        "days_run": days_run,
        "plays_seen": plays_seen,
        "settled_records": len(records),
        "horizon_days": horizon,
    }
