"""Run one settlement sweep: settle every not-yet-final play in a lookback window
and write its outcome back to the shared day-state, so the dashboard's Scout
Reports tab lights up with plan-vs-actual accuracy.

ANALYSIS ONLY. Reads delayed public daily bars (Massive) and writes a verdict
dict per play via store.save_outcomes. No order path; nothing here can place,
review, or cancel a trade.

Idempotent + safe to re-run: a play already scored WIN/LOSS is frozen and
skipped; an OPEN play is re-evaluated each run until it resolves or ages past the
horizon. The engine only touches PAST days (strictly before today), so it never
races the scouts' or alerter's current-day writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ..entry_alerts import store
from ..equity_intelligence.massive_client import MassiveClient
from .config import (
    resolve_bucket,
    resolve_horizon_days,
    resolve_lookback_days,
    resolve_min_interval,
    load_alerts_config,
)
from .settlement import BarLite, bar_date, settle

FROZEN_VERDICTS = frozenset({"WIN", "LOSS"})


@dataclass
class SettlementResult:
    days_processed: int = 0
    settled_win: int = 0
    settled_loss: int = 0
    still_open: int = 0
    skipped_frozen: int = 0
    errors: list[str] = field(default_factory=list)
    detail: str = ""


def today_et() -> str:
    """Today's date in US market time. Falls back to UTC date if zoneinfo/tzdata
    is unavailable (the date only gates which past days are settle-eligible)."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return datetime.now(UTC).date().isoformat()


def _is_frozen(rec: dict[str, Any]) -> bool:
    outcome = store.play_outcome(rec)
    if outcome is None:
        return False
    return str(outcome.get("verdict", "")).strip().upper() in FROZEN_VERDICTS


def _daily_barlites(client: MassiveClient, symbol: str, from_date: str, to_date: str) -> list[BarLite]:
    bars = client.get_daily_bars(symbol, from_date, to_date)  # ascending
    return [BarLite(date=bar_date(b.timestamp_ms), high=float(b.high), low=float(b.low)) for b in bars]


def _minute_barlites(client: MassiveClient, symbol: str, day: str) -> list[BarLite]:
    bars = client.get_aggs_range(symbol, 1, "minute", day, day, sort="asc")
    return [BarLite(date=day, high=float(b.high), low=float(b.low)) for b in bars]


def _settle_one(
    client: MassiveClient,
    rec: dict[str, Any],
    *,
    bars: list[BarLite],
    horizon_days: int,
) -> dict[str, Any] | None:
    """Settle a single play record against pre-fetched daily bars. Returns the
    outcome dict, or None when the record is unusable (missing/uncastable levels)."""
    try:
        entry = float(rec["entry"])
        target = float(rec["target"])
        stop = float(rec["stop"])
    except (KeyError, TypeError, ValueError):
        return None
    symbol = str(rec.get("symbol") or "").upper()
    return settle(
        direction=str(rec.get("direction") or ""),
        entry=entry,
        target=target,
        stop=stop,
        daily_bars=bars,
        horizon_days=horizon_days,
        minute_bars_for=lambda d, s=symbol: _minute_barlites(client, s, d),
    )


def run_settlement(
    *,
    today: str | None = None,
    horizon_days: int | None = None,
    lookback_days: int | None = None,
    bucket: str | None = None,
    client: MassiveClient | None = None,
    dry_run: bool = False,
) -> SettlementResult:
    """Sweep the recent report days and record each play's outcome. Returns a
    tally; never raises on a single symbol's/day's data hiccup (it is recorded in
    ``errors`` and the sweep continues)."""
    config = load_alerts_config()
    if bucket is None:
        bucket = resolve_bucket(config)
    horizon = horizon_days if horizon_days is not None else resolve_horizon_days()
    lookback = lookback_days if lookback_days is not None else resolve_lookback_days()
    today = today or today_et()
    if client is None:
        client = MassiveClient(min_interval=resolve_min_interval())

    result = SettlementResult()
    today_dt = date.fromisoformat(today)

    # Only PAST days are settle-eligible; a day needs at least one later session.
    days = [d for d in store.list_report_days(limit=lookback, bucket=bucket) if d < today]
    for day in days:
        raw = store.load_report_raw(day, bucket=bucket)
        records = store.report_plays(raw)
        if not records:
            continue
        result.days_processed += 1

        day_dt = date.fromisoformat(day)
        from_date = (day_dt + timedelta(days=1)).isoformat()
        # A generous calendar span so `horizon` TRADING sessions are available;
        # settle() only consumes the first `horizon` bars. Capped at yesterday.
        span_end = min(today_dt - timedelta(days=1), day_dt + timedelta(days=horizon * 2 + 5))
        if span_end < day_dt + timedelta(days=1):
            continue  # no completed session after this day yet
        to_date = span_end.isoformat()

        outcomes: dict[str, dict[str, Any]] = {}
        bars_by_symbol: dict[str, list[BarLite]] = {}
        for rec in records:
            if _is_frozen(rec):
                result.skipped_frozen += 1
                continue
            pid = str(rec.get("id") or "")
            symbol = str(rec.get("symbol") or "").upper()
            if not pid or not symbol:
                continue
            try:
                bars = bars_by_symbol.get(symbol)
                if bars is None:
                    bars = _daily_barlites(client, symbol, from_date, to_date)
                    bars_by_symbol[symbol] = bars
                outcome = _settle_one(client, rec, bars=bars, horizon_days=horizon)
            except Exception as exc:  # a single symbol's data hiccup never sinks the sweep
                result.errors.append(f"{day}/{symbol}: {type(exc).__name__}")
                continue
            if outcome is None:
                continue
            verdict = str(outcome.get("verdict", "")).upper()
            if verdict == "WIN":
                result.settled_win += 1
            elif verdict == "LOSS":
                result.settled_loss += 1
            else:
                result.still_open += 1
            outcomes[pid] = outcome

        if outcomes and not dry_run:
            try:
                store.save_outcomes(day, outcomes, bucket=bucket)
            except Exception as exc:
                result.errors.append(f"{day}/save: {type(exc).__name__}")

    result.detail = (
        f"settled {result.settled_win}W/{result.settled_loss}L, "
        f"{result.still_open} still open, {result.skipped_frozen} already-final, "
        f"across {result.days_processed} report day(s)"
        + (" [dry-run]" if dry_run else "")
    )
    return result
