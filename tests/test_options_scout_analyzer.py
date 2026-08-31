from __future__ import annotations

import math
from datetime import date, datetime, timezone

import pytest

from src.equity_intelligence.massive_client import (
    Bar,
    NewsInsight,
    NewsItem,
    OptionContract,
    OptionSnapshot,
)
from src.options_scout.analyzer import (
    _select_contract,
    analyze_symbol,
    rank_plays,
    scout_plays,
)
from src.options_scout.config import load_scout_config


def _snap(
    ticker: str,
    *,
    delta=None,
    premium=5.25,
    premium_source="last_quote_midpoint",
    oi=4213.0,
    day_volume=1875.0,
    theta=None,
    iv=None,
) -> OptionSnapshot:
    return OptionSnapshot(
        contract_ticker=ticker,
        underlying_ticker="AAPL",
        premium=premium,
        premium_source=premium_source if premium is not None else None,
        open_interest=oi,
        day_volume=day_volume,
        day_close=premium,
        bid=None,
        ask=None,
        delta=delta,
        gamma=None,
        theta=theta,
        vega=None,
        implied_volatility=iv,
    )

UTC = timezone.utc


def _bars(closes: list[float]) -> list[Bar]:
    bars = []
    for i, c in enumerate(closes):
        bars.append(Bar(timestamp_ms=i, open=c, high=c * 1.008, low=c * 0.992, close=c, volume=1_000_000))
    return bars


def uptrend_closes(n: int = 260, base: float = 100.0, drift: float = 0.003) -> list[float]:
    return [base * (1.0 + drift) ** i + 0.5 * math.sin(i) for i in range(n)]


def downtrend_closes(n: int = 260, base: float = 300.0, drift: float = 0.003) -> list[float]:
    return [base * (1.0 - drift) ** i + 0.5 * math.sin(i) for i in range(n)]


def flat_closes(n: int = 260, base: float = 100.0) -> list[float]:
    return [base + 0.05 * math.sin(i) for i in range(n)]


class FakeClient:
    """Read-only stand-in: every method just returns canned analysis data.
    It has no order methods at all -- the analyzer cannot trade through it."""

    def __init__(self, closes_by_symbol, news=None, breadth=None, contracts=None, snapshots=None):
        self.closes_by_symbol = closes_by_symbol
        self.news = news or []
        self.breadth = breadth if breadth is not None else _breadth_rows(60, 40)
        self.contracts = contracts
        # snapshots: None (endpoint returns nothing), a dict keyed by contract
        # ticker, or a callable (underlying, ticker) -> OptionSnapshot|None.
        self.snapshots = snapshots

    def get_daily_bars(self, symbol, from_date, to_date, adjusted=True):
        return _bars(self.closes_by_symbol[symbol])

    def get_ticker_news(self, symbol, limit=20, **kwargs):
        return self.news

    def get_grouped_daily(self, date_str, **kwargs):
        return self.breadth

    def get_option_contracts(self, underlying, contract_type=None, expiration_gte=None, **kwargs):
        if self.contracts is not None:
            return self.contracts
        # Two expiries, several strikes -- lets the picker choose.
        out = []
        for exp in ("2026-09-18", "2026-10-16"):
            for strike in (90, 100, 105, 110, 120, 130):
                out.append(
                    OptionContract(
                        ticker=f"O:{underlying}{exp.replace('-', '')}{(contract_type or 'call')[0].upper()}{int(strike)}",
                        underlying_ticker=underlying,
                        contract_type=contract_type or "call",
                        strike_price=float(strike),
                        expiration_date=exp,
                    )
                )
        return out

    def get_option_snapshot(self, underlying, option_ticker):
        if self.snapshots is None:
            return None
        if callable(self.snapshots):
            return self.snapshots(underlying, option_ticker)
        return self.snapshots.get(option_ticker)


def _breadth_rows(adv: int, dec: int):
    rows = []
    for _ in range(adv):
        rows.append(Bar(timestamp_ms=0, open=10.0, high=11.0, low=9.0, close=11.0, volume=1))
    for _ in range(dec):
        rows.append(Bar(timestamp_ms=0, open=10.0, high=11.0, low=9.0, close=9.0, volume=1))
    return rows


TODAY = date(2026, 8, 30)
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def base_config():
    cfg = load_scout_config()
    assert cfg, "config/options_scout.yaml should load"
    return cfg


