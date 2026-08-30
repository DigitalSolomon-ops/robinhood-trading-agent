"""Massive ticker news + per-ticker sentiment as a PRE-TRADE RISK FILTER.

Covers the three things this feature has to be true about:

1. a strongly-negative-sentiment name is skipped (or downweighted) BEFORE the
   entry -- and only inside the configured recency window;
2. the filter can only BLOCK or REDUCE. It can never submit an order, never
   create a signal the rules did not produce, never raise a confidence, never
   block an exit, and never stand in for a risk gate, the kill switch or the
   human confirm-flag;
3. every filtered decision's rationale cites the sentiment AND the headline,
   and every threshold (including the window) comes from config.

The Massive client is mocked throughout (httpx.MockTransport) -- no test in
this file opens a socket.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from src.equity_intelligence.massive_client import MassiveClient, NewsInsight, NewsItem
from src.equity_intelligence.news_sentiment import (
    ALLOW,
    DOWNWEIGHT,
    NOT_APPLICABLE,
    SKIP,
    UNAVAILABLE,
    MassiveNewsSentimentProvider,
    SentimentSnapshot,
    build_sentiment_provider,
    evaluate_news_sentiment,
    news_sentiment_config,
    summarize_news,
)
from src.kill_switch import KillSwitch
from src.portfolio import Portfolio
from src.risk_manager import RiskManager
from src.robinhood_equity_broker import RobinhoodEquityBroker
from src.robinhood_equity_client import RobinhoodEquityClient
from src.strategy_engine import StrategyEngine

SYMBOL = "AAPL"
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


# --- shared fixtures ---------------------------------------------------------


def article(title: str, sentiment: str, hours_ago: float, ticker: str = SYMBOL, reasoning: str | None = None) -> NewsItem:
    published = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return NewsItem(
        id=f"{ticker}-{hours_ago}",
        title=title,
        published_utc=published,
        article_url=f"https://news.example/{ticker}/{hours_ago}",
        tickers=(ticker,),
        insights=(NewsInsight(ticker=ticker, sentiment=sentiment, sentiment_reasoning=reasoning),),
    )


def strategy_config() -> dict:
    """Indicators deliberately absent -- this file isolates the news filter."""
    return {
        "equity_strategy": {"active_profile": "equities_core_test", "minimum_history_points": 50},
        "equity_profiles": {
            "equities_core_test": {
                "buy_when": ["ema20_above_ema50", "rsi_between_35_and_70", "momentum_5_positive"],
                "sell_when": ["rsi_above_75", "momentum_5_negative", "stop_loss_hit", "take_profit_hit"],
            }
        },
    }


def trading_rules(symbol: str = SYMBOL, **news_overrides) -> dict:
    news = {
        "enabled": True,
        "recency_hours": 48,
        "min_rated_articles": 2,
        "strongly_negative_ratio": 0.6,
        "negative_ratio": 0.34,
        "on_strongly_negative": "skip_entry",
        "on_negative": "downweight",
        "strongly_negative_confidence_multiplier": 0.4,
        "negative_confidence_multiplier": 0.6,
        "min_confidence_to_act": 0.0,
    }
    news.update(news_overrides)
    return {
        "trading": {"enabled": True, "mode": "paper", "allowed_symbols": [symbol]},
        "risk": {
            "max_trade_amount_usd": 1000.0,
            "max_daily_loss_usd": 1000.0,
            "max_open_positions": 10,
            "max_trades_per_day": 50,
            "min_order_cooldown_seconds": 0,
            "require_cash_available": True,
            "allow_position_scaling": False,
            "allow_margin": False,
            "allow_shorts": False,
        },
        "orders": {"require_stop_loss": True, "require_take_profit": True},
        "exits": {"stop_loss_percent": 2.0, "take_profit_percent": 4.0},
        "equities": {"universe": [symbol], "news_sentiment": news},
    }


def uptrend_prices(count: int = 60) -> list[float]:
    """The series test_equity_strategy_profile.py proves yields a rules BUY."""
    return [100 + i * 0.03 + ((-1) ** i) * 0.05 for i in range(count)]


def downtrend_prices(count: int = 60) -> list[float]:
    return [100 - i * 0.05 for i in range(count)]


def engine(**news_overrides) -> StrategyEngine:
    return StrategyEngine(strategy_config(), trading_rules(**news_overrides))


def snapshot_from(items: list[NewsItem], config: dict | None = None) -> SentimentSnapshot:
    return summarize_news(SYMBOL, items, config or news_sentiment_config(trading_rules()), now=NOW)


BAD_NEWS = [
    article("Regulator opens probe into flagship product", "negative", 3, reasoning="an antitrust probe is a material overhang"),
    article("Supplier warns on holiday demand", "negative", 9),
    article("Analyst cuts price target after guidance miss", "negative", 20),
    article("Board reiterates buyback", "positive", 30),
]

MIXED_NEWS = [
    article("Q3 revenue misses consensus", "negative", 5, reasoning="a top-line miss pressures the multiple"),
    article("New device ships to strong reviews", "positive", 8),
    article("Analysts split on the quarter", "neutral", 12),
    article("Component shortage flagged by supplier", "negative", 14),
    article("Dividend raised", "positive", 20),
]

GOOD_NEWS = [
    article("Record quarterly revenue", "positive", 2),
    article("Upgraded to overweight", "positive", 6),
    article("New buyback authorized", "positive", 18),
]


# --- 1. thresholds and the recency window come from config -------------------


def test_defaults_are_overridden_key_by_key_not_wholesale():
    config = news_sentiment_config({"equities": {"news_sentiment": {"recency_hours": 6}}})

    assert config["recency_hours"] == 6
    # The keys the override did not mention survive from the defaults.
    assert config["max_articles"] == 20
    assert config["strongly_negative_ratio"] == 0.6
    assert config["on_strongly_negative"] == "skip_entry"
    assert config["negative_labels"] == ["negative"]


def test_the_shipped_config_is_read_and_both_skip_spellings_are_honoured():
    """The repo's own config/trading_rules.yaml drives the filter, and the
    `skip`/`skip_entry` synonyms mean the same thing (the shipped config uses
    skip_entry so it cannot poison the order-symbol guard's vocabulary)."""
    import yaml

    shipped = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "trading_rules.yaml").read_text(encoding="utf-8")
    )
    config = news_sentiment_config(shipped)

    assert config["enabled"] is True
    assert config["on_strongly_negative"] == "skip_entry"
    assert evaluate_news_sentiment(snapshot_from(BAD_NEWS, config), "buy", 0.6, config).action == SKIP

    legacy = {**config, "on_strongly_negative": "skip"}
    assert evaluate_news_sentiment(snapshot_from(BAD_NEWS, legacy), "buy", 0.6, legacy).action == SKIP


