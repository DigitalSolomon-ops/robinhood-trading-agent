"""Massive EOD indicators as signal inputs for the equities lane.

Covers the four things this feature has to be true about:

1. the strategy consumes SMA/EMA/RSI/MACD with CONFIG-DRIVEN thresholds;
2. every decision's rationale cites the indicator values it used;
3. a strongly-overbought / down-trend name is downweighted or skipped;
4. no indicator path can place an order or bypass a risk gate, the kill
   switch, or the human confirm-flag.

The Massive client is mocked throughout (httpx.MockTransport) -- no test in
this file opens a socket.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from src.equity_intelligence.indicator_signals import (
    ALLOW,
    DOWNWEIGHT,
    NOT_APPLICABLE,
    SKIP,
    UNAVAILABLE,
    IndicatorSnapshot,
    MassiveIndicatorProvider,
    build_indicator_provider,
    evaluate_indicators,
    indicator_config,
)
from src.equity_intelligence.massive_client import MassiveClient
from src.kill_switch import KillSwitch
from src.portfolio import Portfolio
from src.risk_manager import RiskManager
from src.robinhood_equity_broker import RobinhoodEquityBroker
from src.robinhood_equity_client import RobinhoodEquityClient
from src.strategy_engine import StrategyEngine

SYMBOL = "AAPL"


# --- shared fixtures ---------------------------------------------------------


def strategy_config(**indicator_overrides) -> dict:
    config = {
        "equity_strategy": {"active_profile": "equities_core_test", "minimum_history_points": 50},
        "equity_profiles": {
            "equities_core_test": {
                "buy_when": ["ema20_above_ema50", "rsi_between_35_and_70", "momentum_5_positive"],
                "sell_when": ["rsi_above_75", "momentum_5_negative", "stop_loss_hit", "take_profit_hit"],
            }
        },
        "equity_indicators": {
            "enabled": True,
            "min_confidence_to_act": 0.0,
            "trend": {"fast_window": 50, "slow_window": 200, "on_downtrend": "skip"},
            "rsi": {"window": 14, "overbought": 70, "strongly_overbought": 80, "oversold": 30},
            "macd": {"on_bearish_cross": "downweight"},
        },
    }
    config["equity_indicators"].update(indicator_overrides)
    return config


def trading_rules(symbol: str = SYMBOL) -> dict:
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
    }


def uptrend_prices(count: int = 60) -> list[float]:
    """The series test_equity_strategy_profile.py proves yields a rules BUY."""
    return [100 + i * 0.03 + ((-1) ** i) * 0.05 for i in range(count)]


def downtrend_prices(count: int = 60) -> list[float]:
    return [100 - i * 0.05 for i in range(count)]


def snapshot(**overrides) -> IndicatorSnapshot:
    """A healthy, mildly bullish snapshot; override one field per test."""
    base = {
        "symbol": SYMBOL,
        "trend_fast": 182.40,
        "trend_slow": 175.10,
        "rsi": 52.0,
        "macd": 1.60,
        "macd_signal": 1.20,
        "macd_histogram": 0.40,
        "as_of_ms": 1_700_000_000_000,
    }
    base.update(overrides)
    return IndicatorSnapshot(**base)


def engine(**indicator_overrides) -> StrategyEngine:
    return StrategyEngine(strategy_config(**indicator_overrides), trading_rules())


# --- 1. the strategy consumes SMA/EMA/RSI/MACD, thresholds from config -------


def test_defaults_are_overridden_key_by_key_not_wholesale():
    config = indicator_config({"equity_indicators": {"rsi": {"overbought": 65}}})

    assert config["rsi"]["overbought"] == 65
    # The keys the override did not mention survive from the defaults.
    assert config["rsi"]["window"] == 14
    assert config["trend"]["slow_window"] == 200
    assert config["macd"]["signal_window"] == 9


def test_absent_config_section_yields_disabled_defaults():
    config = indicator_config({})

    assert config["enabled"] is False
    assert build_indicator_provider({}) is None


def test_thresholds_come_from_config_not_from_code():
    """Same indicator values, two configs, opposite verdicts. If any band were
    hardcoded, one of these two assertions would fail."""
    overbought_at_50 = engine(rsi={"overbought": 50, "strongly_overbought": 51, "on_strongly_overbought": "skip"})
    overbought_at_90 = engine(rsi={"overbought": 90, "strongly_overbought": 95})
    reading = snapshot(rsi=60.0)

    skipped = overbought_at_50.generate_equity_signal(SYMBOL, uptrend_prices(), indicators=reading)
    allowed = overbought_at_90.generate_equity_signal(SYMBOL, uptrend_prices(), indicators=reading)

    assert skipped.side == "hold"
    assert skipped.indicator_action == SKIP
    assert allowed.side == "buy"


def test_provider_reads_sma_rsi_and_macd_from_the_mocked_massive_client():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(f"{path}?window={request.url.params.get('window')}")
        if "/macd/" in path:
            return httpx.Response(
                200,
                json={"results": {"values": [{"timestamp": 3000, "value": 1.6, "signal": 1.2, "histogram": 0.4}]}},
            )
        if "/rsi/" in path:
            return httpx.Response(200, json={"results": {"values": [{"timestamp": 3000, "value": 61.5}]}})
        window = request.url.params.get("window")
        value = 182.4 if window == "50" else 175.1
        return httpx.Response(200, json={"results": {"values": [{"timestamp": 3000, "value": value}]}})

    client = MassiveClient(api_key="test-key", transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    provider = MassiveIndicatorProvider(client, indicator_config(strategy_config()))

    reading = provider.snapshot(SYMBOL)

    assert reading.trend_fast == 182.4
    assert reading.trend_slow == 175.1
    assert reading.rsi == 61.5
    assert (reading.macd, reading.macd_signal, reading.macd_histogram) == (1.6, 1.2, 0.4)
    assert reading.error is None
    assert any("/v1/indicators/sma/AAPL?window=50" in call for call in seen)
    assert any("/v1/indicators/sma/AAPL?window=200" in call for call in seen)
    assert any("/v1/indicators/rsi/AAPL?window=14" in call for call in seen)
    assert any("/v1/indicators/macd/AAPL" in call for call in seen)


def test_provider_can_read_the_ema_trend_series_instead_of_sma():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/v1/indicators/sma/" not in request.url.path, "series: ema must not read SMA"
        return httpx.Response(200, json={"results": {"values": [{"timestamp": 1, "value": 10.0, "signal": 9.0, "histogram": 1.0}]}})

    client = MassiveClient(api_key="test-key", transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    config = indicator_config(strategy_config(trend={"series": "ema", "fast_window": 20, "slow_window": 50}))
    provider = MassiveIndicatorProvider(client, config)

    reading = provider.snapshot(SYMBOL)

    assert reading.trend_fast_label == "EMA20"
    assert reading.trend_slow_label == "EMA50"


def test_provider_caches_per_symbol_so_one_cycle_is_one_read():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"results": {"values": [{"timestamp": 1, "value": 1.0, "signal": 1.0, "histogram": 0.0}]}})

    client = MassiveClient(api_key="test-key", transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    provider = MassiveIndicatorProvider(client, indicator_config(strategy_config()))

    first = provider.snapshot(SYMBOL)
    second = provider.snapshot(SYMBOL)

    assert first is second
    assert calls["n"] == 4  # sma fast, sma slow, rsi, macd -- once each


# --- 2. every rationale cites the indicator values used ----------------------


def test_buy_rationale_cites_every_indicator_value_used():
    signal = engine().generate_equity_signal(SYMBOL, uptrend_prices(), indicators=snapshot())

    assert signal.side == "buy"
    for fragment in ("SMA50=182.40", "SMA200=175.10", "RSI14=52.00", "MACD=1.60", "MACD_signal=1.20"):
        assert fragment in signal.reason, f"{fragment!r} missing from rationale: {signal.reason}"
    assert "uptrend" in signal.reason
    assert "bullish cross" in signal.reason
    assert dict(signal.indicator_values)["RSI14"] == 52.0
    assert signal.indicator_source == "massive"


def test_skip_rationale_names_the_value_and_the_rules_signal_it_overrode():
    signal = engine().generate_equity_signal(SYMBOL, uptrend_prices(), indicators=snapshot(rsi=84.0))

    assert signal.side == "hold"
    assert "RSI14=84.00" in signal.reason
    assert "strongly-overbought 80" in signal.reason
    assert "entry skipped" in signal.reason
    # The rules verdict it overrode is still legible in the same sentence.
    assert "rules signal was 'buy'" in signal.reason
    assert signal.strategy_signal == "buy"
    assert signal.final_signal == "hold"


def test_a_sell_is_annotated_with_the_values_but_never_modulated():
    """An exit must not be blocked or downweighted by a data feed -- that would
    trap an open position behind Massive's uptime."""
    reading = snapshot(trend_fast=100.0, trend_slow=200.0, rsi=95.0, macd=-2.0, macd_signal=-1.0, macd_histogram=-1.0)

    signal = engine().generate_equity_signal(SYMBOL, downtrend_prices(), has_open_position=True, indicators=reading)

    assert signal.side == "sell"
    assert signal.final_signal == "sell"
    assert signal.indicator_action == NOT_APPLICABLE
    assert signal.confidence == signal.base_confidence
    assert "SMA50=100.00" in signal.reason and "RSI14=95.00" in signal.reason
    assert "passed through unchanged" in signal.reason


