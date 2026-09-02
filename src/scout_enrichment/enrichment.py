"""Turn Finnhub's free data into per-symbol enrichment for the options scout:
is there an EARNINGS report inside the play's horizon (a major IV-crush / gap
risk), and what's the latest news HEADLINE.

ANALYSIS ONLY, BEST-EFFORT. With no API key or on any error, enrich_symbols
returns {} and the caller renders the email exactly as before.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable

from .finnhub_client import FinnhubClient

# How far ahead (calendar days) an earnings date counts as "inside the horizon".
# ~2 weeks comfortably covers a typical options-play window.
DEFAULT_EARNINGS_HORIZON_DAYS = 14
# How far back to look for a recent news headline.
DEFAULT_HEADLINE_LOOKBACK_DAYS = 5


@dataclass(frozen=True)
class PlayEnrichment:
    """Event context for one symbol. All fields optional; an all-empty instance
    means 'nothing to add'."""

    earnings_date: str | None = None
    days_to_earnings: int | None = None
    earnings_in_horizon: bool = False
    headline: str | None = None
    headline_url: str | None = None
    headline_source: str | None = None


FINNHUB_VAULT_NAME = "finnhub"  # Secret Manager secret name (operator-created)


def resolve_finnhub_key(explicit: str | None = None) -> str:
    """Env-first (FINNHUB_API_KEY), then Secret Manager (FINNHUB_VAULT_NAME,
    default "finnhub") via the scout's shared resolver. "" when neither is set --
    which simply leaves enrichment dormant. Never logs the value."""
    if explicit:
        return explicit
    value = (os.getenv("FINNHUB_API_KEY") or "").strip()
    if value:
        return value
    try:
        from ..options_scout.config import _secret_from_manager

        fetched = _secret_from_manager(os.getenv("FINNHUB_VAULT_NAME") or FINNHUB_VAULT_NAME)
    except Exception:
        fetched = None
    return (fetched or "").strip()


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _nearest_earnings(events: Iterable[dict[str, Any]], today: date) -> dict[str, date]:
    """Map each symbol to its NEXT (earliest today-or-later) earnings date in the
    fetched window."""
    out: dict[str, date] = {}
    for event in events:
        symbol = str(event.get("symbol", "")).strip().upper()
        when = _parse_date(event.get("date"))
        if not symbol or when is None or when < today:
            continue
        if symbol not in out or when < out[symbol]:
            out[symbol] = when
    return out


def _latest_headline(news: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The most recent news item (max `datetime`, unix seconds)."""
    best: dict[str, Any] | None = None
    best_ts = -1.0
    for item in news:
        headline = str(item.get("headline", "")).strip()
        if not headline:
            continue
        try:
            ts = float(item.get("datetime", 0) or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts >= best_ts:
            best_ts = ts
            best = item
    return best


def enrich_symbols(
    symbols: Iterable[str],
    *,
    today: date,
    earnings_horizon_days: int = DEFAULT_EARNINGS_HORIZON_DAYS,
    headline_lookback_days: int = DEFAULT_HEADLINE_LOOKBACK_DAYS,
    client: FinnhubClient | None = None,
) -> dict[str, PlayEnrichment]:
    """Best-effort event enrichment for a set of symbols. Returns {} when Finnhub
    is unavailable (no key / error). Never raises."""
    uniq = sorted({str(s).strip().upper() for s in symbols if str(s).strip()})
    if not uniq:
        return {}
    if client is None:
        client = FinnhubClient(resolve_finnhub_key())
    if not client.enabled:
        return {}

    # One earnings-calendar call for the whole window, then filter locally.
    try:
        cal = client.get_earnings_calendar(
            today.isoformat(), (today + timedelta(days=earnings_horizon_days)).isoformat()
        )
    except Exception:
        cal = []
    earnings_by_symbol = _nearest_earnings(cal, today)

    news_from = (today - timedelta(days=headline_lookback_days)).isoformat()
    news_to = today.isoformat()

    result: dict[str, PlayEnrichment] = {}
    for symbol in uniq:
        earnings_date = earnings_by_symbol.get(symbol)
        days_to = (earnings_date - today).days if earnings_date else None
        in_horizon = days_to is not None and 0 <= days_to <= earnings_horizon_days

        headline = url = source = None
        try:
            item = _latest_headline(client.get_company_news(symbol, news_from, news_to))
        except Exception:
            item = None
        if item is not None:
            headline = str(item.get("headline", "")).strip() or None
            url = str(item.get("url", "")).strip() or None
            source = str(item.get("source", "")).strip() or None

        if earnings_date is None and headline is None:
            continue  # nothing to add for this symbol
        result[symbol] = PlayEnrichment(
            earnings_date=earnings_date.isoformat() if earnings_date else None,
            days_to_earnings=days_to,
            earnings_in_horizon=in_horizon,
            headline=headline,
            headline_url=url,
            headline_source=source,
        )
    return result
