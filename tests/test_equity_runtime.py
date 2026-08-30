"""End-to-end proving-run coverage for the equities paper runtime.

Exercises run_equity_paper_loop/reconcile_equity_paper the same way an
agent-hosted proving run does: a bounded loop over a fake connector (no real
Robinhood MCP tool is reachable in a test), producing a rationale-per-decision
audit trail and a clean paper-ledger reconciliation.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

import pytest

from src.equity_intelligence.liquidity import LiquiditySnapshot
from src.equity_intelligence.market_regime import RegimeSnapshot
from src.equity_intelligence.massive_client import Bar
from src.equity_intelligence.massive_history import QUOTE_SOURCE, MassiveHistoryUnavailable
from src.equity_runtime import (
    equity_backtest_series,
    equity_kill_switch,
    load_equity_settings,
    reconcile_equity_paper,
    run_equity_cycle,
    run_equity_paper_loop,
    run_equity_proving_run,
)
from src.equity_market_data import CONNECTOR_QUOTE_SOURCE, EquityMarketDataService
from src.logger import SQLiteLogger
from src.paper_broker import PaperBroker
from src.robinhood_equity_client import RobinhoodEquityClient
from src.strategy_engine import StrategyEngine, TradeSignal

AGENT_ACCOUNT = {
    "account_number": "RH-EQ-AGENTIC-2092",
    "nickname": "Agentic",
    "agentic_allowed": True,
    "cash_available_for_trading": "10000.00",
}
DEFAULT_ACCOUNT = {"account_number": "RH-EQ-DEFAULT-2833", "nickname": "Default", "agentic_allowed": False}

SYMBOL = "AAPL"


def uptrend_prices(count: int = 90) -> list[float]:
    # Same slow-drift shape proven in test_equity_strategy_profile.py to
    # clear equities_core_test's buy_when band (ema20>ema50, rsi in
    # [35, 70], momentum positive) without pinning RSI at 100.
    return [100 + i * 0.03 + ((-1) ** i) * 0.05 for i in range(count)]


class FakeConnector:
    """Records calls instead of reaching the real Robinhood MCP connector.

    Feeds one point per get_equity_quotes call from a fixed per-symbol
    series, so a bounded loop of N iterations reproduces exactly the first N
    prices of that series -- the same series test_equity_strategy_profile.py
    already proves yields a buy signal once enough history has accumulated.
    """

    def __init__(self, prices_by_symbol: dict[str, list[float]]) -> None:
        self.prices_by_symbol = prices_by_symbol
        self._index = dict.fromkeys(prices_by_symbol, 0)
        self.place_calls: list[dict] = []

    def get_accounts(self):
        return {"accounts": [AGENT_ACCOUNT, DEFAULT_ACCOUNT]}

    def get_equity_quotes(self, symbols):
        quotes = []
        for symbol in symbols:
            series = self.prices_by_symbol[symbol]
            index = min(self._index[symbol], len(series) - 1)
            self._index[symbol] += 1
            quotes.append({"symbol": symbol, "price": str(series[index])})
        return {"quotes": quotes}

    def get_equity_positions(self, account_number=None):
        return {"positions": []}

    def review_equity_order(self, **kwargs):
        return {"reviewed": True, **kwargs}

    def place_equity_order(self, **kwargs):
        self.place_calls.append(kwargs)
        return {"order_id": "order-1", "status": "accepted"}

    def cancel_equity_order(self, order_id, account_number=None):
        return {"order_id": order_id, "account_number": account_number, "status": "cancel_requested"}


class FakeMassiveClient:
    """Stands in for MassiveClient's historical-bars read. No test reaches the
    real API; what is proven is that the proving run's prices come from THIS
    call and record themselves as such."""

    DAY_MS = 86_400_000

    def __init__(self, closes_by_symbol: dict[str, list[float]]) -> None:
        self.closes_by_symbol = closes_by_symbol
        self.calls: list[dict] = []

    def get_daily_bars(self, ticker: str, from_date: str, to_date: str, adjusted: bool = True) -> list[Bar]:
        self.calls.append({"ticker": ticker, "from_date": from_date, "to_date": to_date, "adjusted": adjusted})
        return [
            Bar(
                timestamp_ms=1_700_000_000_000 + index * self.DAY_MS,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=1_000_000.0,
            )
            for index, close in enumerate(self.closes_by_symbol.get(ticker, []))
        ]


def write_config(
    root: Path,
    universe: list[str] | None = None,
    equities_extra: dict | None = None,
    risk_extra: dict | None = None,
) -> None:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)
    trading_rules = {
        "trading": {"enabled": True, "mode": "paper", "allowed_symbols": []},
        "risk": {
            "max_trade_amount_usd": 250.0,
            "max_daily_loss_usd": 1000.0,
            "max_open_positions": 10,
            "max_trades_per_day": 50,
            "max_symbol_allocation_percent": 100.0,
            "min_order_cooldown_seconds": 0,
            "require_cash_available": True,
            "count_existing_robinhood_holdings": False,
            "allow_position_scaling": False,
            "allow_margin": False,
            "allow_shorting": False,
        },
        "orders": {"order_type": "limit", "time_in_force": "gtc", "require_stop_loss": True, "require_take_profit": True},
        "exits": {"stop_loss_percent": 2.0, "take_profit_percent": 4.0},
        "kill_switch": {"stop_file": "STOP_TRADING", "env_var": "TRADING_ENABLED"},
        "equities": {
            "allow_extended_hours": False,
            "kill_switch": {"stop_file": "STOP_TRADING_EQUITIES", "env_var": "TRADING_ENABLED"},
            "expected_account": {"nickname": "Agentic", "number_suffix": "2092"},
            "universe": universe if universe is not None else [SYMBOL],
            **(equities_extra or {}),
        },
    }
    trading_rules["risk"].update(risk_extra or {})
    strategy = {
        "strategy": {"active_profile": "balanced_test", "minimum_history_points": 50},
        "profiles": {"balanced_test": {"buy_when": ["momentum_5_negative"], "sell_when": ["rsi_above_70"]}},
        "equity_strategy": {"active_profile": "equities_core_test", "minimum_history_points": 50},
        "equity_profiles": {
            "equities_core_test": {
                "buy_when": ["ema20_above_ema50", "rsi_between_35_and_70", "momentum_5_positive"],
                "sell_when": ["rsi_above_75", "momentum_5_negative", "stop_loss_hit", "take_profit_hit"],
            }
        },
    }
    (root / "config" / "trading_rules.yaml").write_text(yaml.safe_dump(trading_rules, sort_keys=False), encoding="utf-8")
    (root / "config" / "strategy.yaml").write_text(yaml.safe_dump(strategy, sort_keys=False), encoding="utf-8")


def test_bounded_paper_loop_completes_unattended_and_reconciles_clean(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)
    connector = FakeConnector({SYMBOL: uptrend_prices()})

    summary = run_equity_paper_loop(connector, tmp_path, iterations=80, poll_interval_seconds=0, sleep=lambda _seconds: None)

    assert summary["iterations_completed"] == 80
    # Nothing here ever reaches the connector's order tools -- paper mode
    # never touches RobinhoodEquityBroker.place_limit_order.
    assert connector.place_calls == []

    reconcile_result = reconcile_equity_paper(tmp_path)
    assert reconcile_result["errors"] == []

    trades = PaperBroker(tmp_path / "data" / "equity_paper_trades.db").get_portfolio()
    assert trades.quantity_for(SYMBOL) > 0, "the uptrend series should have produced at least one simulated paper fill"

    logger = SQLiteLogger(tmp_path / "data" / "trading_agent.db")
    audit = logger.recent_audit_rows(limit=500)
    actions = {row["action"] for row in audit["decisions"]}
    # A readable rationale for both the "skip while history warms up" half
    # and the "act" half of the lane's decisions.
    assert "equity_signal_skipped" in actions
    assert "paper_order_filled" in actions
    assert "equity_market_data_loaded" in actions
    assert "equity_paper_loop_completed" in actions
    filled = next(row for row in audit["decisions"] if row["action"] == "paper_order_filled")
    assert filled["reason"], "a filled paper order must carry a non-empty rationale"
    assert filled["symbol"] == SYMBOL


def test_a_proving_run_replays_real_massive_bars_and_records_the_source(monkeypatch, tmp_path: Path) -> None:
    """The audit's "synthetic feed sold as real quotes" finding, closed.

    A proving run prices itself from the Massive historical-bars endpoint, walks
    one real bar per cycle, writes every price into the candle ledger under the
    name `massive`, and records that name in the completion decision the
    readiness gate reads. The connector is held throughout and is never asked
    for a price -- nor for an order.
    """
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)
    connector = FakeConnector({SYMBOL: uptrend_prices()})
    client = FakeMassiveClient({SYMBOL: uptrend_prices()})

    summary = run_equity_proving_run(
        connector, tmp_path, iterations=80, poll_interval_seconds=0, sleep=lambda _s: None, client=client
    )

    assert summary["iterations_completed"] == 80
    assert summary["quote_source"] == QUOTE_SOURCE == "massive"
    # The bars were pulled once, from the real historical endpoint, over a real
    # window -- not regenerated per cycle.
    assert [call["ticker"] for call in client.calls] == [SYMBOL]
    assert client.calls[0]["from_date"] < client.calls[0]["to_date"]
    assert summary["provenance"]["total_bars"] == len(uptrend_prices())

    # Every price row says where it came from, and none of them claims to be a
    # connector quote.
    sources = EquityMarketDataService(
        RobinhoodEquityClient(connector), tmp_path / "data" / "equity_market_data.db"
    ).sources()
    assert set(sources) == {QUOTE_SOURCE}
    assert CONNECTOR_QUOTE_SOURCE not in sources

    logger = SQLiteLogger(tmp_path / "data" / "trading_agent.db")
    audit = logger.recent_audit_rows(limit=500)
    completed = [row for row in audit["decisions"] if row["action"] == "equity_paper_loop_completed"]
    assert completed, "a bounded proving run must record its completion"
    details = json.loads(completed[-1]["details"])
    assert details["quote_source"] == QUOTE_SOURCE
    assert details["quote_source_provenance"]["vendor"].startswith("Massive")
    # ...and the provenance is stated once, readably, at the top of the run.
    provenance_rows = [row for row in audit["decisions"] if row["action"] == "equity_quote_source"]
    assert provenance_rows and "Massive" in provenance_rows[-1]["reason"]

    # It traded, it reconciles clean, and no connector order tool was touched.
    assert reconcile_equity_paper(tmp_path)["errors"] == []
    assert PaperBroker(tmp_path / "data" / "equity_paper_trades.db").get_portfolio().quantity_for(SYMBOL) > 0
    assert connector.place_calls == []


def test_a_connector_priced_loop_records_the_connector_not_the_real_feed(monkeypatch, tmp_path: Path) -> None:
    """The default loop still prices off the connector -- and says so. That is
    what makes the readiness gate's source assertion meaningful: the two kinds
    of run are distinguishable in the audit log, not merely in intent."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)
    connector = FakeConnector({SYMBOL: uptrend_prices()})

    summary = run_equity_paper_loop(connector, tmp_path, iterations=3, poll_interval_seconds=0, sleep=lambda _s: None)

    assert summary["quote_source"] == CONNECTOR_QUOTE_SOURCE
    assert summary["quote_source"] != QUOTE_SOURCE
    logger = SQLiteLogger(tmp_path / "data" / "trading_agent.db")
    completed = [
        row for row in logger.recent_audit_rows(limit=200)["decisions"] if row["action"] == "equity_paper_loop_completed"
    ]
    assert json.loads(completed[-1]["details"])["quote_source"] == CONNECTOR_QUOTE_SOURCE