# --- 3. overbought / down-trend names are downweighted or skipped ------------


def test_strongly_overbought_name_is_skipped():
    signal = engine().generate_equity_signal(SYMBOL, uptrend_prices(), indicators=snapshot(rsi=88.0))

    assert signal.side == "hold"
    assert signal.indicator_action == SKIP
    assert signal.confidence == 0.0
    # A skipped entry carries no exit levels -- there is no position to exit.
    assert signal.stop_loss_percent is None
    assert signal.take_profit_percent is None


def test_merely_overbought_name_is_downweighted_not_skipped():
    signal = engine().generate_equity_signal(SYMBOL, uptrend_prices(), indicators=snapshot(rsi=73.0))

    assert signal.side == "buy"
    assert signal.indicator_action == DOWNWEIGHT
    assert signal.confidence < signal.base_confidence
    assert "at/above overbought 70" in signal.reason


def test_downtrend_name_is_skipped_under_the_default_trend_policy():
    signal = engine().generate_equity_signal(SYMBOL, uptrend_prices(), indicators=snapshot(trend_fast=150.0, trend_slow=175.0))

    assert signal.side == "hold"
    assert signal.indicator_action == SKIP
    assert "downtrend" in signal.reason
    assert "SMA50=150.00 below SMA200=175.00" in signal.reason