def test_absent_config_section_yields_disabled_defaults():
    config = news_sentiment_config({})

    assert config["enabled"] is False
    assert build_sentiment_provider({}) is None
    assert build_sentiment_provider({"equities": {}}) is None


def test_thresholds_come_from_config_not_from_code():
    """Same headlines, two configs, opposite verdicts. If any band were
    hardcoded, one of these two assertions would fail."""
    strict = engine(strongly_negative_ratio=0.3)
    permissive = engine(strongly_negative_ratio=0.99, negative_ratio=0.99)
    news = snapshot_from(MIXED_NEWS)  # 2 of 5 rated negative -> ratio 0.40

    skipped = strict.generate_equity_signal(SYMBOL, uptrend_prices(), news=news)
    allowed = permissive.generate_equity_signal(SYMBOL, uptrend_prices(), news=news)

    assert skipped.side == "hold"
    assert skipped.sentiment_action == SKIP
    assert allowed.side == "buy"
    assert allowed.sentiment_action == ALLOW


def test_the_recency_window_is_config_driven_and_excludes_older_news():
    """A damning story outside the window is not 'recent sentiment'. The only
    difference between these two reads is recency_hours."""
    stale_disaster = [
        article("Accounting irregularities alleged", "negative", 100),
        article("CFO resigns amid inquiry", "negative", 96),
        article("Auditor withdraws opinion", "negative", 92),
        article("Steady quarter", "positive", 4),
        article("Product refresh well received", "positive", 6),
    ]

    inside_48h = snapshot_from(stale_disaster, news_sentiment_config(trading_rules(recency_hours=48)))
    inside_a_week = snapshot_from(stale_disaster, news_sentiment_config(trading_rules(recency_hours=168)))

    assert (inside_48h.rated, inside_48h.negative) == (2, 0)
    assert (inside_a_week.rated, inside_a_week.negative) == (5, 3)
    assert engine(recency_hours=48).generate_equity_signal(SYMBOL, uptrend_prices(), news=inside_48h).side == "buy"
    assert engine(recency_hours=168).generate_equity_signal(SYMBOL, uptrend_prices(), news=inside_a_week).side == "hold"


