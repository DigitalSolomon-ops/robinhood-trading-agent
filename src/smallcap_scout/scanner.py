"""The Small-Cap Scout scanner: market-wide momentum scan -> ranked watchlist.

ANALYSIS ONLY. Reads market data through MassiveClient (grouped-daily bars for
the whole US market, ticker-details for a float proxy, news+sentiment for a
catalyst) and produces a ranked shares WATCHLIST in the Ross Cameron /
Warrior-Trading "5 Pillars" style. It never places, reviews, or cancels an
order and never touches a trading gate, the crypto lane, or the equities order
path.

HONESTY: the free Massive tier is END-OF-DAY, so this scans the PRIOR completed
session's leaders -- a morning watchlist, not a live pre-market gapper scan.

The 5 Pillars, and where each is applied:
  1. Big move        -- daily % change vs prior close     (market-wide filter)
  2. High rel. volume-- today vol / rolling avg baseline   (market-wide filter)
  3. Price range      -- price within [price_min, price_max](market-wide filter)
  4. Low float        -- shares-outstanding PROXY <= max    (shortlist enrich)
  5. News catalyst    -- a strong recent headline is a PLUS (shortlist enrich)

Pillars 1-3 are computed across every US ticker from grouped-daily bars in a
handful of calls; only the survivors (the shortlist) cost a per-ticker
float/news call, which keeps the run inside the rate-limited free tier.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Sequence

from ..equity_intelligence import summarize_news
from .levels import Levels, compute_levels


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# Pillar labels, used in the reasoning line and the email badges.
PILLAR_BIG_MOVE = "big move"
PILLAR_RVOL = "high RVOL"
PILLAR_PRICE = "price range"
PILLAR_LOW_FLOAT = "low float"
PILLAR_CATALYST = "catalyst"


@dataclass(frozen=True)
class ScoutPick:
    """One ranked watchlist name. Levels are on the underlying SHARES."""

    symbol: str
    last_price: float
    pct_change: float  # percent vs prior close
    volume: float
    rvol: float
    baseline_days: int
    # Pillar 4 -- a FLOAT PROXY, not true free float.
    float_shares: float | None
    float_basis: str | None
    float_known: bool
    market_cap: float | None
    # Pillar 5 -- catalyst.
    news_score: float  # [-1, 1], + = bullish
    catalyst: str | None  # a recent headline title
    news_citation: str
    levels: Levels | None
    pillars: tuple[str, ...]
    rank_score: float
    reasoning: str


def _collect_sessions(
    client: Any, today: date, count: int, max_lookback: int
) -> list[tuple[str, dict[str, Any]]]:
    """Most-recent-first list of up to `count` completed sessions, each a
    (session_iso, {ticker: Bar}) pair. Walks back from `today`, skipping
    weekends without a call and any day the vendor returns nothing for (which is
    how market holidays are handled without a holiday calendar)."""
    sessions: list[tuple[str, dict[str, Any]]] = []
    day = today
    scanned = 0
    while len(sessions) < count and scanned < max_lookback:
        scanned += 1
        current = day
        day = day - timedelta(days=1)
        if current.weekday() >= 5:
            continue
        try:
            rows = client.get_grouped_daily(current.isoformat())
        except Exception:
            continue
        if not rows:
            continue
        by_ticker = {bar.ticker: bar for bar in rows if getattr(bar, "ticker", None)}
        if by_ticker:
            sessions.append((current.isoformat(), by_ticker))
    return sessions


def _series_for(symbol: str, sessions: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    """Chronological (oldest-first) run of a symbol's bars across the window."""
    return [by_ticker[symbol] for _iso, by_ticker in reversed(sessions) if symbol in by_ticker]