def test_downtrend_can_be_configured_to_downweight_instead_of_skip():
    downweighting = engine(trend={"on_downtrend": "downweight", "downtrend_confidence_multiplier": 0.4})

    signal = downweighting.generate_equity_signal(
        SYMBOL, uptrend_prices(), indicators=snapshot(trend_fast=150.0, trend_slow=175.0)
    )

    assert signal.side == "buy"
    assert signal.indicator_action == DOWNWEIGHT
    assert signal.confidence == pytest.approx(signal.base_confidence * 0.4 * 1.1, rel=1e-6)


def test_bearish_macd_cross_downweights_the_entry():
    signal = engine().generate_equity_signal(
        SYMBOL, uptrend_prices(), indicators=snapshot(macd=0.9, macd_signal=1.4, macd_histogram=-0.5)
    )

    assert signal.side == "buy"
    assert signal.indicator_action == DOWNWEIGHT
    assert "bearish cross" in signal.reason
    assert signal.confidence < signal.base_confidence


def test_confidence_floor_turns_a_heavily_downweighted_entry_into_a_hold():
    weak = engine(
        min_confidence_to_act=0.5,
        trend={"on_downtrend": "downweight", "downtrend_confidence_multiplier": 0.5},
    )

    signal = weak.generate_equity_signal(
        SYMBOL,
        uptrend_prices(),
        indicators=snapshot(trend_fast=150.0, trend_slow=175.0, macd=0.9, macd_signal=1.4, macd_histogram=-0.5),
    )

    assert signal.side == "hold"
    assert signal.indicator_action == SKIP
    assert "below min_confidence_to_act" in signal.reason


def test_boosted_confidence_is_capped_by_max_confidence():
    boosted = engine(max_confidence=0.62, rsi={"oversold": 40, "oversold_confidence_multiplier": 5.0})

    signal = boosted.generate_equity_signal(SYMBOL, uptrend_prices(), indicators=snapshot(rsi=35.0))

    assert signal.side == "buy"
    assert signal.confidence == pytest.approx(0.62)


# --- indicator-feed outages degrade, they do not crash or arm ----------------