def test_a_proving_run_refuses_to_fall_back_when_no_real_bars_exist(monkeypatch, tmp_path: Path) -> None:
    """No real history, no proving run. A fallback to a generated series here is
    precisely the defect being closed, so its absence is pinned by a test."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)
    connector = FakeConnector({SYMBOL: uptrend_prices()})

    with pytest.raises(MassiveHistoryUnavailable):
        run_equity_proving_run(
            connector, tmp_path, iterations=5, poll_interval_seconds=0, sleep=lambda _s: None,
            client=FakeMassiveClient({SYMBOL: []}),
        )

    logger = SQLiteLogger(tmp_path / "data" / "trading_agent.db")
    completed = [
        row for row in logger.recent_audit_rows(limit=200)["decisions"] if row["action"] == "equity_paper_loop_completed"
    ]
    assert completed == [], "a run that could not read real bars must not record a completed proving run"


def test_the_backtest_series_is_the_same_real_bars_handed_over_whole(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)
    closes = uptrend_prices(10)

    series, provenance = equity_backtest_series(tmp_path, client=FakeMassiveClient({SYMBOL: closes}))

    assert series == {SYMBOL: closes}
    assert provenance["quote_source"] == QUOTE_SOURCE


def test_loop_requires_an_explicit_bound(tmp_path: Path) -> None:
    write_config(tmp_path)
    connector = FakeConnector({SYMBOL: uptrend_prices()})

    try:
        run_equity_paper_loop(connector, tmp_path)
    except ValueError as exc:
        assert "bounded" in str(exc)
    else:
        raise AssertionError("an unbounded equities paper loop must be refused")


def test_equities_kill_switch_is_its_own_file_not_the_crypto_stop_file(monkeypatch, tmp_path: Path) -> None:
    """The crypto lane's STOP_TRADING must neither block nor be touched by
    an equities paper run, and vice versa -- the two lanes' kill switches
    are separate files by construction."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)
    (tmp_path / "STOP_TRADING").write_text("crypto lane disarmed", encoding="utf-8")
    connector = FakeConnector({SYMBOL: uptrend_prices()})

    summary = run_equity_paper_loop(connector, tmp_path, iterations=3, poll_interval_seconds=0, sleep=lambda _s: None)

    assert summary["iterations_completed"] == 3
    assert (tmp_path / "STOP_TRADING").read_text(encoding="utf-8") == "crypto lane disarmed"