def _news_for(
    client: Any, symbol: str, news_cfg: dict[str, Any], now: datetime
) -> tuple[float, str | None, str]:
    """Reuse summarize_news. Returns (score in [-1,1], catalyst title, citation)."""
    if not news_cfg.get("enabled", True):
        return 0.0, None, "news catalyst disabled"
    try:
        items = client.get_ticker_news(symbol, limit=int(news_cfg.get("max_articles", 20)))
    except Exception as exc:  # a news hiccup must not crash the scan
        return 0.0, None, f"news unavailable ({type(exc).__name__})"
    snap = summarize_news(symbol, items, news_cfg, now=now)
    if not snap.rated:
        return 0.0, None, snap.citation()
    score = (snap.positive - snap.negative) / snap.rated
    title = snap.latest_headline.title if snap.latest_headline else None
    return _clip(score, -1.0, 1.0), title, snap.citation()


def scan(
    client: Any,
    config: dict[str, Any],
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> list[ScoutPick]:
    """Run the market-wide scan and return the top-N ranked watchlist names."""
    today = today or datetime.now(UTC).date()
    now = now or datetime.now(UTC)

    min_gap_pct = float(config.get("min_gap_pct", 10.0))
    price_min = float(config.get("price_min", 1.0))
    price_max = float(config.get("price_max", 20.0))
    min_rvol = float(config.get("min_rvol", 5.0))
    baseline_days = int(config.get("rvol_baseline_days", 20))
    min_baseline = int(config.get("rvol_min_baseline_days", 5))
    max_float = float(config.get("max_float", 20_000_000))
    float_required = bool(config.get("float_required", True))
    shortlist_max = int(config.get("shortlist_max", 40))
    top_n = int(config.get("top_n", 10))
    news_cfg = config.get("news", {}) or {}
    levels_cfg = config.get("levels", {}) or {}

    # scan day + baseline_days prior sessions (prev close = the session at [1]).
    want = baseline_days + 1
    sessions = _collect_sessions(client, today, want, max_lookback=want + 20)
    if len(sessions) < 2:
        return []  # not enough completed sessions to compute a move or a baseline

    _scan_iso, today_map = sessions[0]
    prior = sessions[1:]  # oldest-toward-newest ordering is not required for a mean
    prev_map = sessions[1][1]

    # --- pillars 1-3, market-wide -------------------------------------------
    prelim: list[tuple[float, str, dict[str, Any]]] = []
    for symbol, bar in today_map.items():
        price = float(bar.close)
        if price <= 0 or not (price_min <= price <= price_max):
            continue  # pillar 3
        prev_bar = prev_map.get(symbol)
        if prev_bar is None or float(prev_bar.close) <= 0:
            continue
        prev_close = float(prev_bar.close)
        pct_change = (price - prev_close) / prev_close * 100.0
        if pct_change < min_gap_pct:
            continue  # pillar 1
        baseline_vols = [
            float(m[symbol].volume)
            for _iso, m in prior
            if symbol in m and float(m[symbol].volume) > 0
        ]
        if len(baseline_vols) < min_baseline:
            continue  # too little history for an honest RVOL
        avg_vol = sum(baseline_vols) / len(baseline_vols)
        if avg_vol <= 0:
            continue
        rvol = float(bar.volume) / avg_vol
        if rvol < min_rvol:
            continue  # pillar 2
        prelim.append(
            (
                pct_change * rvol,  # preliminary momentum score for shortlisting
                symbol,
                {
                    "price": price,
                    "pct_change": pct_change,
                    "volume": float(bar.volume),
                    "rvol": rvol,
                    "baseline_days": len(baseline_vols),
                },
            )
        )

    prelim.sort(key=lambda row: row[0], reverse=True)
    shortlist = prelim[: max(shortlist_max, 0)]

    # --- pillars 4-5, per shortlisted name -----------------------------------
    enriched: list[dict[str, Any]] = []
    for _score, symbol, data in shortlist:
        float_shares: float | None = None
        float_basis: str | None = None
        market_cap: float | None = None
        try:
            details = client.get_ticker_details(symbol)
            float_shares = details.shares_outstanding
            float_basis = details.shares_basis
            market_cap = details.market_cap
        except Exception:
            details = None
        float_known = float_shares is not None
        if float_required and float_known and float_shares is not None and float_shares > max_float:
            continue  # pillar 4 (only drops when the float is KNOWN and too big)

        news_score, catalyst, news_citation = _news_for(client, symbol, news_cfg, now)
        levels = compute_levels(_series_for(symbol, sessions), levels_cfg)

        pillars = [PILLAR_BIG_MOVE, PILLAR_RVOL, PILLAR_PRICE]
        if float_known and float_shares is not None and float_shares <= max_float:
            pillars.append(PILLAR_LOW_FLOAT)
        if news_score > 0 and catalyst:
            pillars.append(PILLAR_CATALYST)

        enriched.append(
            {
                **data,
                "symbol": symbol,
                "float_shares": float_shares,
                "float_basis": float_basis,
                "float_known": float_known,
                "market_cap": market_cap,
                "news_score": news_score,
                "catalyst": catalyst,
                "news_citation": news_citation,
                "levels": levels,
                "pillars": tuple(pillars),
            }
        )

    if not enriched:
        return []

    # --- composite ranking ---------------------------------------------------
    weights = config.get("rank_weights", {}) or {}
    w_gap = float(weights.get("gap", 0.35))
    w_rvol = float(weights.get("rvol", 0.30))
    w_float = float(weights.get("float", 0.20))
    w_news = float(weights.get("news", 0.15))
    max_pct = max(row["pct_change"] for row in enriched) or 1.0
    max_rvol = max(row["rvol"] for row in enriched) or 1.0

    picks: list[ScoutPick] = []
    for row in enriched:
        gap_norm = row["pct_change"] / max_pct
        rvol_norm = row["rvol"] / max_rvol
        if row["float_known"] and row["float_shares"] is not None:
            float_bonus = 1.0 - _clip(row["float_shares"] / max_float, 0.0, 1.0)
        else:
            float_bonus = 0.5  # unknown float is neutral, neither rewarded nor punished
        news_bonus = _clip(row["news_score"], 0.0, 1.0)
        rank_score = (
            w_gap * gap_norm + w_rvol * rvol_norm + w_float * float_bonus + w_news * news_bonus
        )
        picks.append(
            ScoutPick(
                symbol=row["symbol"],
                last_price=round(row["price"], 2),
                pct_change=round(row["pct_change"], 2),
                volume=row["volume"],
                rvol=round(row["rvol"], 2),
                baseline_days=row["baseline_days"],
                float_shares=row["float_shares"],
                float_basis=row["float_basis"],
                float_known=row["float_known"],
                market_cap=row["market_cap"],
                news_score=row["news_score"],
                catalyst=row["catalyst"],
                news_citation=row["news_citation"],
                levels=row["levels"],
                pillars=row["pillars"],
                rank_score=round(rank_score, 4),
                reasoning=_reasoning(row),
            )
        )

    picks.sort(key=lambda p: (p.rank_score, p.pct_change), reverse=True)
    return picks[: max(top_n, 0)]


def _fmt_float(shares: float | None) -> str:
    if shares is None:
        return "unknown"
    if shares >= 1_000_000:
        return f"{shares / 1_000_000:.1f}M"
    if shares >= 1_000:
        return f"{shares / 1_000:.0f}K"
    return f"{shares:.0f}"


def _reasoning(row: dict[str, Any]) -> str:
    """One line naming the pillars this name hit."""
    parts = [
        f"+{row['pct_change']:.1f}% on the session",
        f"{row['rvol']:.1f}x relative volume",
        f"${row['price']:.2f} in range",
    ]
    if row["float_known"]:
        parts.append(f"~{_fmt_float(row['float_shares'])} shares outstanding (float proxy)")
    else:
        parts.append("float (shares-outstanding proxy) unknown")
    if row["news_score"] > 0 and row["catalyst"]:
        parts.append("with a positive recent catalyst")
    elif row["catalyst"]:
        parts.append("recent news present")
    pillars = ", ".join(row["pillars"])
    return f"Pillars hit: {pillars}. " + "; ".join(parts) + "."
