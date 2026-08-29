"""End-to-end proving-run coverage for the equities paper runtime.

Exercises run_equity_paper_loop/reconcile_equity_paper the same way an
agent-hosted proving run does: a bounded loop over a fake connector (no real
Robinhood MCP tool is reachable in a test), producing a rationale-per-decision
audit trail and a clean paper-ledger reconciliation.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from src.equity_runtime import (
    equity_kill_switch,
    load_equity_settings,
    reconcile_equity_paper,
    run_equity_cycle,
    run_equity_paper_loop,
)
from src.logger import SQLiteLogger
from src.paper_broker import PaperBroker

AGENT_ACCOUNT = {
    "account_number": "AGENT-ACCT-0001",
    "nickname": "Agentic",
    "agent_tradable": True,
    "cash_available_for_trading": "10000.00",
}
DEFAULT_ACCOUNT = {"account_number": "DEFAULT-ACCT-0002", "nickname": "Default", "agent_tradable": False}

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


def write_config(root: Path, universe: list[str] | None = None) -> None:
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
            "universe": universe if universe is not None else [SYMBOL],
        },
    }
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