def test_equities_stop_file_halts_the_loop_immediately(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)
    rules, _ = load_equity_settings(tmp_path)
    kill = equity_kill_switch(rules, tmp_path)
    Path(kill.stop_file).write_text("stop", encoding="utf-8")
    connector = FakeConnector({SYMBOL: uptrend_prices()})

    summary = run_equity_paper_loop(connector, tmp_path, iterations=5, poll_interval_seconds=0, sleep=lambda _s: None)

    assert summary["iterations_completed"] == 0
    logger = SQLiteLogger(tmp_path / "data" / "trading_agent.db")
    last = logger.get_last_decision()
    assert last["action"] == "equity_halted"
    assert "STOP_TRADING_EQUITIES" in last["reason"]


def test_run_equity_cycle_skips_with_readable_rationale_when_quote_is_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path, universe=["AAPL", "MSFT"])

    class MissingSymbolConnector(FakeConnector):
        def get_equity_quotes(self, symbols):
            payload = super().get_equity_quotes(symbols)
            payload["quotes"] = [row for row in payload["quotes"] if row["symbol"] != "MSFT"]
            return payload

    connector = MissingSymbolConnector({"AAPL": uptrend_prices(5), "MSFT": uptrend_prices(5)})

    result = run_equity_cycle(connector, tmp_path)

    assert result["halted"] is False
    assert result["results"]["MSFT"] is None
    logger = SQLiteLogger(tmp_path / "data" / "trading_agent.db")
    audit = logger.recent_audit_rows(limit=50)
    msft_rows = [row for row in audit["decisions"] if row["symbol"] == "MSFT"]
    assert any("no quote available" in row["reason"] for row in msft_rows)