def test_uptrend_yields_a_call_with_ordered_levels():
    cfg = base_config()
    client = FakeClient({"AAPL": uptrend_closes()})
    play = analyze_symbol(client, "AAPL", cfg, today=TODAY, now=NOW)

    assert play is not None
    assert play.direction == "call"
    # Geometry: ceiling above the reference close, floor below it.
    assert play.ceiling > play.reference_close > play.floor
    # For a call, the target IS the ceiling and the stop the floor.
    assert play.target == play.ceiling
    assert play.stop == play.floor
    assert 0.0 <= play.conviction <= 100.0
    assert play.expected_move_pct > 0.0
    # A contract near the target was chosen from the reference list.
    assert play.contract_ticker and play.contract_ticker.startswith("O:AAPL")
    assert play.strike is not None
    # Rationale names the actual signals.
    for token in ("Trend:", "Momentum:", "RSI(14)", "expected move", "Backtest:"):
        assert token in play.rationale


def test_downtrend_yields_a_put_with_target_below():
    cfg = base_config()
    client = FakeClient({"SPY": downtrend_closes()})
    play = analyze_symbol(client, "SPY", cfg, today=TODAY, now=NOW)

    assert play is not None
    assert play.direction == "put"
    # For a put the target is the FLOOR (downside) and the stop the ceiling.
    assert play.target == play.floor
    assert play.stop == play.ceiling
    assert play.ceiling > play.reference_close > play.floor


def test_flat_series_has_no_clean_setup():
    cfg = base_config()
    client = FakeClient({"MSFT": flat_closes()})
    assert analyze_symbol(client, "MSFT", cfg, today=TODAY, now=NOW) is None


def test_insufficient_history_returns_none():
    cfg = base_config()
    client = FakeClient({"NVDA": uptrend_closes(n=120)})  # < sma_long + 5
    assert analyze_symbol(client, "NVDA", cfg, today=TODAY, now=NOW) is None


def test_positive_news_lifts_conviction_versus_neutral():
    cfg = base_config()
    closes = uptrend_closes()
    neutral = FakeClient({"AAPL": closes}, news=[])
    positive_news = [
        NewsItem(
            id="n1",
            title="AAPL blows past estimates",
            published_utc=NOW.isoformat(),
            article_url="https://example.com/a",
            tickers=("AAPL",),
            insights=(NewsInsight(ticker="AAPL", sentiment="positive", sentiment_reasoning="beat"),),
        )
    ]
    bullish = FakeClient({"AAPL": closes}, news=positive_news)

    base = analyze_symbol(neutral, "AAPL", cfg, today=TODAY, now=NOW)
    lifted = analyze_symbol(bullish, "AAPL", cfg, today=TODAY, now=NOW)
    assert lifted.conviction >= base.conviction
    assert lifted.news_score > 0.0


def test_rank_plays_orders_by_rank_score_and_truncates():
    cfg = base_config()
    client = FakeClient(
        {"AAPL": uptrend_closes(), "SPY": downtrend_closes(), "MSFT": uptrend_closes(base=50.0)}
    )
    plays = scout_plays(client, {**cfg, "universe": ["AAPL", "SPY", "MSFT"], "top_n": 2}, today=TODAY, now=NOW)
    assert len(plays) == 2
    assert plays[0].rank_score >= plays[1].rank_score


def test_scout_reads_regime_once_and_shares_it():
    cfg = base_config()

    class CountingClient(FakeClient):
        grouped_calls = 0

        def get_grouped_daily(self, date_str, **kwargs):
            type(self).grouped_calls += 1
            return super().get_grouped_daily(date_str, **kwargs)

    client = CountingClient({"AAPL": uptrend_closes(), "SPY": uptrend_closes(base=80.0)})
    scout_plays(client, {**cfg, "universe": ["AAPL", "SPY"]}, today=TODAY, now=NOW)
    # One completed session read for the whole run, not one per symbol.
    assert CountingClient.grouped_calls == 1


# --- real contract snapshot: selection + attachment -------------------------


def _delta_contracts():
    return [
        OptionContract(ticker="O:T210", underlying_ticker="AAPL", contract_type="call",
                       strike_price=210.0, expiration_date="2026-09-18"),
        OptionContract(ticker="O:T220", underlying_ticker="AAPL", contract_type="call",
                       strike_price=220.0, expiration_date="2026-09-18"),
        OptionContract(ticker="O:T230", underlying_ticker="AAPL", contract_type="call",
                       strike_price=230.0, expiration_date="2026-09-18"),
    ]