def test_unreadable_indicator_data_passes_the_rules_signal_through_by_default():
    signal = engine().generate_equity_signal(
        SYMBOL, uptrend_prices(), indicators=IndicatorSnapshot(symbol=SYMBOL, error="HTTPError: 503")
    )

    assert signal.side == "buy"
    assert signal.indicator_action == UNAVAILABLE
    assert "indicator data unavailable" in signal.reason
    assert "503" in signal.reason


def test_require_indicators_fails_closed_when_the_data_is_missing():
    strict = engine(require_indicators=True)

    from_error = strict.generate_equity_signal(
        SYMBOL, uptrend_prices(), indicators=IndicatorSnapshot(symbol=SYMBOL, error="HTTPError: 503")
    )
    from_nothing = strict.generate_equity_signal(SYMBOL, uptrend_prices())

    assert from_error.side == "hold" and from_error.indicator_action == SKIP
    assert from_nothing.side == "hold" and from_nothing.indicator_action == SKIP
    assert "required but unavailable" in from_nothing.reason


def test_a_provider_that_raises_degrades_to_an_unmodulated_signal():
    class ExplodingProvider:
        def snapshot(self, symbol: str) -> IndicatorSnapshot:
            raise RuntimeError("massive is down")

    signal = engine().generate_equity_signal(SYMBOL, uptrend_prices(), indicator_provider=ExplodingProvider())

    assert signal.side == "buy"
    assert signal.indicator_action == UNAVAILABLE
    assert "massive is down" in signal.reason