def test_a_halted_symbol_is_skipped_with_a_rationale_and_never_priced(monkeypatch, tmp_path: Path) -> None:
    """A halted/delisted quote must be dropped before the trade path, with an
    audit rationale -- this is the formerly-dead validate_equity_symbols
    _quote_is_active gate now running on live quotes. Reverting the gate (so a
    halted-but-priced symbol is priced and evaluated like any other) removes the
    equity_symbol_unavailable rationale and fails this test."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path, universe=["AAPL", "HALT"])

    class HaltedSymbolConnector(FakeConnector):
        def get_equity_quotes(self, symbols):
            quotes = []
            for symbol in symbols:
                if symbol == "HALT":
                    # A positive price AND an inactive state: only the state
                    # check keeps it out of the trade path.
                    quotes.append({"symbol": "HALT", "price": "42.00", "state": "halted"})
                    continue
                series = self.prices_by_symbol[symbol]
                index = min(self._index[symbol], len(series) - 1)
                self._index[symbol] += 1
                quotes.append({"symbol": symbol, "price": str(series[index])})
            return {"quotes": quotes}

    connector = HaltedSymbolConnector({"AAPL": uptrend_prices(5), "HALT": [42.0] * 5})

    result = run_equity_cycle(connector, tmp_path)

    assert result["halted"] is False
    logger = SQLiteLogger(tmp_path / "data" / "trading_agent.db")
    audit = logger.recent_audit_rows(limit=50)
    halt_rows = [row for row in audit["decisions"] if row["symbol"] == "HALT"]
    assert any(row["action"] == "equity_symbol_unavailable" for row in halt_rows), (
        "a halted symbol must write a skip rationale, not be priced silently"
    )
    # It never reached the market-data ledger, so the strategy never saw it.
    market_db = tmp_path / "data" / "equity_market_data.db"
    if market_db.exists():
        history = EquityMarketDataService(
            RobinhoodEquityClient(connector), market_db
        ).history_count("HALT")
        assert history == 0, "a halted symbol's price must never be saved"
    assert connector.place_calls == []


def test_a_midcycle_kill_switch_trip_logs_the_remaining_symbols(monkeypatch, tmp_path: Path) -> None:
    """A kill-switch trip AFTER the pre-loop check must not leave the rest of the
    universe silently unevaluated. Reverting the per-symbol loop to a bare
    `continue` (no logged decision) removes the equity_halted rationale and
    fails this test."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path, universe=["AAPL", "MSFT"])
    stop_path = tmp_path / "STOP_TRADING_EQUITIES"

    class StopMidCycleConnector(FakeConnector):
        def get_equity_quotes(self, symbols):
            payload = super().get_equity_quotes(symbols)
            # The pre-loop halt check has already passed; drop the stop file now
            # so the per-symbol loop sees the switch tripped mid-cycle.
            stop_path.write_text("stop", encoding="utf-8")
            return payload

    connector = StopMidCycleConnector({"AAPL": uptrend_prices(5), "MSFT": uptrend_prices(5)})

    result = run_equity_cycle(connector, tmp_path)

    # The pre-loop check was clean, so the cycle did not report a top-level halt;
    # the trip happened inside the loop and must be its own logged decision.
    assert result["halted"] is False
    logger = SQLiteLogger(tmp_path / "data" / "trading_agent.db")
    audit = logger.recent_audit_rows(limit=50)
    halted = [row for row in audit["decisions"] if row["action"] == "equity_halted"]
    assert halted, "a mid-cycle kill-switch trip must write a rationale"
    assert "not evaluated" in halted[-1]["reason"]
    assert "STOP_TRADING_EQUITIES" in halted[-1]["reason"]
    assert connector.place_calls == []