def test_undated_and_other_ticker_insights_are_not_counted():
    """An article with no timestamp cannot be placed in the window, and an
    article rated for a DIFFERENT ticker is not a reading about this one."""
    items = [
        NewsItem(id="1", title="Undated doom", published_utc=None, article_url=None, tickers=(SYMBOL,),
                 insights=(NewsInsight(ticker=SYMBOL, sentiment="negative", sentiment_reasoning=None),)),
        article("Rival stumbles", "negative", 2, ticker="MSFT"),
        article("Solid print", "positive", 3),
    ]

    reading = snapshot_from(items)

    assert reading.rated == 1
    assert reading.negative == 0
    assert reading.articles_in_window == 2  # the MSFT piece is in-window but unrated for AAPL


def test_negative_labels_are_config_driven():
    """A vendor relabelling is a config change, not a code change."""
    items = [article("Downbeat outlook", "bearish", 2), article("Cautious tone", "bearish", 5)]
    config = news_sentiment_config(trading_rules(negative_labels=["bearish"]))

    reading = summarize_news(SYMBOL, items, config, now=NOW)
    result = evaluate_news_sentiment(reading, "buy", 0.6, config)

    assert reading.negative == 2
    assert result.action == SKIP


# --- 2. a strongly-negative name is skipped, a mildly negative one downweighted


def test_strongly_negative_name_is_skipped_before_entry():
    signal = engine().generate_equity_signal(SYMBOL, uptrend_prices(), news=snapshot_from(BAD_NEWS))

    assert signal.strategy_signal == "buy", "the rules alone would have entered"
    assert signal.side == "hold"
    assert signal.final_signal == "hold"
    assert signal.sentiment_action == SKIP
    assert signal.confidence == 0.0
    # A skipped entry carries no exit levels -- there is no position to exit.
    assert signal.stop_loss_percent is None
    assert signal.take_profit_percent is None


def test_moderately_negative_name_is_downweighted_not_skipped():
    signal = engine().generate_equity_signal(SYMBOL, uptrend_prices(), news=snapshot_from(MIXED_NEWS))

    assert signal.side == "buy"
    assert signal.sentiment_action == DOWNWEIGHT
    assert signal.confidence == pytest.approx(signal.pre_sentiment_confidence * 0.6, rel=1e-6)
    assert signal.confidence < signal.pre_sentiment_confidence


def test_strongly_negative_can_be_configured_to_downweight_instead_of_skip():
    lenient = engine(on_strongly_negative="downweight", strongly_negative_confidence_multiplier=0.25)

    signal = lenient.generate_equity_signal(SYMBOL, uptrend_prices(), news=snapshot_from(BAD_NEWS))

    assert signal.side == "buy"
    assert signal.sentiment_action == DOWNWEIGHT
    assert signal.confidence == pytest.approx(signal.pre_sentiment_confidence * 0.25, rel=1e-6)


def test_confidence_floor_turns_a_heavily_downweighted_entry_into_a_hold():
    weak = engine(on_strongly_negative="downweight", strongly_negative_confidence_multiplier=0.4, min_confidence_to_act=0.5)

    signal = weak.generate_equity_signal(SYMBOL, uptrend_prices(), news=snapshot_from(BAD_NEWS))

    assert signal.side == "hold"
    assert signal.sentiment_action == SKIP
    assert "below min_confidence_to_act" in signal.reason


