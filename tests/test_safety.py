from __future__ import annotations

import json
from pathlib import Path

from src.kill_switch import KillSwitch
from src.live_broker import LiveBroker
from src.logger import SQLiteLogger
from src import main as app_main
from src.order_manager import OrderManager
from src.paper_broker import PaperBroker
from src.portfolio import Portfolio
from src.portfolio import Position
from src.risk_manager import RiskManager
from src.strategy_engine import TradeSignal


def rules() -> dict:
    return {
        "trading": {"enabled": False, "mode": "paper", "allowed_symbols": ["BTC-USD"]},
        "risk": {
            "max_trade_amount_usd": 25,
            "max_daily_loss_usd": 25,
            "max_open_positions": 2,
            "max_trades_per_day": 5,
            "require_cash_available": True,
            "count_existing_robinhood_holdings": False,
            "allow_position_scaling": False,
            "allow_margin": False,
            "allow_shorting": False,
            "max_symbol_allocation_percent": 5,
            "min_order_cooldown_seconds": 300,
            "require_live_order_reconciliation": True,
        },
        "orders": {"require_stop_loss": True, "require_take_profit": True},
    }


def test_kill_switch_blocks_when_env_disabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "false")
    kill = KillSwitch(stop_file=str(tmp_path / "STOP_TRADING"))
    assert "TRADING_ENABLED=false" in kill.halt_reasons()


def test_risk_manager_default_rules_block_trades(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "false")
    signal = TradeSignal(
        symbol="BTC-USD",
        side="buy",
        confidence=0.8,
        reason="test",
        stop_loss_percent=2,
        take_profit_percent=4,
        strategy_signal="buy",
    )
    manager = RiskManager(rules(), KillSwitch(stop_file=str(tmp_path / "STOP_TRADING")))
    decision = manager.evaluate(
        signal=signal,
        mode="paper",
        notional=25,
        portfolio=Portfolio(cash_usd=100),
        daily_summary={"realized_pnl": 0, "trade_count": 0},
        has_api_credentials=False,
    )
    assert not decision.allowed
    assert "config trading.enabled=false" in decision.reasons
    assert "API credentials are missing" in decision.reasons


def test_live_submit_requires_live_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    live_rules = rules()
    live_rules["trading"]["enabled"] = True
    signal = TradeSignal("BTC-USD", "buy", 0.8, "test", 2, 4, "buy")
    manager = RiskManager(live_rules, KillSwitch(stop_file=str(tmp_path / "STOP_TRADING")))
    decision = manager.evaluate(
        signal=signal,
        mode="live",
        notional=25,
        portfolio=Portfolio(cash_usd=100),
        daily_summary={"realized_pnl": 0, "trade_count": 0},
        has_api_credentials=True,
        submit_live_order=True,
    )
    assert not decision.allowed
    assert "live submission requires config trading.mode=live" in decision.reasons