# --- market-regime brake + liquidity tradability gate, end to end -------------
#
# Both are READ-ONLY inputs that may only reduce or skip. These tests pin the
# three things that matter: a risk-off market shrinks or refuses NEW entries, an
# illiquid/halted name never reaches the strategy at all, and neither path can
# place an order or step over a risk gate or the kill switch.


class StubRegimeProvider:
    """Hands the runtime a fixed breadth reading. No vendor is contacted."""

    def __init__(self, advancers: int, decliners: int, session: str = "2026-08-27") -> None:
        self.snapshot_value = RegimeSnapshot(
            session=session,
            counted=advancers + decliners,
            advancers=advancers,
            decliners=decliners,
        )
        self.calls = 0

    def snapshot(self) -> RegimeSnapshot:
        self.calls += 1
        return self.snapshot_value


class StubLiquidityProvider:
    def __init__(self, snapshots: dict[str, LiquiditySnapshot]) -> None:
        self.snapshots = snapshots

    def snapshot(self, symbol: str) -> LiquiditySnapshot:
        return self.snapshots.get(symbol, LiquiditySnapshot(symbol=symbol, error="no stub configured"))


def regime_config(**overrides) -> dict:
    return {"market_regime": {"enabled": True, "min_symbols": 10, **overrides}}


def decisions(root: Path) -> list[dict]:
    return SQLiteLogger(root / "data" / "trading_agent.db").recent_audit_rows(limit=2000)["decisions"]