def test_thin_coverage_stands_down_rather_than_guessing():
    """One negative article is 100% negative and means nothing. Below
    min_rated_articles the filter says so and does not act."""
    signal = engine().generate_equity_signal(SYMBOL, uptrend_prices(), news=snapshot_from([article("Lone bear note", "negative", 2)]))

    assert signal.side == "buy"
    assert signal.sentiment_action == ALLOW
    assert "below min_rated_articles 2" in signal.reason


def test_clean_news_leaves_the_entry_untouched():
    signal = engine().generate_equity_signal(SYMBOL, uptrend_prices(), news=snapshot_from(GOOD_NEWS))

    assert signal.side == "buy"
    assert signal.sentiment_action == ALLOW
    assert signal.confidence == signal.pre_sentiment_confidence
    assert "no negative-news veto" in signal.reason


# --- 3. the rationale cites the sentiment AND the headline -------------------


def test_skip_rationale_cites_the_sentiment_the_headline_and_the_rules_signal():
    signal = engine().generate_equity_signal(SYMBOL, uptrend_prices(), news=snapshot_from(BAD_NEWS))

    for fragment in (
        "massive_news",
        "3/4 rated articles negative",
        "(75%)",
        "last 48h",
        'most recent negative: "Regulator opens probe into flagship product"',
        "[negative, 2026-08-30T09:00:00Z",
        "negative share 0.75 at/above strongly_negative_ratio 0.60",
        "entry skipped",
        # The rules verdict it overrode is still legible in the same sentence.
        "rules signal was 'buy'",
    ):
        assert fragment in signal.reason, f"{fragment!r} missing from rationale: {signal.reason}"
    # The vendor's own reasoning rides along when it supplied one.
    assert "an antitrust probe is a material overhang" in signal.reason


def test_downweight_rationale_cites_the_headline_and_the_confidence_change():
    signal = engine().generate_equity_signal(SYMBOL, uptrend_prices(), news=snapshot_from(MIXED_NEWS))

    assert '"Q3 revenue misses consensus"' in signal.reason
    assert "2/5 rated articles negative" in signal.reason
    assert "downweighted (x0.60)" in signal.reason
    assert f"confidence {signal.pre_sentiment_confidence:.2f} -> {signal.confidence:.2f}" in signal.reason


def test_the_structured_fields_carry_the_same_citation_as_the_prose():
    signal = engine().generate_equity_signal(SYMBOL, uptrend_prices(), news=snapshot_from(BAD_NEWS))

    assert signal.sentiment_source == "massive_news"
    assert dict(signal.sentiment_counts) == {
        "articles_in_window": 4,
        "rated": 4,
        "negative": 3,
        "neutral": 0,
        "positive": 1,
    }
    assert signal.sentiment_negative_ratio == pytest.approx(0.75)
    assert "Regulator opens probe into flagship product" in signal.sentiment_headline
    assert "negative" in signal.sentiment_headline


# --- the news feed degrades, it does not crash or arm ------------------------


def test_unreadable_news_passes_the_rules_signal_through_by_default():
    signal = engine().generate_equity_signal(
        SYMBOL, uptrend_prices(), news=SentimentSnapshot(symbol=SYMBOL, error="HTTPError: 503")
    )

    assert signal.side == "buy"
    assert signal.sentiment_action == UNAVAILABLE
    assert "news sentiment unavailable" in signal.reason
    assert "503" in signal.reason


def test_require_news_fails_closed_when_the_data_is_missing():
    strict = engine(require_news=True)

    from_error = strict.generate_equity_signal(
        SYMBOL, uptrend_prices(), news=SentimentSnapshot(symbol=SYMBOL, error="HTTPError: 503")
    )
    from_nothing = strict.generate_equity_signal(SYMBOL, uptrend_prices())
    from_thin = strict.generate_equity_signal(SYMBOL, uptrend_prices(), news=snapshot_from(GOOD_NEWS[:1]))

    assert from_error.side == "hold" and from_error.sentiment_action == SKIP
    assert from_nothing.side == "hold" and from_nothing.sentiment_action == SKIP
    assert "required but unavailable" in from_nothing.reason
    assert from_thin.side == "hold" and "require_news" in from_thin.reason


