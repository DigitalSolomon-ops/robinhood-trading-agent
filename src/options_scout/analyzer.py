"""The Options Scout analyzer: thesis, levels, conviction, contract, ranking.

ANALYSIS ONLY. Reads market data through MassiveClient (daily bars, news,
grouped-daily breadth, options-contract REFERENCE) and produces ranked CANDIDATE
plays. It never places, reviews, or cancels an order and never touches a trading
gate. Every price level is on the UNDERLYING -- the free data tier does not
authorize option quotes/greeks/IV, so the reader maps the level onto the listed
contract.

Reuses the equities-lane analysis helpers where they fit:
  * equity_intelligence.summarize_news  -> per-symbol news sentiment
  * equity_intelligence.summarize_breadth -> market-wide breadth / regime
The directional indicators are computed locally (options_scout.indicators) so
the LIVE thesis and the HISTORICAL backtest share one identical rule -- see
indicators.py and backtest.py for why that matters.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ..equity_intelligence import summarize_breadth, summarize_news
from ..equity_intelligence.massive_client import OptionContract
from .backtest import HitRate, backtest_setup
from .indicators import Factor, build_series, directional_score_at, realized_vol_at


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class Play:
    """One ranked candidate play. Levels are on the UNDERLYING."""

    symbol: str
    direction: str  # "call" | "put"
    reference_close: float
    entry: float
    ceiling: float
    floor: float
    expected_move_pct: float
    horizon_days: int
    # Contract reference (may be blank when the reference API returns nothing).
    strike: float | None
    expiry_date: str | None
    contract_ticker: str | None
    # Scoring
    conviction: float  # 0..100
    factors: tuple[Factor, ...]
    news_score: float  # [-1, 1], + = bullish
    news_citation: str
    regime_ratio: float  # advancers / moved, 0..1
    regime_note: str
    hit_rate: HitRate
    rank_score: float
    rationale: str

    @property
    def target(self) -> float:
        """The level the setup is trying to reach (ceiling for a call, floor for
        a put). The other level is the stop."""
        return self.ceiling if self.direction == "call" else self.floor

    @property
    def stop(self) -> float:
        return self.floor if self.direction == "call" else self.ceiling


def _news_score(client: Any, symbol: str, news_cfg: dict[str, Any], now: datetime) -> tuple[float, str]:
    """Reuse summarize_news. Returns (score in [-1,1], citation)."""
    if not news_cfg.get("enabled", True):
        return 0.0, "news adjustment disabled"
    try:
        items = client.get_ticker_news(symbol, limit=int(news_cfg.get("max_articles", 20)))
    except Exception as exc:  # a data feed hiccup must not crash analysis
        return 0.0, f"news unavailable ({type(exc).__name__})"
    snap = summarize_news(symbol, items, news_cfg, now=now)
    if not snap.rated:
        return 0.0, snap.citation()
    score = (snap.positive - snap.negative) / snap.rated
    return _clip(score, -1.0, 1.0), snap.citation()


def _regime(client: Any, regime_cfg: dict[str, Any], today: date) -> tuple[float, str]:
    """Reuse summarize_breadth over the most recent completed session.
    Returns (advance_ratio 0..1, note)."""
    if not regime_cfg.get("enabled", True):
        return 0.5, "regime adjustment disabled"
    # Walk back from yesterday until a session returns rows (skips weekends and
    # holidays without shipping a calendar), same shape as MassiveBreadthProvider.
    for back in range(1, 8):
        day = today - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        try:
            rows = client.get_grouped_daily(day.isoformat())
        except Exception as exc:
            return 0.5, f"breadth unavailable ({type(exc).__name__})"
        if rows:
            snap = summarize_breadth(rows, regime_cfg, day.isoformat())
            return snap.advance_ratio, snap.citation()
    return 0.5, "no completed session with breadth in the last week"


def _pick_contract(
    client: Any,
    symbol: str,
    direction: str,
    target: float,
    today: date,
    horizon_days: int,
) -> OptionContract | None:
    """Nearest listed contract to the target: earliest expiry on/after the
    horizon, then strike closest to the target level. Reference metadata only."""
    # trading days -> calendar days, plus a couple of days of slack.
    target_expiry = today + timedelta(days=math.ceil(horizon_days * 7 / 5) + 2)
    lo = round(target * 0.8, 2)
    hi = round(target * 1.2, 2)
    try:
        contracts = client.get_option_contracts(
            symbol,
            contract_type=direction,
            expiration_gte=target_expiry.isoformat(),
            strike_gte=lo,
            strike_lte=hi,
            limit=250,
        )
    except Exception:
        return None
    contracts = [c for c in contracts if c.expiration_date and c.contract_type == direction]
    if not contracts:
        return None
    nearest_expiry = min(c.expiration_date for c in contracts)
    same_expiry = [c for c in contracts if c.expiration_date == nearest_expiry]
    return min(same_expiry, key=lambda c: abs(c.strike_price - target))


def analyze_symbol(
    client: Any,
    symbol: str,
    config: dict[str, Any],
    *,
    regime_ratio: float | None = None,
    regime_note: str = "",
    today: date | None = None,
    now: datetime | None = None,
) -> Play | None:
    """Build one Play for `symbol`, or None when there is no clean setup / data."""
    today = today or datetime.now(UTC).date()
    now = now or datetime.now(UTC)

    ind_cfg = config.get("indicators", {}) or {}
    weights = config.get("weights", {}) or {}
    levels_cfg = config.get("levels", {}) or {}
    horizon_days = int(config.get("horizon_days", 10))
    history_days = int(config.get("history_days", 730))

    from_date = (today - timedelta(days=history_days)).isoformat()
    to_date = today.isoformat()
    try:
        bars = client.get_daily_bars(symbol, from_date, to_date)
    except Exception:
        return None
    if len(bars) < int(ind_cfg.get("sma_long", 200)) + 5:
        return None  # not enough history for the slow trend leg

    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    series = build_series(highs, lows, closes, ind_cfg)

    last = series.length - 1
    read = directional_score_at(series, last, weights)
    if read is None:
        return None
    min_abs = float(ind_cfg.get("min_abs_score", 0.15))
    if abs(read.score) < min_abs:
        return None  # no clean directional edge today

    direction = read.direction
    close = closes[last]
    atr_val = series.atr[last] or 0.0
    vol_lookback = int(levels_cfg.get("vol_lookback", 20))
    rv = realized_vol_at(closes, last, vol_lookback)
    if rv is None or rv <= 0:
        return None
    expected_move_pct = rv * math.sqrt(horizon_days)
    move = close * expected_move_pct
    stop_move = move * float(levels_cfg.get("stop_fraction", 0.6))
    band = atr_val * float(levels_cfg.get("entry_band_atr_fraction", 0.25))

    if direction == "call":
        ceiling = close + move
        floor = close - stop_move
    else:
        ceiling = close + stop_move
        floor = close - move
    entry = close  # entry zone center; band reported in rationale

    # Context: news sentiment + market regime.
    news_cfg = config.get("news", {}) or {}
    news_score, news_citation = _news_score(client, symbol, news_cfg, now)
    if regime_ratio is None:
        regime_ratio, regime_note = _regime(client, config.get("regime", {}) or {}, today)

    # Conviction: base = |score|*100, nudged (never dominated) by aligned news
    # and breadth. Multipliers are bounded so context can only tilt the reading.
    base = abs(read.score) * 100.0
    news_w = float(weights.get("news_adjust", 0.15))
    regime_w = float(weights.get("regime_adjust", 0.15))
    aligned_news = news_score if direction == "call" else -news_score
    breadth_score = (regime_ratio - 0.5) * 2.0
    aligned_regime = breadth_score if direction == "call" else -breadth_score
    multiplier = _clip(1.0 + news_w * aligned_news + regime_w * aligned_regime, 0.5, 1.5)
    conviction = _clip(base * multiplier, 0.0, 100.0)

    # Backtest the SAME rule over history.
    bt_cfg = config.get("backtest", {}) or {}
    hit = backtest_setup(
        series,
        direction,
        horizon_days,
        vol_lookback,
        weights,
        int(config.get("min_occurrences", 10)),
        trigger_min_abs_score=float(bt_cfg.get("trigger_min_abs_score", min_abs)),
        fresh_edge_only=bool(bt_cfg.get("fresh_edge_only", True)),
    )

    contract = _pick_contract(client, symbol, direction, ceiling if direction == "call" else floor, today, horizon_days)

    rank_score = (conviction / 100.0) * hit.hit_rate * hit.confidence_weight

    rationale = _rationale(
        symbol, direction, series, last, entry, band, ceiling, floor,
        expected_move_pct, horizon_days, news_citation, regime_note, hit,
    )

    return Play(
        symbol=symbol,
        direction=direction,
        reference_close=round(close, 2),
        entry=round(entry, 2),
        ceiling=round(ceiling, 2),
        floor=round(floor, 2),
        expected_move_pct=expected_move_pct,
        horizon_days=horizon_days,
        strike=contract.strike_price if contract else None,
        expiry_date=contract.expiration_date if contract else None,
        contract_ticker=contract.ticker if contract else None,
        conviction=round(conviction, 1),
        factors=read.factors,
        news_score=news_score,
        news_citation=news_citation,
        regime_ratio=regime_ratio,
        regime_note=regime_note,
        hit_rate=hit,
        rank_score=rank_score,
        rationale=rationale,
    )


def _rationale(
    symbol: str,
    direction: str,
    series: Any,
    idx: int,
    entry: float,
    band: float,
    ceiling: float,
    floor: float,
    expected_move_pct: float,
    horizon_days: int,
    news_citation: str,
    regime_note: str,
    hit: HitRate,
) -> str:
    ef, es = series.ema_fast[idx], series.ema_slow[idx]
    sl, rsi_v, hist = series.sma_long[idx], series.rsi[idx], series.macd_hist[idx]
    close = series.closes[idx]
    trend = (
        f"EMA20 {'above' if ef >= es else 'below'} EMA50 and price "
        f"{'above' if close >= sl else 'below'} SMA200"
    )
    mom = f"MACD histogram {'positive' if hist >= 0 else 'negative'} ({hist:+.2f})"
    if rsi_v >= 70:
        rsi_desc = "overbought"
    elif rsi_v <= 30:
        rsi_desc = "oversold"
    elif rsi_v >= 50:
        rsi_desc = "above midline"
    else:
        rsi_desc = "below midline"
    target = ceiling if direction == "call" else floor
    stop = floor if direction == "call" else ceiling
    verb = "upside" if direction == "call" else "downside"
    return (
        f"{direction.upper()} on {symbol}. Trend: {trend}. Momentum: {mom}. "
        f"RSI(14) {rsi_v:.0f} ({rsi_desc}). Realized-vol expected move "
        f"~{expected_move_pct * 100:.1f}% over {horizon_days} trading days frames "
        f"a {verb} target near {target:.2f} with a stop near {stop:.2f}; entry zone "
        f"{entry - band:.2f}-{entry + band:.2f}. "
        f"News: {news_citation}. Regime: {regime_note}. "
        f"Backtest: {hit.summary()} (base rate, not a guarantee)."
    )


def rank_plays(plays: list[Play], top_n: int) -> list[Play]:
    """Rank by rank_score = (conviction) x (hit-rate x sample-size confidence),
    highest first, and keep the top N. Ties broken by raw conviction."""
    ordered = sorted(plays, key=lambda p: (p.rank_score, p.conviction), reverse=True)
    return ordered[: max(top_n, 0)]


def scout_plays(
    client: Any,
    config: dict[str, Any],
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> list[Play]:
    """Analyze the whole universe once and return the top-N ranked plays.

    The market regime is read ONCE (it is a property of the market, not the
    ticker) and shared across every symbol -- one grouped-daily read per run.
    """
    today = today or datetime.now(UTC).date()
    now = now or datetime.now(UTC)
    universe = config.get("universe") or []
    top_n = int(config.get("top_n", 5))

    regime_ratio, regime_note = _regime(client, config.get("regime", {}) or {}, today)

    plays: list[Play] = []
    for symbol in universe:
        play = analyze_symbol(
            client, symbol, config,
            regime_ratio=regime_ratio, regime_note=regime_note,
            today=today, now=now,
        )
        if play is not None:
            plays.append(play)
    return rank_plays(plays, top_n)