def test_sell_requires_open_position(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    paper_rules = rules()
    paper_rules["trading"]["enabled"] = True
    signal = TradeSignal("BTC-USD", "sell", 0.8, "test", strategy_signal="sell")
    manager = RiskManager(paper_rules, KillSwitch(stop_file=str(tmp_path / "STOP_TRADING")))

    blocked = manager.evaluate(
        signal=signal,
        mode="paper",
        notional=25,
        portfolio=Portfolio(cash_usd=100),
        daily_summary={"realized_pnl": 0, "trade_count": 0},
        has_api_credentials=True,
    )
    allowed = manager.evaluate(
        signal=signal,
        mode="paper",
        notional=25,
        portfolio=Portfolio(cash_usd=100, positions={"BTC-USD": Position("BTC-USD", 0.01)}),
        daily_summary={"realized_pnl": 0, "trade_count": 0},
        has_api_credentials=True,
    )

    assert "no open position to sell" in blocked.reasons
    assert "no open position to sell" not in allowed.reasons


def test_sell_exit_does_not_require_entry_exit_brackets(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    paper_rules = rules()
    paper_rules["trading"]["enabled"] = True
    signal = TradeSignal("BTC-USD", "sell", 0.8, "test", strategy_signal="sell")
    manager = RiskManager(paper_rules, KillSwitch(stop_file=str(tmp_path / "STOP_TRADING")))

    decision = manager.evaluate(
        signal=signal,
        mode="paper",
        notional=25,
        portfolio=Portfolio(cash_usd=100, positions={"BTC-USD": Position("BTC-USD", 0.01)}),
        daily_summary={"realized_pnl": 0, "trade_count": 0},
        has_api_credentials=True,
    )

    assert "stop-loss is missing" not in decision.reasons
    assert "take-profit is missing" not in decision.reasons


def test_sell_less_equal_more_than_position(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    paper_rules = rules()
    paper_rules["trading"]["enabled"] = True
    signal = TradeSignal("BTC-USD", "sell", 0.8, "test", strategy_signal="sell")
    manager = RiskManager(paper_rules, KillSwitch(stop_file=str(tmp_path / "STOP_TRADING")))
    portfolio = Portfolio(cash_usd=100, positions={"BTC-USD": Position("BTC-USD", 0.01)})

    less = manager.evaluate(signal, "paper", 10, portfolio, {"realized_pnl": 0, "trade_count": 0}, True, order_quantity=0.005)
    equal = manager.evaluate(signal, "paper", 10, portfolio, {"realized_pnl": 0, "trade_count": 0}, True, order_quantity=0.01)
    more = manager.evaluate(signal, "paper", 10, portfolio, {"realized_pnl": 0, "trade_count": 0}, True, order_quantity=0.02)

    assert "sell would create short position" not in less.reasons
    assert "sell would create short position" not in equal.reasons
    assert "sell would create short position" in more.reasons


def test_reconcile_zeroes_tiny_negative_residual(tmp_path: Path) -> None:
    broker = PaperBroker(tmp_path / "paper_trades.db")
    broker.place_order(
        {
            "symbol": "BTC-USD",
            "side": "sell",
            "quantity": 0.0000005,
            "limit_price": 100.0,
            "notional": 0.00005,
            "reason": "test",
            "strategy_signal": "test",
        }
    )

    result = broker.reconcile_positions(epsilon=0.000001)
    portfolio = broker.get_portfolio()

    assert result["adjusted"]
    assert not result["errors"]
    assert abs(portfolio.quantity_for("BTC-USD")) < 1e-12


def test_buy_duplicate_position_blocked_when_scaling_disabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    paper_rules = rules()
    paper_rules["trading"]["enabled"] = True
    signal = TradeSignal("BTC-USD", "buy", 0.8, "test", 2, 4, "buy")
    manager = RiskManager(paper_rules, KillSwitch(stop_file=str(tmp_path / "STOP_TRADING")))

    decision = manager.evaluate(
        signal=signal,
        mode="paper",
        notional=25,
        portfolio=Portfolio(cash_usd=100, positions={"BTC-USD": Position("BTC-USD", 0.01)}),
        daily_summary={"realized_pnl": 0, "trade_count": 0},
        has_api_credentials=True,
    )

    assert "position scaling disabled" in decision.reasons


class FakeClient:
    def __init__(self) -> None:
        self.place_order_called = False

    def place_order(self, **kwargs):
        self.place_order_called = True
        raise AssertionError("dry-run must not submit a live order")


class FakeLiveBroker:
    def __init__(self) -> None:
        self.calls = 0

    def place_limit_order(self, order: dict):
        self.calls += 1
        return {
            **order,
            "submitted": False,
            "status": "dry_run_order_preview",
            "order_payload": {"symbol": order["symbol"], "side": order["side"]},
        }


def enabled_rules() -> dict:
    paper_rules = rules()
    paper_rules["trading"]["enabled"] = True
    return paper_rules


def test_live_broker_dry_run_never_calls_client_place_order() -> None:
    client = FakeClient()
    broker = LiveBroker(client, dry_run=True, account_number="123456789")

    result = broker.place_limit_order(
        {
            "client_order_id": "client-1",
            "symbol": "BTC-USD",
            "side": "buy",
            "limit_price": 100.0,
            "quantity": 0.1,
            "notional": 10.0,
            "time_in_force": "gtc",
        }
    )

    assert result["submitted"] is False
    assert result["status"] == "dry_run_order_preview"
    assert result["order_payload"]["symbol"] == "BTC-USD"
    assert client.place_order_called is False


def test_order_manager_dry_run_generates_payload_after_risk_passes(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    paper_rules = enabled_rules()
    signal = TradeSignal("BTC-USD", "buy", 0.8, "test", 2, 4, "buy")
    kill = KillSwitch(stop_file=str(tmp_path / "STOP_TRADING"))
    live_broker = FakeLiveBroker()
    manager = OrderManager(
        paper_rules,
        RiskManager(paper_rules, kill),
        SQLiteLogger(tmp_path / "agent.db"),
        PaperBroker(tmp_path / "paper.db"),
        live_broker,
    )

    result = manager.process_signal(
        signal,
        limit_price=100.0,
        mode="live-dry-run",
        portfolio=Portfolio(cash_usd=1000),
        daily_summary={"realized_pnl": 0, "trade_count": 0},
        has_api_credentials=True,
    )

    assert result is not None
    assert result["submitted"] is False
    assert result["status"] == "dry_run_order_preview"
    assert live_broker.calls == 1


def test_build_limit_order_honors_manual_preview_amount(tmp_path: Path) -> None:
    paper_rules = enabled_rules()
    manager = OrderManager(
        paper_rules,
        RiskManager(paper_rules, KillSwitch(stop_file=str(tmp_path / "STOP_TRADING"))),
        SQLiteLogger(tmp_path / "agent.db"),
        PaperBroker(tmp_path / "paper.db"),
    )
    signal = TradeSignal("BTC-USD", "buy", 0.8, "test", 2, 4, "buy")

    order = manager.build_limit_order(signal, limit_price=100.0, amount_usd=5.0)

    assert order["notional"] == 5.0
    assert order["quantity"] == 0.05


def test_reset_daily_summary_clears_trade_and_block_counts(tmp_path: Path) -> None:
    logger = SQLiteLogger(tmp_path / "agent.db")
    logger.increment_trade_count(pnl=-3.5)
    logger.log_risk_block("BTC-USD", "buy", "test block")

    result = logger.reset_daily_summary()

    assert result["before"]["trade_count"] == 1
    assert result["before"]["blocked_count"] == 1
    assert result["before"]["realized_pnl"] == -3.5
    assert result["after"] == {"realized_pnl": 0.0, "trade_count": 0, "blocked_count": 0}
    assert logger.get_daily_summary() == {"realized_pnl": 0.0, "trade_count": 0, "blocked_count": 0}


def test_stop_trading_blocks_dry_run_payload_generation(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    paper_rules = enabled_rules()
    stop_file = tmp_path / "STOP_TRADING"
    stop_file.write_text("stop", encoding="utf-8")
    signal = TradeSignal("BTC-USD", "buy", 0.8, "test", 2, 4, "buy")
    live_broker = FakeLiveBroker()
    manager = OrderManager(
        paper_rules,
        RiskManager(paper_rules, KillSwitch(stop_file=str(stop_file))),
        SQLiteLogger(tmp_path / "agent.db"),
        PaperBroker(tmp_path / "paper.db"),
        live_broker,
    )

    result = manager.process_signal(
        signal,
        limit_price=100.0,
        mode="live-dry-run",
        portfolio=Portfolio(cash_usd=100),
        daily_summary={"realized_pnl": 0, "trade_count": 0},
        has_api_credentials=True,
    )

    assert result is None
    assert live_broker.calls == 0


def test_trading_mode_paper_prevents_real_order_submission(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    paper_rules = enabled_rules()
    signal = TradeSignal("BTC-USD", "buy", 0.8, "test", 2, 4, "buy")
    manager = RiskManager(paper_rules, KillSwitch(stop_file=str(tmp_path / "STOP_TRADING")))

    decision = manager.evaluate(
        signal=signal,
        mode="paper",
        notional=25,
        portfolio=Portfolio(cash_usd=100),
        daily_summary={"realized_pnl": 0, "trade_count": 0},
        has_api_credentials=True,
        submit_live_order=True,
    )

    assert not decision.allowed
    assert "live submission requires TRADING_MODE=live" in decision.reasons
    assert "live submission requires config trading.mode=live" in decision.reasons


def write_project_config(
    root: Path,
    trading_mode: str = "live",
    enabled: bool = True,
    allowed_symbols: list[str] | None = None,
    risk_overrides: dict | None = None,
) -> None:
    risk_values = {
        "max_trade_amount_usd": 1,
        "max_daily_loss_usd": 1,
        "max_open_positions": 1,
        "max_trades_per_day": 1,
        "require_cash_available": True,
        "count_existing_robinhood_holdings": False,
        "allow_position_scaling": False,
        "allow_margin": False,
        "allow_shorting": False,
        "max_symbol_allocation_percent": 5,
        "min_order_cooldown_seconds": 300,
        "require_live_order_reconciliation": True,
    }
    risk_values.update(risk_overrides or {})
    symbols_yaml = "\n".join(f"    - {symbol}" for symbol in (allowed_symbols or ["BTC-USD"]))
    (root / "config").mkdir()
    (root / "data").mkdir()
    (root / "config" / "trading_rules.yaml").write_text(
        f"""
trading:
  enabled: {str(enabled).lower()}
  mode: {trading_mode}
  allowed_symbols:
{symbols_yaml}
risk:
  max_trade_amount_usd: {risk_values["max_trade_amount_usd"]}
  max_daily_loss_usd: {risk_values["max_daily_loss_usd"]}
  max_open_positions: {risk_values["max_open_positions"]}
  max_trades_per_day: {risk_values["max_trades_per_day"]}
  require_cash_available: {str(risk_values["require_cash_available"]).lower()}
  count_existing_robinhood_holdings: {str(risk_values["count_existing_robinhood_holdings"]).lower()}
  allow_position_scaling: {str(risk_values["allow_position_scaling"]).lower()}
  allow_margin: {str(risk_values["allow_margin"]).lower()}
  allow_shorting: {str(risk_values["allow_shorting"]).lower()}
  max_symbol_allocation_percent: {risk_values["max_symbol_allocation_percent"]}
  min_order_cooldown_seconds: {risk_values["min_order_cooldown_seconds"]}
  require_live_order_reconciliation: {str(risk_values["require_live_order_reconciliation"]).lower()}
orders:
  require_stop_loss: true
  require_take_profit: true
  time_in_force: gtc
exits:
  stop_loss_percent: 2
  take_profit_percent: 4
kill_switch:
  stop_file: STOP_TRADING
  env_var: TRADING_ENABLED
""",
        encoding="utf-8",
    )
    (root / "config" / "strategy.yaml").write_text("strategy: {}\n", encoding="utf-8")


class FakeSmokeClient:
    has_credentials = True
    api_version = "v2"

    def __init__(self, holdings=None) -> None:
        self.holdings = holdings or []
        self.place_order_called = False

    def get_accounts(self):
        return {"results": [{"account_number": "ACCT1234", "buying_power": "100"}]}

    def get_holdings(self, account_number=None):
        return {"results": self.holdings}

    def get_best_bid_ask(self, *symbols):
        return {"results": [{"symbol": symbols[0], "ask": "100", "bid": "99"}]}

    def get_trading_pairs(self, *symbols):
        return {"results": [{"symbol": symbols[0], "state": "tradable"}]}

    def get_orders(self, account_number=None):
        return {"results": []}

    def place_order(self, **kwargs):
        self.place_order_called = True
        return {"id": "live-order-1", "state": "queued", "request": kwargs}

    def cancel_order(self, order_id: str):
        return {"id": order_id, "state": "cancelled"}


class FakeOpenOrdersClient(FakeSmokeClient):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled_ids: list[str] = []

    def get_orders(self, account_number=None):
        return {"results": [{"id": "order-1", "symbol": "BTC-USD", "state": "open"}]}

    def cancel_order(self, order_id: str):
        self.cancelled_ids.append(order_id)
        return {"id": order_id, "state": "cancelled"}


class FakeValidationClient(FakeSmokeClient):
    def __init__(self) -> None:
        super().__init__()
        self.place_order_called = False

    def get_trading_pairs(self, *symbols):
        symbol = symbols[0]
        if symbol == "BAD-USD":
            return {"results": [{"symbol": symbol, "state": "paused"}]}
        if symbol == "MYSTERY-USD":
            return {"results": []}
        return {"results": [{"symbol": symbol, "state": "tradable"}]}

    def get_best_bid_ask(self, *symbols):
        symbol = symbols[0]
        if symbol == "MYSTERY-USD":
            return {"results": []}
        return {"results": [{"symbol": symbol, "ask": "100", "bid": "99"}]}


def setup_live_smoke(
    monkeypatch,
    tmp_path: Path,
    trading_mode: str = "live",
    enabled: bool = True,
    client=None,
    allowed_symbols: list[str] | None = None,
    risk_overrides: dict | None = None,
) -> FakeSmokeClient:
    write_project_config(tmp_path, trading_mode=trading_mode, enabled=enabled, allowed_symbols=allowed_symbols, risk_overrides=risk_overrides)
    monkeypatch.setattr(app_main, "ROOT", tmp_path)
    monkeypatch.setenv("TRADING_MODE", trading_mode)
    monkeypatch.setenv("TRADING_ENABLED", "true" if enabled else "false")
    fake_client = client or FakeSmokeClient()
    monkeypatch.setattr(app_main, "make_client", lambda: fake_client)
    live_symbols = allowed_symbols or ["BTC-USD"]
    app_main.save_symbol_validation(
        {
            "validated_at": "test",
            "submitted": False,
            "available_for_live": live_symbols,
            "unavailable": [],
            "unknown": [],
            "details": {symbol: {"submitted": False, "pair_available": True, "has_bid_ask": True} for symbol in live_symbols},
        },
        tmp_path,
    )
    return fake_client


def test_live_smoke_refuses_without_flag(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path)

    try:
        app_main.live_smoke_order("BTC-USD", "buy", 1, False, lambda _: app_main.LIVE_SMOKE_CONFIRM_TEXT)
    except SystemExit as exc:
        assert "--confirm-live-smoke is required" in str(exc)
    else:
        raise AssertionError("live smoke should refuse without confirmation flag")

    assert client.place_order_called is False


def test_live_smoke_refuses_in_paper_mode(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path, trading_mode="paper")

    try:
        app_main.live_smoke_order("BTC-USD", "buy", 1, True, lambda _: app_main.LIVE_SMOKE_CONFIRM_TEXT)
    except SystemExit as exc:
        assert ".env TRADING_MODE must be live" in str(exc)
        assert "config trading.mode must be live" in str(exc)
    else:
        raise AssertionError("live smoke should refuse in paper mode")

    assert client.place_order_called is False


def test_live_smoke_refuses_amount_over_one(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path)

    try:
        app_main.live_smoke_order("BTC-USD", "buy", 1.01, True, lambda _: app_main.LIVE_SMOKE_CONFIRM_TEXT)
    except SystemExit as exc:
        assert "live smoke amount is capped at $1" in str(exc)
    else:
        raise AssertionError("live smoke should refuse amount over $1")

    assert client.place_order_called is False


def test_live_smoke_refuses_when_stop_file_exists(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path)
    (tmp_path / "STOP_TRADING").write_text("stop", encoding="utf-8")

    try:
        app_main.live_smoke_order("BTC-USD", "buy", 1, True, lambda _: app_main.LIVE_SMOKE_CONFIRM_TEXT)
    except SystemExit as exc:
        assert "STOP_TRADING exists" in str(exc)
    else:
        raise AssertionError("live smoke should refuse when stop file exists")

    assert client.place_order_called is False


def test_live_smoke_refuses_when_typed_confirmation_mismatches(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path)

    try:
        app_main.live_smoke_order("BTC-USD", "buy", 1, True, lambda _: "NO")
    except SystemExit as exc:
        assert "typed confirmation did not match" in str(exc)
    else:
        raise AssertionError("live smoke should refuse mismatched typed confirmation")

    assert client.place_order_called is False


def test_live_smoke_sell_refuses_if_position_unavailable(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path, client=FakeSmokeClient(holdings=[]))

    try:
        app_main.live_smoke_order("BTC-USD", "sell", 1, True, lambda _: app_main.LIVE_SMOKE_CONFIRM_TEXT)
    except SystemExit as exc:
        assert "no open position to sell" in str(exc)
    else:
        raise AssertionError("live smoke sell should refuse without holdings")

    assert client.place_order_called is False


def test_live_smoke_submit_called_only_after_all_gates_and_exact_confirmation(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path)

    app_main.live_smoke_order("BTC-USD", "buy", 1, True, lambda _: app_main.LIVE_SMOKE_CONFIRM_TEXT)

    assert client.place_order_called is True


def assert_live_loop_refuses(monkeypatch, tmp_path: Path, expected: str, **setup_kwargs) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path, **setup_kwargs)

    try:
        app_main.run_live_loop(1, True, test_runner=lambda: (True, "ok"))
    except SystemExit as exc:
        assert expected in str(exc)
    else:
        raise AssertionError("restricted live loop should refuse")

    assert client.place_order_called is False


def test_unattended_live_loop_refuses_without_flag(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path)

    try:
        app_main.run_live_loop(1, False, test_runner=lambda: (True, "ok"))
    except SystemExit as exc:
        assert "--confirm-unattended-live is required" in str(exc)
    else:
        raise AssertionError("restricted live loop should refuse without flag")

    assert client.place_order_called is False


def test_unattended_live_loop_refuses_if_max_trade_amount_too_high(monkeypatch, tmp_path: Path) -> None:
    assert_live_loop_refuses(monkeypatch, tmp_path, "max_trade_amount_usd must be <= 1", risk_overrides={"max_trade_amount_usd": 1.01})


def test_unattended_live_loop_refuses_if_max_daily_loss_too_high(monkeypatch, tmp_path: Path) -> None:
    assert_live_loop_refuses(monkeypatch, tmp_path, "max_daily_loss_usd must be <= 1", risk_overrides={"max_daily_loss_usd": 1.01})


def test_unattended_live_loop_refuses_if_max_trades_per_day_too_high(monkeypatch, tmp_path: Path) -> None:
    assert_live_loop_refuses(monkeypatch, tmp_path, "max_trades_per_day must be <= 1", risk_overrides={"max_trades_per_day": 2})


def test_unattended_live_loop_refuses_if_allowed_symbols_not_btc_only(monkeypatch, tmp_path: Path) -> None:
    assert_live_loop_refuses(monkeypatch, tmp_path, "allowed symbols must be exactly BTC-USD", allowed_symbols=["BTC-USD", "ETH-USD"])


def test_unattended_live_loop_refuses_if_stop_trading_exists(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path)
    (tmp_path / "STOP_TRADING").write_text("stop", encoding="utf-8")

    try:
        app_main.run_live_loop(1, True, test_runner=lambda: (True, "ok"))
    except SystemExit as exc:
        assert "STOP_TRADING exists" in str(exc)
    else:
        raise AssertionError("restricted live loop should refuse when STOP_TRADING exists")

    assert client.place_order_called is False


def assert_bounded_live_loop_refuses(monkeypatch, tmp_path: Path, expected: str, **setup_kwargs) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path, **setup_kwargs)

    try:
        app_main.run_bounded_live_loop(1, True, test_runner=lambda: (True, "ok"))
    except SystemExit as exc:
        assert expected in str(exc)
    else:
        raise AssertionError("bounded live loop should refuse")

    assert client.place_order_called is False


def test_bounded_live_loop_refuses_without_flag(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path)

    try:
        app_main.run_bounded_live_loop(1, False, test_runner=lambda: (True, "ok"))
    except SystemExit as exc:
        assert "--confirm-bounded-live is required" in str(exc)
    else:
        raise AssertionError("bounded live loop should refuse without flag")

    assert client.place_order_called is False


def test_bounded_live_loop_refuses_in_paper_mode(monkeypatch, tmp_path: Path) -> None:
    assert_bounded_live_loop_refuses(monkeypatch, tmp_path, ".env TRADING_MODE must be live", trading_mode="paper")


def test_bounded_live_loop_refuses_if_max_trade_amount_too_high(monkeypatch, tmp_path: Path) -> None:
    assert_bounded_live_loop_refuses(monkeypatch, tmp_path, "max_trade_amount_usd must be <= 100", risk_overrides={"max_trade_amount_usd": 100.01})


def test_bounded_live_loop_refuses_if_max_daily_loss_too_high(monkeypatch, tmp_path: Path) -> None:
    assert_bounded_live_loop_refuses(monkeypatch, tmp_path, "max_daily_loss_usd must be <= 100", risk_overrides={"max_daily_loss_usd": 100.01})


def test_bounded_live_loop_refuses_if_max_trades_per_day_too_high(monkeypatch, tmp_path: Path) -> None:
    assert_bounded_live_loop_refuses(monkeypatch, tmp_path, "max_trades_per_day must be <= 5", risk_overrides={"max_trades_per_day": 6})


def test_bounded_live_loop_accepts_multi_symbol_live_allowlist(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path, allowed_symbols=["BTC-USD", "ETH-USD"])
    try:
        app_main.run_bounded_live_loop(0, True, test_runner=lambda: (True, "ok"))
    except SystemExit as exc:
        raise AssertionError(f"multi-symbol bounded live allowlist should not be rejected: {exc}")
    assert client.place_order_called is False


def test_bounded_live_loop_refuses_if_stop_trading_exists(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path)
    (tmp_path / "STOP_TRADING").write_text("stop", encoding="utf-8")

    try:
        app_main.run_bounded_live_loop(1, True, test_runner=lambda: (True, "ok"))
    except SystemExit as exc:
        assert "STOP_TRADING exists" in str(exc)
    else:
        raise AssertionError("bounded live loop should refuse when STOP_TRADING exists")

    assert client.place_order_called is False


def test_bounded_live_loop_refuses_negative_paper_positions(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path)
    PaperBroker(tmp_path / "data" / "paper_trades.db").place_order(
        {
            "client_order_id": "negative-paper",
            "symbol": "BTC-USD",
            "side": "sell",
            "quantity": 0.0001,
            "limit_price": 100,
            "notional": 0.01,
            "reason": "test",
            "strategy_signal": "sell",
        }
    )

    try:
        app_main.run_bounded_live_loop(1, True, test_runner=lambda: (True, "ok"))
    except SystemExit as exc:
        assert "negative paper positions detected" in str(exc)
    else:
        raise AssertionError("bounded live loop should refuse negative paper positions")

    assert client.place_order_called is False


def test_bounded_live_loop_refuses_when_pytest_fails(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path)

    try:
        app_main.run_bounded_live_loop(1, True, test_runner=lambda: (False, "failure"))
    except SystemExit as exc:
        assert "pytest must pass before bounded live loop" in str(exc)
    else:
        raise AssertionError("bounded live loop should refuse when tests fail")

    assert client.place_order_called is False


def test_bounded_live_loop_refuses_when_robinhood_connection_fails(monkeypatch, tmp_path: Path) -> None:
    class FailingClient(FakeSmokeClient):
        def get_accounts(self):
            raise RuntimeError("network down")

    client = setup_live_smoke(monkeypatch, tmp_path, client=FailingClient())

    try:
        app_main.run_bounded_live_loop(1, True, test_runner=lambda: (True, "ok"))
    except SystemExit as exc:
        assert "Robinhood connection failed" in str(exc)
    else:
        raise AssertionError("bounded live loop should refuse when connection fails")

    assert client.place_order_called is False


def test_bounded_live_loop_refuses_if_allocation_cap_too_high(monkeypatch, tmp_path: Path) -> None:
    assert_bounded_live_loop_refuses(
        monkeypatch,
        tmp_path,
        "max_symbol_allocation_percent must be <= 25",
        risk_overrides={"max_symbol_allocation_percent": 26},
    )


def test_bounded_live_loop_refuses_if_cooldown_too_low(monkeypatch, tmp_path: Path) -> None:
    assert_bounded_live_loop_refuses(
        monkeypatch,
        tmp_path,
        "min_order_cooldown_seconds must be >= 300",
        risk_overrides={"min_order_cooldown_seconds": 299},
    )


def test_bounded_live_loop_refuses_without_reconciliation_requirement(monkeypatch, tmp_path: Path) -> None:
    assert_bounded_live_loop_refuses(
        monkeypatch,
        tmp_path,
        "require_live_order_reconciliation must be true",
        risk_overrides={"require_live_order_reconciliation": False},
    )


def test_risk_manager_blocks_symbol_allocation_limit(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    live_rules = enabled_rules()
    live_rules["trading"]["mode"] = "live"
    live_rules["risk"]["max_symbol_allocation_percent"] = 5
    signal = TradeSignal("BTC-USD", "buy", 0.8, "test", 2, 4, "buy")
    manager = RiskManager(live_rules, KillSwitch(stop_file=str(tmp_path / "STOP_TRADING")))

    decision = manager.evaluate(
        signal=signal,
        mode="live",
        notional=10,
        portfolio=Portfolio(cash_usd=100),
        daily_summary={"realized_pnl": 0, "trade_count": 0},
        has_api_credentials=True,
        submit_live_order=True,
        order_quantity=0.1,
        current_price=100,
    )

    assert not decision.allowed
    assert "symbol allocation limit exceeded" in decision.reasons


def test_risk_manager_blocks_order_cooldown(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    live_rules = enabled_rules()
    live_rules["trading"]["mode"] = "live"
    live_rules["risk"]["min_order_cooldown_seconds"] = 300
    signal = TradeSignal("BTC-USD", "buy", 0.8, "test", 2, 4, "buy")
    manager = RiskManager(live_rules, KillSwitch(stop_file=str(tmp_path / "STOP_TRADING")))

    decision = manager.evaluate(
        signal=signal,
        mode="live",
        notional=1,
        portfolio=Portfolio(cash_usd=100),
        daily_summary={"realized_pnl": 0, "trade_count": 0},
        has_api_credentials=True,
        submit_live_order=True,
        order_quantity=0.01,
        current_price=100,
        last_order_timestamp=SQLiteLogger.now(),
    )

    assert not decision.allowed
    assert "order cooldown is active" in decision.reasons


def test_live_launch_readiness_reports_bounded_state(monkeypatch, tmp_path: Path) -> None:
    setup_live_smoke(monkeypatch, tmp_path)

    report = app_main.live_launch_readiness(root=tmp_path)

    assert report["ready"] is True
    assert report["risk"]["max_trade_amount_usd"] == 1
    assert report["risk"]["max_symbol_allocation_percent"] == 5
    assert report["submitted_live_orders_today"] == 0


def test_reconcile_live_orders_is_read_only(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path, client=FakeOpenOrdersClient())

    result = app_main.reconcile_live_orders(tmp_path)

    assert result["submitted"] is False
    assert result["open_live_orders"] == 1
    assert client.place_order_called is False
    assert client.cancelled_ids == []


def test_cancel_open_live_orders_requires_flag(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path, client=FakeOpenOrdersClient())

    try:
        app_main.cancel_open_live_orders(False, lambda _: app_main.LIVE_CANCEL_CONFIRM_TEXT)
    except SystemExit as exc:
        assert "--confirm-cancel-live is required" in str(exc)
    else:
        raise AssertionError("cancel-open-live-orders should refuse without flag")

    assert client.cancelled_ids == []


def test_cancel_open_live_orders_requires_exact_typed_confirmation(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path, client=FakeOpenOrdersClient())

    try:
        app_main.cancel_open_live_orders(True, lambda _: "NO")
    except SystemExit as exc:
        assert "typed confirmation did not match" in str(exc)
    else:
        raise AssertionError("cancel-open-live-orders should refuse bad typed confirmation")

    assert client.cancelled_ids == []


def test_cancel_open_live_orders_cancels_only_after_confirmation(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path, client=FakeOpenOrdersClient())

    result = app_main.cancel_open_live_orders(True, lambda _: app_main.LIVE_CANCEL_CONFIRM_TEXT)

    assert result["cancelled_count"] == 1
    assert client.cancelled_ids == ["order-1"]
    assert client.place_order_called is False


def test_export_live_audit_redacts_sensitive_fields(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    logger = SQLiteLogger(tmp_path / "data" / "trading_agent.db")
    logger.log_decision(None, "test", "sensitive-bearing row", {"account_number": "ACCT1234", "api_key": "secret"})

    output = app_main.export_live_audit(str(tmp_path / "logs" / "audit.json"), root=tmp_path)
    payload = json.loads(output.read_text(encoding="utf-8"))

    text = json.dumps(payload)
    assert "ACCT1234" not in text
    assert "secret" not in text
    assert "***" in text


def test_return_to_paper_preserves_credentials(monkeypatch, tmp_path: Path) -> None:
    setup_live_smoke(monkeypatch, tmp_path)
    (tmp_path / ".env").write_text(
        "ROBINHOOD_API_KEY=secret-api-key\nROBINHOOD_PRIVATE_KEY=secret-private-key\nTRADING_MODE=live\nTRADING_ENABLED=true\n",
        encoding="utf-8",
    )

    app_main.return_to_paper(True)

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    rules_text = (tmp_path / "config" / "trading_rules.yaml").read_text(encoding="utf-8")
    assert "ROBINHOOD_API_KEY=secret-api-key" in env_text
    assert "ROBINHOOD_PRIVATE_KEY=secret-private-key" in env_text
    assert "TRADING_MODE=paper" in env_text
    assert "TRADING_ENABLED=true" in env_text
    assert "mode: paper" in rules_text


def test_validate_symbols_classifies_available_unavailable_and_unknown(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path, allowed_symbols=["BTC-USD", "BAD-USD", "MYSTERY-USD"], client=FakeValidationClient())

    result = app_main.validate_symbols(tmp_path)

    assert result["available_for_live"] == ["BTC-USD"]
    assert result["unavailable"] == ["BAD-USD"]
    assert result["unknown"] == ["MYSTERY-USD"]
    assert result["submitted"] is False
    assert client.place_order_called is False
    assert (tmp_path / "data" / "symbol_validation.json").exists()


def test_live_readiness_fails_with_unvalidated_symbols(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path, allowed_symbols=["BTC-USD", "ETH-USD"])
    app_main.save_symbol_validation(
        {
            "available_for_live": ["BTC-USD"],
            "unavailable": [],
            "unknown": [],
            "details": {"BTC-USD": {"submitted": False}},
        },
        tmp_path,
    )

    report = app_main.live_launch_readiness(root=tmp_path)

    assert report["ready"] is False
    assert "ETH-USD" in ", ".join(report["unvalidated_live_symbols"])
    assert client.place_order_called is False


def test_bounded_live_loop_logs_scanned_and_skips_unvalidated_symbols(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path, allowed_symbols=["BTC-USD", "ETH-USD"])
    app_main.save_symbol_validation(
        {
            "available_for_live": ["BTC-USD"],
            "unavailable": [],
            "unknown": [],
            "details": {"BTC-USD": {"submitted": False}},
        },
        tmp_path,
    )

    try:
        app_main.run_bounded_live_loop(1, True, test_runner=lambda: (True, "ok"))
    except SystemExit as exc:
        assert "unvalidated live symbols: ETH-USD" in str(exc)

    logger = SQLiteLogger(tmp_path / "data" / "trading_agent.db")
    last = logger.get_last_decision()
    assert last is not None
    assert last["action"] == "bounded_live_refused"
    assert client.place_order_called is False


def test_live_mode_blocks_short_sell(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    live_rules = enabled_rules()
    live_rules["trading"]["mode"] = "live"
    live_rules["risk"]["allow_shorting"] = False
    signal = TradeSignal("BTC-USD", "sell", 0.8, "test", strategy_signal="sell")
    manager = RiskManager(live_rules, KillSwitch(stop_file=str(tmp_path / "STOP_TRADING")))

    decision = manager.evaluate(
        signal=signal,
        mode="live",
        notional=1,
        portfolio=Portfolio(cash_usd=100),
        daily_summary={"realized_pnl": 0, "trade_count": 0},
        has_api_credentials=True,
        submit_live_order=True,
        order_quantity=0.00001,
    )

    assert not decision.allowed
    assert "no open position to sell" in decision.reasons


def test_live_mode_blocks_duplicate_btc_position_when_scaling_false(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    live_rules = enabled_rules()
    live_rules["trading"]["mode"] = "live"
    live_rules["risk"]["allow_position_scaling"] = False
    signal = TradeSignal("BTC-USD", "buy", 0.8, "test", 2, 4, "buy")
    manager = RiskManager(live_rules, KillSwitch(stop_file=str(tmp_path / "STOP_TRADING")))

    decision = manager.evaluate(
        signal=signal,
        mode="live",
        notional=1,
        portfolio=Portfolio(cash_usd=100, positions={"BTC-USD": Position("BTC-USD", 0.001)}),
        daily_summary={"realized_pnl": 0, "trade_count": 0},
        has_api_credentials=True,
        submit_live_order=True,
        order_quantity=0.00001,
    )

    assert not decision.allowed
    assert "position scaling disabled" in decision.reasons


def test_live_single_trade_refuses_without_flag(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path, risk_overrides={"max_daily_loss_usd": 5})

    try:
        app_main.live_single_trade("BTC-USD", "buy", 1, False, lambda _: app_main.LIVE_SINGLE_CONFIRM_TEXT)
    except SystemExit as exc:
        assert "--confirm-live is required" in str(exc)
    else:
        raise AssertionError("live single trade should refuse without flag")

    assert client.place_order_called is False


def test_live_single_trade_refuses_in_paper_mode(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path, trading_mode="paper", risk_overrides={"max_daily_loss_usd": 5})

    try:
        app_main.live_single_trade("BTC-USD", "buy", 1, True, lambda _: app_main.LIVE_SINGLE_CONFIRM_TEXT)
    except SystemExit as exc:
        assert ".env TRADING_MODE must be live" in str(exc)
        assert "config trading.mode must be live" in str(exc)
    else:
        raise AssertionError("live single trade should refuse in paper mode")

    assert client.place_order_called is False


def test_live_single_trade_refuses_amount_over_one(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path, risk_overrides={"max_daily_loss_usd": 5})

    try:
        app_main.live_single_trade("BTC-USD", "buy", 1.01, True, lambda _: app_main.LIVE_SINGLE_CONFIRM_TEXT)
    except SystemExit as exc:
        assert "amount must be <= $1" in str(exc)
    else:
        raise AssertionError("live single trade should refuse amount over $1")

    assert client.place_order_called is False


def test_live_single_trade_refuses_when_daily_trade_count_used(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path, risk_overrides={"max_daily_loss_usd": 5})
    logger = SQLiteLogger(tmp_path / "data" / "trading_agent.db")
    logger.increment_trade_count()

    try:
        app_main.live_single_trade("BTC-USD", "buy", 1, True, lambda _: app_main.LIVE_SINGLE_CONFIRM_TEXT)
    except SystemExit as exc:
        assert "daily trade count must be below 1" in str(exc)
    else:
        raise AssertionError("live single trade should refuse after daily trade count is used")

    assert client.place_order_called is False


def test_live_single_trade_refuses_when_daily_loss_hit(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path, risk_overrides={"max_daily_loss_usd": 5})
    logger = SQLiteLogger(tmp_path / "data" / "trading_agent.db")
    logger.increment_trade_count(pnl=-5)
    logger.reset_daily_summary()
    logger.increment_trade_count(pnl=-5)
    with logger.connect() as conn:
        conn.execute("UPDATE daily_summary SET trade_count = 0")

    try:
        app_main.live_single_trade("BTC-USD", "buy", 1, True, lambda _: app_main.LIVE_SINGLE_CONFIRM_TEXT)
    except SystemExit as exc:
        assert "daily loss must be below $5" in str(exc)
    else:
        raise AssertionError("live single trade should refuse when daily loss is hit")

    assert client.place_order_called is False


def test_live_single_trade_refuses_negative_position(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(
        monkeypatch,
        tmp_path,
        risk_overrides={"max_daily_loss_usd": 5},
        client=FakeSmokeClient(holdings=[{"asset_code": "BTC", "quantity_available_for_trading": "-0.1"}]),
    )

    try:
        app_main.live_single_trade("BTC-USD", "buy", 1, True, lambda _: app_main.LIVE_SINGLE_CONFIRM_TEXT)
    except SystemExit as exc:
        assert "negative positions detected" in str(exc)
    else:
        raise AssertionError("live single trade should refuse negative positions")

    assert client.place_order_called is False


def test_live_single_trade_refuses_typed_confirmation_mismatch(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path, risk_overrides={"max_daily_loss_usd": 5})

    try:
        app_main.live_single_trade("BTC-USD", "buy", 1, True, lambda _: "NO")
    except SystemExit as exc:
        assert "typed confirmation did not match" in str(exc)
    else:
        raise AssertionError("live single trade should refuse mismatched typed confirmation")

    assert client.place_order_called is False


def test_live_single_trade_submits_once_after_all_gates_and_exact_confirmation(monkeypatch, tmp_path: Path) -> None:
    client = setup_live_smoke(monkeypatch, tmp_path, risk_overrides={"max_daily_loss_usd": 5})

    app_main.live_single_trade("BTC-USD", "buy", 1, True, lambda _: app_main.LIVE_SINGLE_CONFIRM_TEXT)

    assert client.place_order_called is True