def test_a_provider_that_raises_degrades_to_an_unfiltered_signal():
    class ExplodingProvider:
        def snapshot(self, symbol: str) -> SentimentSnapshot:
            raise RuntimeError("massive news is down")

    signal = engine().generate_equity_signal(SYMBOL, uptrend_prices(), sentiment_provider=ExplodingProvider())

    assert signal.side == "buy"
    assert signal.sentiment_action == UNAVAILABLE
    assert "massive news is down" in signal.reason


def test_omitting_news_entirely_leaves_the_signal_untouched():
    """Back-compat: the rules-only path is unchanged when no news is supplied
    and none is required."""
    signal = engine().generate_equity_signal(SYMBOL, uptrend_prices())

    assert signal.side == "buy"
    assert signal.sentiment_action == ""
    assert signal.sentiment_counts == ()
    assert "news_sentiment" not in signal.reason


# --- the provider, against the mocked Massive client -------------------------


def test_provider_reads_ticker_news_from_the_mocked_massive_client():
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "a",
                        "title": "Regulator opens probe into flagship product",
                        "published_utc": (NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "article_url": "https://news.example/a",
                        "tickers": [SYMBOL],
                        "insights": [
                            {"ticker": SYMBOL, "sentiment": "negative", "sentiment_reasoning": "probe is an overhang"}
                        ],
                    },
                    {
                        "id": "b",
                        "title": "Supplier warns on demand",
                        "published_utc": (NOW - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "article_url": "https://news.example/b",
                        "tickers": [SYMBOL],
                        "insights": [{"ticker": SYMBOL, "sentiment": "negative", "sentiment_reasoning": None}],
                    },
                    {
                        "id": "c",
                        "title": "Ancient good news",
                        "published_utc": (NOW - timedelta(hours=500)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "article_url": "https://news.example/c",
                        "tickers": [SYMBOL],
                        "insights": [{"ticker": SYMBOL, "sentiment": "positive", "sentiment_reasoning": None}],
                    },
                ]
            },
        )

    client = MassiveClient(api_key="test-key", transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    provider = MassiveNewsSentimentProvider(client, news_sentiment_config(trading_rules()), now=lambda: NOW)

    reading = provider.snapshot(SYMBOL)

    assert reading.error is None
    assert (reading.rated, reading.negative, reading.positive) == (2, 2, 0)
    assert reading.negative_ratio == pytest.approx(1.0)
    assert reading.worst_headline.title == "Regulator opens probe into flagship product"
    assert seen[0].path == "/v2/reference/news"
    assert seen[0].params["ticker"] == SYMBOL
    assert seen[0].params["limit"] == "20"
    # The window is requested of the vendor AND re-applied locally: the
    # 500-hour-old article came back anyway and was still excluded.
    assert seen[0].params["published_utc.gte"] == "2026-08-28T12:00:00Z"


def test_provider_caches_per_symbol_so_one_cycle_is_one_read():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"results": []})

    client = MassiveClient(api_key="test-key", transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    provider = MassiveNewsSentimentProvider(client, news_sentiment_config(trading_rules()), now=lambda: NOW)

    first = provider.snapshot(SYMBOL)
    second = provider.snapshot(SYMBOL)

    assert first is second
    assert calls["n"] == 1


def test_a_massive_http_failure_becomes_an_errored_snapshot_not_an_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    client = MassiveClient(api_key="test-key", transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    provider = MassiveNewsSentimentProvider(client, news_sentiment_config(trading_rules()), now=lambda: NOW)

    reading = provider.snapshot(SYMBOL)

    assert reading.error is not None
    assert reading.rated == 0


def test_build_sentiment_provider_is_config_gated():
    built = build_sentiment_provider(trading_rules(), client_factory=lambda: object())
    off = build_sentiment_provider(trading_rules(enabled=False), client_factory=lambda: object())

    assert isinstance(built, MassiveNewsSentimentProvider)
    assert off is None


# --- 4. the filter can ONLY block or reduce ----------------------------------


def test_glowing_news_can_never_create_a_signal_the_rules_did_not_produce():
    """Maximally positive coverage on a name whose rules verdict is HOLD must
    stay a hold. The filter is one-directional by construction."""
    perfect = snapshot_from(GOOD_NEWS)

    warming_up = engine().generate_equity_signal(SYMBOL, [100.0, 101.0, 99.5], news=perfect)
    no_entry = engine().generate_equity_signal(SYMBOL, downtrend_prices(), news=perfect)

    assert warming_up.side == "hold"
    assert warming_up.reason == "insufficient market data"  # rationale untouched on a non-entry
    assert no_entry.side == "hold"
    assert no_entry.final_signal == "hold"
    assert no_entry.sentiment_action == NOT_APPLICABLE


def test_a_multiplier_above_one_is_clamped_so_the_filter_can_never_boost():
    """A misconfiguration that asks the risk filter to RAISE confidence is
    refused at the config clamp, and says so in the rationale."""
    misconfigured = engine(on_strongly_negative="downweight", strongly_negative_confidence_multiplier=5.0)

    signal = misconfigured.generate_equity_signal(SYMBOL, uptrend_prices(), news=snapshot_from(BAD_NEWS))

    assert signal.sentiment_confidence_multiplier == 1.0
    assert signal.confidence == signal.pre_sentiment_confidence
    assert "clamped to 1.00" in signal.reason
    assert "may only reduce confidence, never raise it" in signal.reason


def test_apply_refuses_to_raise_confidence_even_given_a_hand_built_boost():
    """Belt and braces: even a SentimentFilterResult constructed by hand with a
    multiplier of 10 cannot increase the confidence at the point of
    application."""
    from src.equity_intelligence.news_sentiment import SentimentFilterResult

    strategy = engine()
    buy = strategy.generate_equity_signal(SYMBOL, uptrend_prices())
    assert buy.side == "buy"

    boosted = strategy.apply_sentiment_filter(
        buy, SentimentFilterResult(ALLOW, 10.0, ("hand-built boost",), snapshot_from(GOOD_NEWS))
    )

    assert boosted.confidence == buy.confidence
    assert boosted.side == "buy"


def test_a_skip_verdict_can_never_be_applied_to_an_exit():
    """Bad news must not trap an open position: even a hand-built SKIP cannot
    turn a sell into a hold."""
    strategy = engine()
    sell = strategy.generate_equity_signal(SYMBOL, downtrend_prices(), has_open_position=True)
    assert sell.side == "sell"

    forced_skip = evaluate_news_sentiment(snapshot_from(BAD_NEWS), "buy", sell.confidence, strategy.news_sentiment_config)
    assert forced_skip.action == SKIP

    result = strategy.apply_sentiment_filter(sell, forced_skip)

    assert result.side == "sell"
    assert result.final_signal == "sell"
    assert result.confidence == sell.confidence
    assert result.stop_loss_percent == sell.stop_loss_percent
    # It is still annotated, so the audit says what the news was at exit time.
    assert "Regulator opens probe" in result.reason


def test_a_sell_is_annotated_with_the_sentiment_but_never_filtered():
    signal = engine().generate_equity_signal(SYMBOL, downtrend_prices(), has_open_position=True, news=snapshot_from(BAD_NEWS))

    assert signal.side == "sell"
    assert signal.sentiment_action == NOT_APPLICABLE
    assert signal.confidence == signal.pre_sentiment_confidence
    assert "3/4 rated articles negative" in signal.reason


def test_an_allowed_signal_still_faces_every_risk_gate(monkeypatch):
    """Clean news changes nothing about the gates: the same signal is still
    refused by the allowlist and by the kill switch."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    signal = engine().generate_equity_signal(SYMBOL, uptrend_prices(), news=snapshot_from(GOOD_NEWS))
    assert signal.side == "buy" and signal.sentiment_action == ALLOW

    portfolio = Portfolio(cash_usd=10_000.0, positions={})
    kwargs = dict(
        signal=signal,
        mode="paper",
        notional=250.0,
        portfolio=portfolio,
        daily_summary={"realized_pnl": 0.0, "trade_count": 0},
        has_api_credentials=True,
        order_quantity=250.0 / signal.current_mid,
        current_price=signal.current_mid,
    )
    open_switch = KillSwitch(stop_file="__no_such_stop_file__", env_var="TRADING_ENABLED")

    assert RiskManager(trading_rules(), open_switch).evaluate(**kwargs).allowed

    not_allowlisted = RiskManager(trading_rules("MSFT"), open_switch).evaluate(**kwargs)
    assert not not_allowlisted.allowed
    assert any("not allowlisted" in reason for reason in not_allowlisted.reasons)

    monkeypatch.setenv("TRADING_ENABLED", "false")
    assert not RiskManager(trading_rules(), open_switch).evaluate(**kwargs).allowed


def test_clean_news_cannot_clear_the_human_confirm_flag():
    """Glowing coverage does not arm the broker: dry_run/confirm are the
    operator's flags and nothing in the news path touches them."""

    class Connector:
        def get_accounts(self):
            return {"accounts": [{"account_number": "RH-EQ-AGENTIC-2092", "nickname": "Agentic", "agentic_allowed": True}]}

        def place_equity_order(self, **kwargs):
            raise AssertionError("no news-sentiment path may reach the connector's order tool")

    broker = RobinhoodEquityBroker(RobinhoodEquityClient(Connector()))
    signal = engine().generate_equity_signal(SYMBOL, uptrend_prices(), news=snapshot_from(GOOD_NEWS))
    assert signal.side == "buy"

    result = broker.place_limit_order(
        {"symbol": signal.symbol, "side": "buy", "quantity": 1, "limit_price": 100.0, "notional": 100.0}
    )

    assert broker.will_submit is False
    assert result["submitted"] is False
    assert result["status"] == "dry_run_order_preview"


def test_the_news_module_holds_no_order_path_at_all():
    """A structural guard over the module's own AST (prose in docstrings is
    ignored): the code that interprets news must not name an order tool, a
    broker, the risk manager, or the kill switch. If a future edit reaches for
    one, this fails before it can ship."""
    import ast

    source = (Path(__file__).resolve().parents[1] / "src" / "equity_intelligence" / "news_sentiment.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)

    identifiers: set[str] = set()
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, ast.FunctionDef):
            identifiers.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            imported_modules.add(node.module or "")
            identifiers.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)

    for forbidden in (
        "place_order",
        "place_equity_order",
        "place_limit_order",
        "submit_signal",
        "cancel_order",
        "cancel_equity_order",
        "process_signal",
        "OrderManager",
        "RiskManager",
        "KillSwitch",
        "RobinhoodEquityBroker",
        "RobinhoodEquityClient",
        "PaperBroker",
        "confirm_live_order",
        "dry_run",
        "will_submit",
    ):
        assert forbidden not in identifiers, f"news_sentiment.py must not reference {forbidden!r}"

    # The only client method it may reach is the read-only news endpoint.
    assert "get_ticker_news" in identifiers
    for reachable in ("get_equity_quotes", "get_equity_positions", "get_accounts", "review_equity_order"):
        assert reachable not in identifiers, f"news_sentiment.py must not reference {reachable!r}"

    for module in imported_modules:
        assert not any(
            part in module
            for part in ("order_manager", "risk_manager", "kill_switch", "broker", "robinhood_equity_client")
        ), f"news_sentiment.py must not import {module!r}"


# --- end to end through the paper runtime ------------------------------------


def test_a_negative_name_never_fills_in_a_full_paper_cycle(monkeypatch, tmp_path: Path):
    """End to end through run_equity_paper_loop: a sentiment-skipped entry
    produces a readable rationale in the audit log, no paper fill, and no
    connector order call. The rules series alone WOULD have filled (proved by
    test_equity_runtime.py), so the skip is genuinely the news filter's doing."""
    from src.equity_runtime import run_equity_paper_loop
    from src.logger import SQLiteLogger
    from src.paper_broker import PaperBroker
    from tests.test_equity_runtime import FakeConnector, uptrend_prices as runtime_uptrend, write_config

    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)

    class BadNewsProvider:
        """A stub Massive news provider pinning the name at 3-of-4 negative."""

        def snapshot(self, symbol: str) -> SentimentSnapshot:
            return summarize_news(symbol, BAD_NEWS, news_sentiment_config(trading_rules()), now=NOW)

    connector = FakeConnector({SYMBOL: runtime_uptrend()})
    summary = run_equity_paper_loop(
        connector,
        tmp_path,
        iterations=80,
        poll_interval_seconds=0,
        sleep=lambda _s: None,
        sentiment_provider=BadNewsProvider(),
    )

    assert summary["iterations_completed"] == 80
    assert connector.place_calls == [], "no news-sentiment path may reach the connector's order tool"
    assert PaperBroker(tmp_path / "data" / "equity_paper_trades.db").get_portfolio().quantity_for(SYMBOL) == 0

    decisions = SQLiteLogger(tmp_path / "data" / "trading_agent.db").recent_audit_rows(limit=2000)["decisions"]
    assert not [row for row in decisions if row["action"] == "paper_order_filled"]

    skips = [
        row
        for row in decisions
        if row["action"] == "equity_signal_skipped" and "entry skipped by news sentiment" in row["reason"]
    ]
    assert skips, "the sentiment skip must be written to the audit log with the headline that caused it"
    assert "Regulator opens probe into flagship product" in skips[-1]["reason"]
    assert "3/4 rated articles negative" in skips[-1]["reason"]

    context = [row for row in decisions if row["action"] == "equity_sentiment_context"]
    assert context, "every sentiment-influenced decision writes its structured context"
    assert all("negative_share=" in row["reason"] for row in context)
    assert all('"negative": 3' in row["details"] for row in context)