def test_select_contract_prefers_target_delta_when_greeks_present():
    """With greeks available, the ~0.35-delta contract is chosen even though a
    DIFFERENT strike sits nearest the target level -- proving delta selection is
    live, not strike-distance."""
    cfg = base_config()
    snaps = {
        "O:T210": _snap("O:T210", delta=0.55, premium=8.0),   # nearest strike to 211
        "O:T220": _snap("O:T220", delta=0.35, premium=4.0),   # nearest target delta
        "O:T230": _snap("O:T230", delta=0.18, premium=1.5),
    }
    client = FakeClient({"AAPL": uptrend_closes()}, contracts=_delta_contracts(), snapshots=snaps)

    contract, snap, method = _select_contract(client, "AAPL", "call", 211.0, TODAY, 10, cfg)

    assert method == "target_delta"
    assert contract.ticker == "O:T220"
    assert snap.delta == 0.35


def test_select_contract_falls_back_to_strike_distance_when_greeks_null():
    """Weekend/after-hours: no snapshot carries greeks, so selection reverts to
    the strike nearest the target level -- and the (greek-less) snapshot with
    premium/OI is still attached to the chosen contract."""
    cfg = base_config()
    snaps = {
        "O:T210": _snap("O:T210", delta=None, premium=8.0, premium_source="day_close"),
        "O:T220": _snap("O:T220", delta=None, premium=4.0, premium_source="day_close"),
        "O:T230": _snap("O:T230", delta=None, premium=1.5, premium_source="day_close"),
    }
    client = FakeClient({"AAPL": uptrend_closes()}, contracts=_delta_contracts(), snapshots=snaps)

    contract, snap, method = _select_contract(client, "AAPL", "call", 211.0, TODAY, 10, cfg)

    assert method == "strike_distance"
    assert contract.ticker == "O:T210"  # nearest strike to the 211 target level
    assert snap is not None and snap.premium == 8.0  # premium/OI still attached
    assert snap.has_greeks is False


def test_select_contract_survives_a_client_without_snapshot_support():
    """A client that returns no snapshot at all still yields a strike-distance
    pick (contract chosen, snapshot None) -- analysis never crashes."""
    cfg = base_config()
    client = FakeClient({"AAPL": uptrend_closes()}, contracts=_delta_contracts())  # snapshots=None

    contract, snap, method = _select_contract(client, "AAPL", "call", 211.0, TODAY, 10, cfg)

    assert method == "strike_distance"
    assert contract.ticker == "O:T210"
    assert snap is None


def test_analyze_symbol_attaches_real_contract_economics():
    """End-to-end: a play carries the real premium, cost/contract, max loss,
    open interest, breakeven and greeks pulled from the snapshot."""
    cfg = base_config()

    def snap_for(underlying, ticker):
        return _snap(ticker, delta=0.34, premium=5.25, oi=4213.0,
                     day_volume=1875.0, theta=-0.08, iv=0.28)

    client = FakeClient({"AAPL": uptrend_closes()}, snapshots=snap_for)
    play = analyze_symbol(client, "AAPL", cfg, today=TODAY, now=NOW)

    assert play is not None
    assert play.premium == 5.25
    assert play.cost_per_contract == 525.0
    assert play.max_loss == 525.0
    assert play.open_interest == 4213.0
    assert play.day_volume == 1875.0
    assert play.has_greeks is True
    assert play.delta == 0.34
    assert play.contract_selection == "target_delta"
    assert play.strike is not None
    assert play.breakeven == round(play.strike + 5.25, 2)


def test_analyze_symbol_handles_weekend_null_greeks_gracefully():
    """A weekend snapshot (premium/OI present, greeks None) still produces a
    play: premium and breakeven compute, has_greeks is False, no crash."""
    cfg = base_config()

    def snap_for(underlying, ticker):
        return _snap(ticker, delta=None, premium=5.10, premium_source="day_close",
                     oi=4213.0, day_volume=1875.0)

    client = FakeClient({"AAPL": uptrend_closes()}, snapshots=snap_for)
    play = analyze_symbol(client, "AAPL", cfg, today=TODAY, now=NOW)

    assert play is not None
    assert play.premium == 5.10
    assert play.has_greeks is False
    assert play.delta is None
    assert play.contract_selection == "strike_distance"
    assert play.breakeven == round(play.strike - 5.10, 2) if play.direction == "put" \
        else round(play.strike + 5.10, 2)