def test_a_risk_off_regime_blocks_new_entries_and_the_rationale_cites_it(monkeypatch, tmp_path: Path) -> None:
    """The same 80-iteration uptrend that fills in
    test_bounded_paper_loop_completes_unattended_and_reconciles_clean fills
    NOTHING once breadth says risk-off. Removing the brake makes this fail."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path, equities_extra=regime_config(on_risk_off="block_entries"))
    connector = FakeConnector({SYMBOL: uptrend_prices()})

    summary = run_equity_paper_loop(
        connector,
        tmp_path,
        iterations=80,
        poll_interval_seconds=0,
        sleep=lambda _s: None,
        regime_provider=StubRegimeProvider(advancers=25, decliners=75),
    )

    assert summary["iterations_completed"] == 80
    assert PaperBroker(tmp_path / "data" / "equity_paper_trades.db").get_portfolio().quantity_for(SYMBOL) == 0
    assert connector.place_calls == []

    rows = decisions(tmp_path)
    assert not [row for row in rows if row["action"] == "paper_order_filled"]
    blocked = [row for row in rows if row["action"] == "equity_regime_blocked"]
    assert blocked, "an entry refused by the regime must write its own rationale"
    reason = blocked[-1]["reason"]
    assert "risk_off" in reason
    assert "25 advancing" in reason, "the rationale must cite the breadth it acted on"
    assert "rules signal was" in reason, "and say what it overrode"
    regime_rows = [row for row in rows if row["action"] == "equity_market_regime"]
    assert regime_rows and json.loads(regime_rows[-1]["details"])["regime"] == "risk_off"


def test_a_risk_off_regime_can_scale_the_trade_size_down_instead_of_blocking(monkeypatch, tmp_path: Path) -> None:
    """Same market, same signals -- the entry still happens, at 40% of the
    configured per-trade cap, and says so in its own order rationale."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(
        tmp_path,
        equities_extra=regime_config(on_risk_off="scale_down", risk_off_size_multiplier=0.4),
    )
    connector = FakeConnector({SYMBOL: uptrend_prices()})

    run_equity_paper_loop(
        connector,
        tmp_path,
        iterations=80,
        poll_interval_seconds=0,
        sleep=lambda _s: None,
        regime_provider=StubRegimeProvider(advancers=25, decliners=75),
    )

    filled = [row for row in decisions(tmp_path) if row["action"] == "paper_order_filled"]
    assert filled, "scale_down must still allow the entry, only smaller"
    # config risk.max_trade_amount_usd is 250.00; the risk-off multiplier is 0.4.
    assert json.loads(filled[0]["details"])["notional"] == 100.0
    assert "market_regime[risk_off" in filled[0]["reason"]
    assert "250.00 -> 100.00" in filled[0]["reason"]
    assert connector.place_calls == []


def test_the_same_run_without_a_regime_sizes_at_the_full_configured_cap(monkeypatch, tmp_path: Path) -> None:
    """The control for the test above: with breadth disabled the identical run
    trades the full 250.00 cap, so the 100.00 above is the brake acting and not
    an artefact of the fixture."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)
    connector = FakeConnector({SYMBOL: uptrend_prices()})

    run_equity_paper_loop(connector, tmp_path, iterations=80, poll_interval_seconds=0, sleep=lambda _s: None)

    rows = decisions(tmp_path)
    filled = [row for row in rows if row["action"] == "paper_order_filled"]
    assert filled
    assert json.loads(filled[0]["details"])["notional"] == 250.0
    assert "market_regime" not in filled[0]["reason"]
    assert not [row for row in rows if row["action"] == "equity_market_regime"]


def test_a_risk_off_regime_never_blocks_or_shrinks_an_exit(monkeypatch, tmp_path: Path) -> None:
    """Bad breadth must not trap an open position. The brake applies to entries
    only: a sell is sized and routed exactly as it would be in any market."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path, equities_extra=regime_config(on_risk_off="block_entries"))
    paper = PaperBroker(tmp_path / "data" / "equity_paper_trades.db")
    paper.place_order(
        {"symbol": SYMBOL, "side": "buy", "quantity": 1.0, "limit_price": 100.0, "notional": 100.0}
    )
    assert paper.get_portfolio().quantity_for(SYMBOL) == 1.0

    def forced_exit(self, symbol, prices, **kwargs):
        return TradeSignal(
            symbol=symbol, side="sell", confidence=0.6, reason="forced_exit",
            strategy_signal="sell", final_signal="sell",
        )

    monkeypatch.setattr(StrategyEngine, "generate_equity_signal", forced_exit)
    connector = FakeConnector({SYMBOL: uptrend_prices()})

    result = run_equity_cycle(connector, tmp_path, regime_provider=StubRegimeProvider(advancers=25, decliners=75))

    assert result["regime"] == "risk_off"
    assert result["regime_action"] == "block_entries"
    assert paper.get_portfolio().quantity_for(SYMBOL) == 0, "the exit must fill in full despite the risk-off regime"
    assert not [row for row in decisions(tmp_path) if row["action"] == "equity_regime_blocked"]
    assert connector.place_calls == []


