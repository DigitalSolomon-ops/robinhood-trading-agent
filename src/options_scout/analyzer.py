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
from ..equity_intelligence.massive_client import OptionContract, OptionSnapshot
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
    # --- REAL contract data (Options plan snapshot). All optional: premium/OI
    # populate now, greeks/IV are None off market hours. Defaulted so a Play can
    # still be built when the snapshot endpoint returns nothing. ---------------
    premium: float | None = None  # per-share option price (x100 = one contract)
    premium_source: str | None = None  # "last_quote_midpoint" | "day_close"
    open_interest: float | None = None
    day_volume: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    implied_volatility: float | None = None
    contract_selection: str = "strike_distance"  # or "target_delta"

    @property
    def target(self) -> float:
        """The level the setup is trying to reach (ceiling for a call, floor for
        a put). The other level is the stop."""
        return self.ceiling if self.direction == "call" else self.floor

    @property
    def stop(self) -> float:
        return self.floor if self.direction == "call" else self.ceiling

    @property
    def has_greeks(self) -> bool:
        return self.delta is not None

    @property
    def cost_per_contract(self) -> float | None:
        """Premium x 100 (one US equity option controls 100 shares)."""
        return round(self.premium * 100.0, 2) if self.premium is not None else None

    @property
    def max_loss(self) -> float | None:
        """The defined risk of a LONG option is the premium paid, premium x 100.
        (This tool only ever frames long-option plays.)"""
        return self.cost_per_contract

    @property
    def breakeven(self) -> float | None:
        """Underlying breakeven at expiry: call strike + premium, put strike -
        premium. None until both strike and premium are known."""
        if self.premium is None or self.strike is None:
            return None
        if self.direction == "call":
            return round(self.strike + self.premium, 2)
        return round(self.strike - self.premium, 2)


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


def _nearest_expiry_contracts(
    client: Any,
    symbol: str,
    direction: str,
    target: float,
    today: date,
    horizon_days: int,
) -> list[OptionContract]:
    """The listed contracts of the earliest expiry on/after the horizon, within
    a strike band around the target. Reference metadata only (no price)."""
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
        return []
    contracts = [c for c in contracts if c.expiration_date and c.contract_type == direction]
    if not contracts:
        return []
    nearest_expiry = min(c.expiration_date for c in contracts)
    return [c for c in contracts if c.expiration_date == nearest_expiry]


def _safe_snapshot(client: Any, symbol: str, option_ticker: str) -> OptionSnapshot | None:
    """Fetch one contract snapshot, swallowing any error (a data hiccup, an
    entitlement gap, or a FakeClient without the method) so analysis never
    crashes on a missing snapshot."""
    getter = getattr(client, "get_option_snapshot", None)
    if getter is None:
        return None
    try:
        return getter(symbol, option_ticker)
    except Exception:
        return None


def _select_contract(
    client: Any,
    symbol: str,
    direction: str,
    target: float,
    today: date,
    horizon_days: int,
    config: dict[str, Any],
) -> tuple[OptionContract | None, OptionSnapshot | None, str]:
    """Choose the contract to surface and fetch its real snapshot.

    Selection: within the nearest-expiry group, scan the contracts closest to
    the target LEVEL by strike (bounded), fetch each snapshot, and -- WHEN greeks
    are available -- pick the one whose |delta| is nearest the configured target
    delta (default ~0.35 for the directional side). When no scanned contract has
    greeks (weekend / after-hours), fall back to the strike-distance nearest.

    Returns (contract, snapshot_for_that_contract, selection_method).
    """
    same_expiry = _nearest_expiry_contracts(client, symbol, direction, target, today, horizon_days)
    if not same_expiry:
        return None, None, "none"

    sel_cfg = config.get("option_selection", {}) or {}
    target_delta = abs(float(sel_cfg.get("target_delta", 0.35)))
    scan_max = max(1, int(sel_cfg.get("delta_scan_max_contracts", 8)))

    by_strike = sorted(same_expiry, key=lambda c: abs(c.strike_price - target))
    scan = by_strike[:scan_max]

    snaps: dict[str, OptionSnapshot] = {}
    for contract in scan:
        snap = _safe_snapshot(client, symbol, contract.ticker)
        if snap is not None:
            snaps[contract.ticker] = snap

    with_greeks = [
        (contract, snaps[contract.ticker])
        for contract in scan
        if contract.ticker in snaps and snaps[contract.ticker].has_greeks
    ]
    if with_greeks:
        chosen, snap = min(
            with_greeks, key=lambda cs: abs(abs(cs[1].delta) - target_delta)
        )
        return chosen, snap, "target_delta"

    # Fallback: greeks unavailable (weekend/after-hours) -> nearest strike to the
    # target level, with whatever snapshot (premium/OI) we did get for it.
    chosen = by_strike[0]
    return chosen, snaps.get(chosen.ticker), "strike_distance"


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

    contract, snapshot, selection = _select_contract(
        client, symbol, direction, ceiling if direction == "call" else floor,
        today, horizon_days, config,
    )

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
        premium=snapshot.premium if snapshot else None,
        premium_source=snapshot.premium_source if snapshot else None,
        open_interest=snapshot.open_interest if snapshot else None,
        day_volume=snapshot.day_volume if snapshot else None,
        delta=snapshot.delta if snapshot else None,
        gamma=snapshot.gamma if snapshot else None,
        theta=snapshot.theta if snapshot else None,
        vega=snapshot.vega if snapshot else None,
        implied_volatility=snapshot.implied_volatility if snapshot else None,
        contract_selection=selection if contract else "strike_distance",
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
