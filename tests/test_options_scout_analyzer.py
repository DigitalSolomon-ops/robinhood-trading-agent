from __future__ import annotations

import math
from datetime import date, datetime, timezone

import pytest

from src.equity_intelligence.massive_client import (
    Bar,
    NewsInsight,
    NewsItem,
    OptionContract,
)
from src.options_scout.analyzer import analyze_symbol, rank_plays, scout_plays
from src.options_scout.config import load_scout_config

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

    def __init__(self, closes_by_symbol, news=None, breadth=None, contracts=None):
        self.closes_by_symbol = closes_by_symbol
        self.news = news or []
        self.breadth = breadth if breadth is not None else _breadth_rows(60, 40)
        self.contracts = contracts

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