def test_an_illiquid_symbol_is_skipped_by_the_now_live_tradability_gate(monkeypatch, tmp_path: Path) -> None:
    """MSFT quotes fine and is not halted, so only the liquidity half of the
    gate can refuse it -- and the gate is only consulted at all because
    run_equity_cycle now calls it. Reverting either half fails this."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(
        tmp_path,
        universe=["AAPL", "MSFT"],
        equities_extra={"liquidity": {"enabled": True, "max_stale_days": 5}},
    )
    connector = FakeConnector({"AAPL": uptrend_prices(5), "MSFT": uptrend_prices(5)})
    liquid = LiquiditySnapshot(
        symbol="AAPL", bars=30, first_day="2026-07-01", last_day="2026-08-27", stale_days=1,
        last_close=225.0, average_volume=50_000_000.0, average_dollar_volume=11_000_000_000.0,
    )
    stale = LiquiditySnapshot(
        symbol="MSFT", bars=30, first_day="2026-06-01", last_day="2026-07-18", stale_days=41,
        last_close=410.0, average_volume=20_000_000.0, average_dollar_volume=8_000_000_000.0,
    )

    result = run_equity_cycle(
        connector, tmp_path, liquidity_provider=StubLiquidityProvider({"AAPL": liquid, "MSFT": stale})
    )

    assert result["halted"] is False
    assert result["tradable_symbols"] == ["AAPL"]
    assert result["skipped_symbols"] == ["MSFT"]
    assert result["results"]["MSFT"] is None

    skipped = [row for row in decisions(tmp_path) if row["action"] == "equity_symbol_unavailable"]
    assert [row["symbol"] for row in skipped] == ["MSFT"], "every skip logs a rationale"
    assert "max_stale_days" in skipped[-1]["reason"]
    assert "delisted" in skipped[-1]["reason"]

    # It never reached the price ledger, so the strategy never saw it.
    market_data = EquityMarketDataService(
        RobinhoodEquityClient(connector), tmp_path / "data" / "equity_market_data.db"
    )
    assert market_data.history_count("MSFT") == 0
    assert market_data.history_count("AAPL") == 1
    assert connector.place_calls == []


def test_the_gate_costs_no_extra_connector_quote_poll(monkeypatch, tmp_path: Path) -> None:
    """The gate reuses the cycle's own quote read. A second poll per cycle would
    silently double the connector traffic and desynchronise the price series."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path)

    class CountingConnector(FakeConnector):
        def __init__(self, prices_by_symbol):
            super().__init__(prices_by_symbol)
            self.quote_reads = 0

        def get_equity_quotes(self, symbols):
            self.quote_reads += 1
            return super().get_equity_quotes(symbols)

    connector = CountingConnector({SYMBOL: uptrend_prices()})

    run_equity_paper_loop(connector, tmp_path, iterations=5, poll_interval_seconds=0, sleep=lambda _s: None)

    assert connector.quote_reads == 5


def test_a_risk_on_regime_cannot_outrank_the_kill_switch(monkeypatch, tmp_path: Path) -> None:
    """The best possible breadth reading is still downstream of the emergency
    stop: the equities stop file halts the cycle before a symbol is evaluated."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path, equities_extra=regime_config())
    (tmp_path / "STOP_TRADING_EQUITIES").write_text("stop", encoding="utf-8")
    connector = FakeConnector({SYMBOL: uptrend_prices()})

    result = run_equity_cycle(connector, tmp_path, regime_provider=StubRegimeProvider(advancers=95, decliners=5))

    assert result["halted"] is True
    assert result["results"] == {}
    assert connector.place_calls == []
    assert PaperBroker(tmp_path / "data" / "equity_paper_trades.db").get_portfolio().quantity_for(SYMBOL) == 0


def test_a_regime_scaled_entry_still_has_to_clear_every_risk_gate(monkeypatch, tmp_path: Path) -> None:
    """Scaling is not an exemption. With max_trades_per_day at 0, RiskManager
    refuses the (already shrunk) order and nothing fills -- the regime brake
    removes no gate, it only makes the order it asks about smaller."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(
        tmp_path,
        equities_extra=regime_config(on_risk_off="scale_down", risk_off_size_multiplier=0.4),
        risk_extra={"max_trades_per_day": 0},
    )
    connector = FakeConnector({SYMBOL: uptrend_prices()})

    run_equity_paper_loop(
        connector,
        tmp_path,
        iterations=80,
        poll_interval_seconds=0,
        sleep=lambda _s: None,
        regime_provider=StubRegimeProvider(advancers=25, decliners=75),
    )

    rows = decisions(tmp_path)
    assert not [row for row in rows if row["action"] == "paper_order_filled"]
    blocked = [row for row in rows if row["action"] == "blocked"]
    assert blocked, "the risk layer must still have been asked, and still have refused"
    assert "max daily trade count is hit" in blocked[-1]["reason"]
    assert connector.place_calls == []