def test_the_runtime_wires_the_filter_from_config_and_degrades_open_without_a_key(monkeypatch, tmp_path: Path):
    """Config-driven wiring: with `equities.news_sentiment.enabled: true` in
    trading_rules.yaml the runtime builds a provider by itself. With no Massive
    key that provider errors, and the lane degrades to rules-only (fail-open by
    default) rather than crashing or blocking everything."""
    import yaml

    from src.equity_runtime import run_equity_paper_loop
    from tests.test_equity_runtime import FakeConnector, uptrend_prices as runtime_uptrend, write_config

    monkeypatch.setenv("TRADING_ENABLED", "true")
    # No key, and no shelling out to gcloud for one -- this test never leaves
    # the process.
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    monkeypatch.setenv("DS_VAULT_NO_GCLOUD", "1")
    write_config(tmp_path)
    rules_path = tmp_path / "config" / "trading_rules.yaml"
    rules = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    rules["equities"]["news_sentiment"] = {"enabled": True, "recency_hours": 48}
    rules_path.write_text(yaml.safe_dump(rules, sort_keys=False), encoding="utf-8")

    connector = FakeConnector({SYMBOL: runtime_uptrend()})
    run_equity_paper_loop(connector, tmp_path, iterations=80, poll_interval_seconds=0, sleep=lambda _s: None)

    from src.logger import SQLiteLogger
    from src.paper_broker import PaperBroker

    decisions = SQLiteLogger(tmp_path / "data" / "trading_agent.db").recent_audit_rows(limit=2000)["decisions"]
    context = [row for row in decisions if row["action"] == "equity_sentiment_context"]
    assert context, "the runtime must have built a provider from config alone"
    # Every ENTRY-eligible cycle records that the feed could not be read; the
    # warmup holds record only that a non-entry is not the filter's business.
    assert any("news sentiment unavailable" in row["reason"] for row in context)
    assert all("massive_news" in row["details"] for row in context)
    # Fail-open by default: the rules signal still trades in paper.
    assert connector.place_calls == []
    assert PaperBroker(tmp_path / "data" / "equity_paper_trades.db").get_portfolio().quantity_for(SYMBOL) > 0