def test_a_massive_http_failure_becomes_an_errored_snapshot_not_an_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    client = MassiveClient(api_key="test-key", transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    provider = MassiveIndicatorProvider(client, indicator_config(strategy_config()))

    reading = provider.snapshot(SYMBOL)

    assert reading.error is not None
    assert reading.trend_fast is None


def test_omitting_indicators_entirely_leaves_the_signal_byte_identical():
    """Back-compat: the rules-only path is untouched when no indicator data is
    supplied and none is required."""
    plain = engine()

    signal = plain.generate_equity_signal(SYMBOL, uptrend_prices())

    assert signal.side == "buy"
    assert signal.indicator_action == ""
    assert signal.indicator_values == ()
    assert "indicators" not in signal.reason


# --- 4. no indicator path places an order or bypasses a gate -----------------


def test_indicators_can_never_create_a_signal_the_rules_did_not_produce():
    """Maximally bullish readings on a name whose rules verdict is HOLD must
    stay a hold. Modulation is one-directional by construction."""
    perfect = snapshot(trend_fast=999.0, trend_slow=1.0, rsi=5.0, macd=99.0, macd_signal=-99.0, macd_histogram=198.0)

    warming_up = engine().generate_equity_signal(SYMBOL, [100.0, 101.0, 99.5], indicators=perfect)
    no_position = engine().generate_equity_signal(SYMBOL, downtrend_prices(), indicators=perfect)

    assert warming_up.side == "hold"
    assert warming_up.reason == "insufficient market data"  # rationale untouched on a non-entry
    assert no_position.side == "hold"
    assert no_position.final_signal == "hold"


def test_a_skip_verdict_can_never_be_applied_to_an_exit():
    """Even a hand-built SKIP modulation cannot turn a sell into a hold --
    apply_indicator_modulation refuses it structurally."""
    strategy = engine()
    sell = strategy.generate_equity_signal(SYMBOL, downtrend_prices(), has_open_position=True)
    assert sell.side == "sell"

    forced_skip = evaluate_indicators(snapshot(rsi=99.0), "buy", sell.confidence, strategy.indicator_config)
    assert forced_skip.action == SKIP

    result = strategy.apply_indicator_modulation(sell, forced_skip)

    assert result.side == "sell"
    assert result.confidence == sell.confidence


def test_a_boosted_signal_still_faces_every_risk_gate(monkeypatch):
    """The best possible indicator reading changes nothing about the gates:
    the same signal is refused by the allowlist and by the kill switch."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    perfect = snapshot(rsi=32.0)
    signal = engine().generate_equity_signal(SYMBOL, uptrend_prices(), indicators=perfect)
    assert signal.side == "buy" and signal.indicator_action == ALLOW

    portfolio = Portfolio(cash_usd=10_000.0, positions={})
    daily = {"realized_pnl": 0.0, "trade_count": 0}
    kwargs = dict(
        signal=signal,
        mode="paper",
        notional=250.0,
        portfolio=portfolio,
        daily_summary=daily,
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
    halted = RiskManager(trading_rules(), open_switch).evaluate(**kwargs)
    assert not halted.allowed


def test_a_boosted_signal_cannot_clear_the_human_confirm_flag(tmp_path: Path):
    """A perfect indicator reading does not arm the broker: dry_run/confirm are
    the operator's flags and nothing in the indicator path touches them."""

    class Connector:
        def get_accounts(self):
            return {"accounts": [{"account_number": "RH-EQ-AGENTIC-2092", "nickname": "Agentic", "agentic_allowed": True}]}

        def place_equity_order(self, **kwargs):
            raise AssertionError("no indicator path may reach the connector's order tool")

    broker = RobinhoodEquityBroker(RobinhoodEquityClient(Connector()))
    signal = engine().generate_equity_signal(SYMBOL, uptrend_prices(), indicators=snapshot(rsi=32.0))
    assert signal.side == "buy"

    result = broker.place_limit_order(
        {"symbol": signal.symbol, "side": "buy", "quantity": 1, "limit_price": 100.0, "notional": 100.0}
    )

    assert broker.will_submit is False
    assert result["submitted"] is False
    assert result["status"] == "dry_run_order_preview"


def test_an_overbought_name_never_fills_in_a_full_paper_cycle(monkeypatch, tmp_path: Path):
    """End to end through run_equity_cycle: an indicator-skipped entry produces
    a readable rationale in the audit log, no paper fill, and no connector
    order call. The rules series alone would have filled (proved by
    test_equity_runtime.py), so the skip is genuinely the indicators' doing."""
    from tests.test_equity_runtime import FakeConnector, uptrend_prices as runtime_uptrend, write_config
    from src.equity_runtime import run_equity_paper_loop
    from src.logger import SQLiteLogger
    from src.paper_broker import PaperBroker

    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)

    class OverboughtProvider:
        """A stub Massive provider pinning the name at RSI 91 in a downtrend."""

        def snapshot(self, symbol: str) -> IndicatorSnapshot:
            return IndicatorSnapshot(
                symbol=symbol, trend_fast=90.0, trend_slow=150.0, rsi=91.0, macd=-1.0, macd_signal=0.5, macd_histogram=-1.5
            )

    connector = FakeConnector({"AAPL": runtime_uptrend()})
    summary = run_equity_paper_loop(
        connector,
        tmp_path,
        iterations=80,
        poll_interval_seconds=0,
        sleep=lambda _s: None,
        indicator_provider=OverboughtProvider(),
    )

    assert summary["iterations_completed"] == 80
    assert connector.place_calls == [], "no indicator path may reach the connector's order tool"
    assert PaperBroker(tmp_path / "data" / "equity_paper_trades.db").get_portfolio().quantity_for("AAPL") == 0

    decisions = SQLiteLogger(tmp_path / "data" / "trading_agent.db").recent_audit_rows(limit=2000)["decisions"]
    assert not [row for row in decisions if row["action"] == "paper_order_filled"]
    skips = [row for row in decisions if row["action"] == "equity_signal_skipped" and "RSI14=91.00" in row["reason"]]
    assert skips, "the indicator skip must be written to the audit log with the values that caused it"
    assert "downtrend" in skips[-1]["reason"]
    context = [row for row in decisions if row["action"] == "equity_indicator_context"]
    assert context, "every indicator-influenced decision writes its structured context"
    # EVERY context row cites the values, including the ones on non-entry
    # signals where the interpretation was 'not applicable'.
    assert all("RSI14=91.00" in row["reason"] for row in context)
    assert all('"RSI14": 91.0' in row["details"] for row in context)


def test_the_indicator_module_holds_no_order_path_at_all():
    """A structural guard over the module's own AST (prose in docstrings is
    ignored): the code that interprets indicators must not name an order tool,
    a broker, the risk manager, or the kill switch. If a future edit reaches
    for one, this fails before it can ship."""
    import ast

    source = (Path(__file__).resolve().parents[1] / "src" / "equity_intelligence" / "indicator_signals.py").read_text(
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
        assert forbidden not in identifiers, f"indicator_signals.py must not reference {forbidden!r}"

    for module in imported_modules:
        assert not any(
            part in module
            for part in ("order_manager", "risk_manager", "kill_switch", "broker", "robinhood_equity_client")
        ), f"indicator_signals.py must not import {module!r}"