def test_a_failed_quote_read_fails_the_gate_closed_not_open(monkeypatch, tmp_path: Path) -> None:
    """Assuming tradable when the read fails is what turns a connector outage
    into an unsupervised trade. A failed read hands the gate an EMPTY row list,
    so every symbol is refused -- with a rationale each."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path, universe=["AAPL", "MSFT"])

    class BrokenQuoteConnector(FakeConnector):
        def get_equity_quotes(self, symbols):
            raise RuntimeError("connector quote tool is unavailable")

    connector = BrokenQuoteConnector({"AAPL": uptrend_prices(5), "MSFT": uptrend_prices(5)})

    result = run_equity_cycle(connector, tmp_path)

    assert result["tradable_symbols"] == []
    assert sorted(result["skipped_symbols"]) == ["AAPL", "MSFT"]
    assert result["results"] == {"AAPL": None, "MSFT": None}
    rows = decisions(tmp_path)
    skipped = {row["symbol"] for row in rows if row["action"] == "equity_symbol_unavailable"}
    assert skipped == {"AAPL", "MSFT"}
    assert any(row["action"] == "equity_market_data_failed" for row in rows)
    assert connector.place_calls == []


def test_a_gate_that_cannot_run_at_all_refuses_the_whole_universe(monkeypatch, tmp_path: Path) -> None:
    """The proving-run shape: prices come from a replayed series, so the gate
    reads the connector quotes itself. If THAT read raises, the fail-closed
    report refuses everything rather than letting the cycle trade blind."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path, universe=["AAPL", "MSFT"])

    class ReplayedPrices:
        quote_source_name = "replayed_test_series"

        def get_prices(self, symbols, logger=None):
            return {symbol: 100.0 for symbol in symbols}

    class BrokenQuoteConnector(FakeConnector):
        def get_equity_quotes(self, symbols):
            raise RuntimeError("connector quote tool is unavailable {not a format string}")

    connector = BrokenQuoteConnector({"AAPL": uptrend_prices(5), "MSFT": uptrend_prices(5)})

    result = run_equity_cycle(connector, tmp_path, quote_source=ReplayedPrices())

    assert result["tradable_symbols"] == []
    assert result["results"] == {"AAPL": None, "MSFT": None}
    validated = [row for row in decisions(tmp_path) if row["action"] == "equity_symbols_validated"]
    assert validated and "no symbol is evaluated this cycle" in validated[-1]["reason"]
    assert connector.place_calls == []


def test_unreadable_breadth_leaves_sizing_at_the_configured_cap(monkeypatch, tmp_path: Path) -> None:
    """A breadth outage degrades to today's behaviour rather than halting the
    lane -- and the audit says the reading was unavailable, not that it was fine."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    write_config(tmp_path, equities_extra=regime_config())

    class ExplodingRegimeProvider:
        def snapshot(self):
            raise RuntimeError("429 rate limited")

    connector = FakeConnector({SYMBOL: uptrend_prices()})

    run_equity_paper_loop(
        connector, tmp_path, iterations=80, poll_interval_seconds=0, sleep=lambda _s: None,
        regime_provider=ExplodingRegimeProvider(),
    )

    rows = decisions(tmp_path)
    filled = [row for row in rows if row["action"] == "paper_order_filled"]
    assert filled and json.loads(filled[0]["details"])["notional"] == 250.0
    regime_rows = [row for row in rows if row["action"] == "equity_market_regime"]
    assert regime_rows and "unavailable" in regime_rows[-1]["reason"]
    assert "429" in regime_rows[-1]["reason"]
