"""The equities broker runs on the SHARED risk machinery, and cannot submit.

Three properties are proved here, all by connector call-count -- the only
witness that says nothing reached Robinhood:

1. the broker routes through the existing OrderManager / RiskManager / kill
   switch, not a parallel risk path of its own;
2. STOP_TRADING and TRADING_ENABLED=false each block execution;
3. an order submits ONLY on an explicit live confirmation, and an order aimed
   at any non-agentic account is refused before the risk layer ever runs.

Point 3 is the runtime half of tests/test_order_symbol_guard.py, which
deliberately declines to check confirm-flag polarity statically because
`if not confirm: submit(...)` passes any static check. That exact inverted
shape is exercised below against the real call path.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from src.kill_switch import KillSwitch
from src.logger import SQLiteLogger
from src.order_manager import OrderManager
from src.paper_broker import PaperBroker
from src.portfolio import Portfolio, Position
from src.risk_manager import RiskManager
from src.robinhood_equity_broker import RobinhoodEquityBroker, equity_portfolio
from src.robinhood_equity_client import AgentAccountMismatchError, RobinhoodEquityClient
from src.strategy_engine import TradeSignal
from src import market_hours

# A weekday inside regular trading hours, and one well outside it -- both
# non-holiday so only the time-of-day (and the extended-hours flag) is under
# test.
DURING_RTH = datetime(2026, 8, 31, 10, 0)
# After the 16:00 regular close but inside the 04:00-20:00 extended window,
# so this same timestamp exercises both "blocked by default" and "allowed
# once extended hours are explicitly opted in".
OUTSIDE_RTH = datetime(2026, 8, 31, 17, 0)
A_MARKET_HOLIDAY = datetime.combine(sorted(market_hours.us_market_holidays(2026))[0], market_hours.REGULAR_OPEN)

AGENT_ACCOUNT = {
    "account_number": "AGENT-ACCT-0001",
    "nickname": "Agentic",
    "agent_tradable": True,
    "cash_available_for_trading": "500.00",
    "buying_power": "1000.00",
}
DEFAULT_ACCOUNT = {"account_number": "DEFAULT-ACCT-0002", "nickname": "Default", "agent_tradable": False}

# A placeholder ticker: never a real one, so this file cannot suggest a symbol
# the lane is not configured for.
TEST_SYMBOL = "SYMBOL"
OPTION_SYMBOL = "SYMBOL250117C00150000"


class FakeConnector:
    """Records calls instead of reaching the real Robinhood MCP connector."""

    def __init__(self, positions: list[dict] | None = None) -> None:
        self.positions = positions if positions is not None else []
        self.place_calls: list[dict] = []
        self.cancel_calls: list[dict] = []
        self.quote_calls: list[dict] = []

    def get_accounts(self):
        return {"accounts": [AGENT_ACCOUNT, DEFAULT_ACCOUNT]}

    def get_equity_quotes(self, symbols):
        self.quote_calls.append({"symbols": symbols})
        return {"quotes": [{"symbol": symbol, "price": "100.00"} for symbol in symbols]}

    def get_equity_positions(self, account_number=None):
        return {"positions": self.positions}

    def review_equity_order(self, **kwargs):
        return {"reviewed": True, **kwargs}

    def place_equity_order(self, **kwargs):
        self.place_calls.append(kwargs)
        return {"order_id": "order-1", "status": "accepted"}

    def cancel_equity_order(self, order_id, account_number=None):
        self.cancel_calls.append({"order_id": order_id, "account_number": account_number})
        return {"order_id": order_id, "status": "cancel_requested"}


def make_broker(connector: FakeConnector, **kwargs) -> RobinhoodEquityBroker:
    # Every test in this file except the market-hours-guard section below is
    # about a DIFFERENT gate; pin the clock to a known weekday inside regular
    # trading hours by default so those tests never flake depending on the
    # real wall-clock time the suite happens to run at (e.g. a weekend).
    kwargs.setdefault("clock", lambda: DURING_RTH)
    return RobinhoodEquityBroker(RobinhoodEquityClient(connector), **kwargs)


def order(**overrides) -> dict:
    base = {
        "client_order_id": "client-1",
        "symbol": TEST_SYMBOL,
        "side": "buy",
        "order_type": "limit",
        "limit_price": 100.0,
        "quantity": 0.25,
        "notional": 25.0,
        "time_in_force": "gtc",
        "reason": "test rationale",
        "strategy_signal": "buy",
    }
    return {**base, **overrides}


def rules() -> dict:
    return {
        "trading": {"enabled": True, "mode": "live", "allowed_symbols": [TEST_SYMBOL]},
        "risk": {
            "max_trade_amount_usd": 25,
            "max_daily_loss_usd": 25,
            "max_open_positions": 2,
            "max_trades_per_day": 5,
            "require_cash_available": True,
            "allow_position_scaling": False,
            "allow_margin": False,
            "allow_shorting": False,
            "max_symbol_allocation_percent": 25,
            "min_order_cooldown_seconds": 0,
        },
        "orders": {"require_stop_loss": True, "require_take_profit": True, "time_in_force": "gtc"},
    }


def signal() -> TradeSignal:
    return TradeSignal(TEST_SYMBOL, "buy", 0.8, "ema cross with rsi confirmation", 2, 4, "buy")


class CountingRiskManager(RiskManager):
    """The real risk layer, plus a counter proving whether it was consulted."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.evaluations = 0

    def evaluate(self, *args, **kwargs):
        self.evaluations += 1
        return super().evaluate(*args, **kwargs)


def build_lane(tmp_path: Path, connector: FakeConnector, **broker_kwargs):
    """The shared machinery, wired exactly as the crypto lane wires it."""
    stop_file = tmp_path / "STOP_TRADING"
    kill_switch = KillSwitch(stop_file=str(stop_file))
    lane_rules = rules()
    logger = SQLiteLogger(tmp_path / "agent.db")
    risk_manager = CountingRiskManager(lane_rules, kill_switch)
    broker = make_broker(connector, kill_switch=kill_switch, logger=logger, **broker_kwargs)
    order_manager = OrderManager(
        lane_rules,
        risk_manager,
        logger,
        PaperBroker(tmp_path / "paper.db"),
        broker,
    )
    return broker, order_manager, risk_manager, logger, stop_file


def run_lane(broker, order_manager, mode="live", account_number=None, portfolio=None):
    return broker.submit_signal(
        order_manager,
        signal(),
        limit_price=100.0,
        mode=mode,
        portfolio=portfolio if portfolio is not None else Portfolio(cash_usd=1000.0),
        daily_summary={"realized_pnl": 0, "trade_count": 0},
        account_number=account_number,
    )


# --- default posture ---------------------------------------------------------


def test_a_broker_built_with_no_flags_cannot_submit() -> None:
    broker = make_broker(FakeConnector())

    assert broker.dry_run is True
    assert broker.confirm_live_order is False
    assert broker.will_submit is False


def test_dry_run_builds_a_payload_and_never_submits() -> None:
    connector = FakeConnector()
    broker = make_broker(connector)

    result = broker.place_limit_order(order())

    assert result["submitted"] is False
    assert result["status"] == "dry_run_order_preview"
    assert result["order_payload"]["symbol"] == TEST_SYMBOL
    assert result["order_payload"]["quantity"] == "0.25"
    assert result["order_payload"]["limit_price"] == "100.0"
    assert result["human_gate"]
    assert connector.place_calls == []


def test_payload_targets_the_pinned_agent_account() -> None:
    connector = FakeConnector()
    broker = make_broker(connector)

    result = broker.place_limit_order(order())

    assert result["order_payload"]["account_number"] == AGENT_ACCOUNT["account_number"]
    assert broker.account_number == AGENT_ACCOUNT["account_number"]


def test_time_in_force_maps_to_robinhood_vocabulary() -> None:
    # Robinhood calls a day order "gfd" -- Alpaca calls the same thing "day".
    assert RobinhoodEquityBroker.time_in_force("gtc") == "gtc"
    assert RobinhoodEquityBroker.time_in_force("day") == "gfd"
    assert RobinhoodEquityBroker.time_in_force(None) == "gfd"
    assert RobinhoodEquityBroker.time_in_force("nonsense") == "gfd"


def test_option_symbols_are_refused_before_anything_else() -> None:
    connector = FakeConnector()
    broker = make_broker(connector, dry_run=False, confirm_live_order=True)

    with pytest.raises(ValueError):
        broker.place_limit_order(order(symbol=OPTION_SYMBOL))

    assert connector.place_calls == []


def test_a_side_that_is_not_buy_or_sell_is_refused() -> None:
    connector = FakeConnector()
    broker = make_broker(connector, dry_run=False, confirm_live_order=True)

    with pytest.raises(ValueError):
        broker.place_limit_order(order(side="short"))

    assert connector.place_calls == []


# --- the non-agentic account gate --------------------------------------------


def test_a_non_agentic_account_is_rejected_at_the_broker() -> None:
    connector = FakeConnector()
    broker = make_broker(connector, dry_run=False, confirm_live_order=True)

    with pytest.raises(AgentAccountMismatchError):
        broker.place_limit_order(order(account_number=DEFAULT_ACCOUNT["account_number"]))

    assert connector.place_calls == []


def test_a_non_agentic_account_is_rejected_before_the_risk_layer(monkeypatch, tmp_path: Path) -> None:
    """Out of bounds is not a risk question -- the refusal must precede it."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    connector = FakeConnector()
    broker, order_manager, risk_manager, logger, _ = build_lane(
        tmp_path, connector, dry_run=False, confirm_live_order=True
    )

    with pytest.raises(AgentAccountMismatchError):
        run_lane(broker, order_manager, account_number=DEFAULT_ACCOUNT["account_number"])

    assert risk_manager.evaluations == 0
    assert connector.place_calls == []
    last = logger.get_last_decision()
    assert last["action"] == "equity_order_refused"
    assert DEFAULT_ACCOUNT["account_number"] in last["reason"]


# --- the shared kill switch ---------------------------------------------------


def test_stop_trading_file_blocks_execution(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    connector = FakeConnector()
    broker, order_manager, risk_manager, logger, stop_file = build_lane(
        tmp_path, connector, dry_run=False, confirm_live_order=True
    )
    stop_file.write_text("stop", encoding="utf-8")

    result = run_lane(broker, order_manager)

    assert result is None
    assert risk_manager.evaluations == 1
    assert connector.place_calls == []
    assert "STOP_TRADING" in logger.get_last_decision()["reason"]


def test_trading_enabled_false_blocks_execution(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "false")
    connector = FakeConnector()
    broker, order_manager, risk_manager, logger, _ = build_lane(
        tmp_path, connector, dry_run=False, confirm_live_order=True
    )

    result = run_lane(broker, order_manager)

    assert result is None
    assert risk_manager.evaluations == 1
    assert connector.place_calls == []
    assert "TRADING_ENABLED=false" in logger.get_last_decision()["reason"]


def test_the_kill_switch_is_re_read_at_the_moment_of_submission(monkeypatch, tmp_path: Path) -> None:
    """Belt and braces: even a caller that skips the risk layer entirely and
    calls the broker directly is refused while the kill switch is engaged."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    connector = FakeConnector()
    broker, _, _, _, stop_file = build_lane(tmp_path, connector, dry_run=False, confirm_live_order=True)
    stop_file.write_text("stop", encoding="utf-8")

    with pytest.raises(RuntimeError, match="kill switch"):
        broker.place_limit_order(order())

    assert connector.place_calls == []


# --- the market-hours guard ----------------------------------------------------


def test_a_broker_built_with_no_flags_defaults_extended_hours_off() -> None:
    broker = make_broker(FakeConnector())

    assert broker.allow_extended_hours is False


def test_an_order_outside_regular_hours_is_refused_by_default() -> None:
    connector = FakeConnector()
    broker = make_broker(connector, dry_run=False, confirm_live_order=True, clock=lambda: OUTSIDE_RTH)

    with pytest.raises(RuntimeError, match="outside regular trading hours"):
        broker.place_limit_order(order())

    assert connector.place_calls == []


def test_an_order_inside_regular_hours_is_not_blocked_by_the_clock() -> None:
    connector = FakeConnector()
    broker = make_broker(connector, dry_run=False, confirm_live_order=True, clock=lambda: DURING_RTH)

    result = broker.place_limit_order(order())

    assert result["submitted"] is True
    assert len(connector.place_calls) == 1


def test_extended_hours_opt_out_defaults_off_so_the_same_clock_still_blocks() -> None:
    connector = FakeConnector()
    broker = make_broker(
        connector,
        dry_run=False,
        confirm_live_order=True,
        clock=lambda: OUTSIDE_RTH,
        allow_extended_hours=False,
    )

    with pytest.raises(RuntimeError, match="outside regular trading hours"):
        broker.place_limit_order(order())

    assert connector.place_calls == []


def test_explicit_extended_hours_opt_in_allows_the_same_clock_through() -> None:
    connector = FakeConnector()
    broker = make_broker(
        connector,
        dry_run=False,
        confirm_live_order=True,
        clock=lambda: OUTSIDE_RTH,
        allow_extended_hours=True,
    )

    result = broker.place_limit_order(order())

    assert result["submitted"] is True
    assert len(connector.place_calls) == 1


def test_a_market_holiday_is_refused_even_with_extended_hours_opted_in() -> None:
    """Market-closed (holiday) must be handled cleanly -- refused with a
    readable reason, not a crash -- and an extended-hours opt-in cannot open
    a day the market itself never opens."""
    connector = FakeConnector()
    broker = make_broker(
        connector,
        dry_run=False,
        confirm_live_order=True,
        clock=lambda: A_MARKET_HOLIDAY,
        allow_extended_hours=True,
    )

    with pytest.raises(RuntimeError, match="market closed"):
        broker.place_limit_order(order())

    assert connector.place_calls == []


def test_market_hours_guard_refuses_before_the_kill_switch_check(monkeypatch, tmp_path: Path) -> None:
    """Both the market-hours guard and the kill switch re-check happen at the
    submission moment; prove the hours guard alone is enough to stop an
    order (the kill switch is left open here)."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    connector = FakeConnector()
    broker, _, _, logger, _ = build_lane(tmp_path, connector, dry_run=False, confirm_live_order=True)
    broker._clock = lambda: OUTSIDE_RTH

    with pytest.raises(RuntimeError, match="outside regular trading hours"):
        broker.place_limit_order(order())

    assert connector.place_calls == []
    assert "outside regular trading hours" in logger.get_last_decision()["reason"]


def test_a_dry_run_preview_is_never_blocked_by_the_market_clock() -> None:
    """Previewing a payload is harmless at any hour -- only a real
    submission is time-gated -- so a broker with no live confirmation must
    still hand back a clean preview instead of raising, weekend or not."""
    connector = FakeConnector()
    broker = make_broker(connector, clock=lambda: OUTSIDE_RTH)

    result = broker.place_limit_order(order())

    assert result["submitted"] is False
    assert result["status"] == "dry_run_order_preview"
    assert connector.place_calls == []


def test_the_shared_lane_blocks_a_live_signal_outside_regular_hours(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    connector = FakeConnector()
    broker, order_manager, risk_manager, logger, _ = build_lane(
        tmp_path, connector, dry_run=False, confirm_live_order=True
    )
    broker._clock = lambda: OUTSIDE_RTH

    with pytest.raises(RuntimeError, match="outside regular trading hours"):
        run_lane(broker, order_manager)

    # The market-hours guard fires inside place_limit_order, reached only
    # after the shared risk layer has already allowed the order through.
    assert risk_manager.evaluations == 1
    assert connector.place_calls == []


# --- the human gate: submits ONLY on an explicit live confirmation ------------


@pytest.mark.parametrize(
    "dry_run,confirm_live_order,case",
    [
        (True, False, "defaults -- neither flag"),
        (True, True, "confirmed but dry_run still on"),
        (False, False, "dry_run off, confirmation absent"),
    ],
)
def test_every_flag_combination_but_both_stays_unsubmitted(dry_run, confirm_live_order, case) -> None:
    connector = FakeConnector()
    broker = make_broker(connector, dry_run=dry_run, confirm_live_order=confirm_live_order)

    result = broker.place_limit_order(order())

    assert result["submitted"] is False, case
    assert len(connector.place_calls) == 0, case


def test_submits_once_when_dry_run_is_off_and_the_order_is_confirmed() -> None:
    connector = FakeConnector()
    broker = make_broker(connector, dry_run=False, confirm_live_order=True)

    result = broker.place_limit_order(order())

    assert result["submitted"] is True
    assert result["status"] == "submitted"
    assert len(connector.place_calls) == 1
    assert connector.place_calls[0]["account_number"] == AGENT_ACCOUNT["account_number"]
    assert connector.place_calls[0]["symbol"] == TEST_SYMBOL


def test_a_truthy_non_boolean_confirmation_does_not_arm_the_lane() -> None:
    connector = FakeConnector()
    broker = make_broker(connector, dry_run=False, confirm_live_order="yes")

    result = broker.place_limit_order(order())

    assert result["submitted"] is False
    assert connector.place_calls == []


def test_an_inverted_confirm_caller_still_cannot_submit() -> None:
    """The static guard cannot see `if not confirm: submit(...)`, so the shape
    is exercised here: the gate lives inside the broker, not in the caller."""
    connector = FakeConnector()

    def buggy_caller(confirm: bool):
        if not confirm:
            broker = make_broker(connector, dry_run=False, confirm_live_order=confirm)
            return broker.place_limit_order(order())
        return None

    result = buggy_caller(confirm=False)

    assert result["submitted"] is False
    assert connector.place_calls == []


def test_absent_confirmation_does_not_submit_through_the_whole_lane(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    connector = FakeConnector()
    broker, order_manager, risk_manager, _, _ = build_lane(tmp_path, connector)

    result = run_lane(broker, order_manager)

    assert risk_manager.evaluations == 1
    assert result["submitted"] is False
    assert len(connector.place_calls) == 0


def test_confirmed_lane_run_submits_exactly_once(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    connector = FakeConnector()
    broker, order_manager, risk_manager, logger, _ = build_lane(
        tmp_path, connector, dry_run=False, confirm_live_order=True
    )

    result = run_lane(broker, order_manager)

    assert result["submitted"] is True
    assert risk_manager.evaluations == 1
    assert len(connector.place_calls) == 1
    # The project's stated purpose: a readable rationale per decision.
    last = logger.get_last_decision()
    assert last["action"] == "live_order_submitted"
    assert last["reason"] == "ema cross with rsi confirmation"


# --- a readable rationale for every decision, act or skip --------------------


def test_a_hold_signal_is_skipped_with_a_readable_rationale_and_never_gated(monkeypatch, tmp_path: Path) -> None:
    """The strategy's own reason for holding must survive into the audit
    log, and a hold must never reach the risk/compliance gates -- there is no
    order to gate."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    connector = FakeConnector()
    broker, order_manager, risk_manager, logger, _ = build_lane(
        tmp_path, connector, dry_run=False, confirm_live_order=True
    )
    hold_signal = TradeSignal(TEST_SYMBOL, "hold", 0.0, "no_entry_conditions_met", None, None, "hold")

    result = broker.submit_signal(
        order_manager,
        hold_signal,
        limit_price=100.0,
        mode="live",
        portfolio=Portfolio(cash_usd=1000.0),
        daily_summary={"realized_pnl": 0, "trade_count": 0},
    )

    assert result is None
    assert risk_manager.evaluations == 0
    assert connector.place_calls == []
    last = logger.get_last_decision()
    assert last["action"] == "equity_signal_skipped"
    assert TEST_SYMBOL in last["reason"]
    assert "no_entry_conditions_met" in last["reason"]


# --- routing through the shared OrderManager ---------------------------------


def test_the_shared_risk_layer_blocks_a_symbol_that_is_not_allowlisted(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    connector = FakeConnector()
    broker, order_manager, risk_manager, logger, _ = build_lane(
        tmp_path, connector, dry_run=False, confirm_live_order=True
    )
    off_list = TradeSignal("NOTLISTED", "buy", 0.8, "test", 2, 4, "buy")

    result = broker.submit_signal(
        order_manager,
        off_list,
        limit_price=100.0,
        mode="live",
        portfolio=Portfolio(cash_usd=1000.0),
        daily_summary={"realized_pnl": 0, "trade_count": 0},
    )

    assert result is None
    assert risk_manager.evaluations == 1
    assert connector.place_calls == []
    assert "not allowlisted" in logger.get_last_decision()["reason"]


def test_the_shared_risk_layer_blocks_a_used_up_daily_trade_count(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    connector = FakeConnector()
    broker, order_manager, risk_manager, logger, _ = build_lane(
        tmp_path, connector, dry_run=False, confirm_live_order=True
    )

    result = broker.submit_signal(
        order_manager,
        signal(),
        limit_price=100.0,
        mode="live",
        portfolio=Portfolio(cash_usd=1000.0),
        daily_summary={"realized_pnl": 0, "trade_count": rules()["risk"]["max_trades_per_day"]},
    )

    assert result is None
    assert risk_manager.evaluations == 1
    assert connector.place_calls == []
    assert "max daily trade count" in logger.get_last_decision()["reason"]


def test_an_armed_broker_still_previews_in_live_dry_run_mode(monkeypatch, tmp_path: Path) -> None:
    """OrderManager's live-dry-run path calls place_limit_order exactly as its
    live path does, so arming the broker must not turn a preview run into a
    real order. The flags are restored afterwards."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    connector = FakeConnector()
    broker, order_manager, _, logger, _ = build_lane(tmp_path, connector, dry_run=False, confirm_live_order=True)

    result = run_lane(broker, order_manager, mode="live-dry-run")

    assert result["submitted"] is False
    assert connector.place_calls == []
    assert logger.get_last_decision()["action"] == "dry_run_order_preview"
    assert broker.will_submit is True


# --- cancel mirrors the same gates -------------------------------------------


def test_cancel_is_inert_without_an_explicit_confirmation() -> None:
    connector = FakeConnector()
    broker = make_broker(connector)

    result = broker.cancel_order("order-1")

    assert result["status"] == "dry_run_cancel_prepared"
    assert result["account_number"] == AGENT_ACCOUNT["account_number"]
    assert connector.cancel_calls == []


def test_cancel_reaches_the_connector_only_when_confirmed() -> None:
    connector = FakeConnector()
    broker = make_broker(connector, dry_run=False, confirm_live_order=True)

    broker.cancel_order("order-1")

    assert connector.cancel_calls == [
        {"order_id": "order-1", "account_number": AGENT_ACCOUNT["account_number"]}
    ]


# --- reads --------------------------------------------------------------------


def test_portfolio_uses_settled_cash_not_buying_power() -> None:
    connector = FakeConnector(positions=[{"symbol": TEST_SYMBOL, "quantity": "3", "average_buy_price": "90.00"}])
    broker = make_broker(connector)

    portfolio = broker.get_portfolio()

    assert portfolio.cash_usd == 500.00
    assert portfolio.cash_usd != float(AGENT_ACCOUNT["buying_power"])
    assert portfolio.quantity_for(TEST_SYMBOL) == 3.0
    assert portfolio.positions[TEST_SYMBOL].average_price == 90.00


def test_portfolio_skips_zero_quantity_rows() -> None:
    portfolio = equity_portfolio(
        {"cash": "100"},
        {"positions": [{"symbol": TEST_SYMBOL, "quantity": "0"}, {"symbol": "OTHER", "quantity": "2"}]},
    )

    assert TEST_SYMBOL not in portfolio.positions
    assert portfolio.open_position_count == 1


def seed_order(logger: SQLiteLogger, symbol: str, side: str, day: str, notional: float = 100.0, status: str = "submitted") -> None:
    """Insert an order history row with a chosen timestamp -- log_order()
    always stamps `now()`, but the PDT/settlement guards need control over
    which historical day a fill landed on."""
    with logger.connect() as conn:
        conn.execute(
            """
            INSERT INTO orders (timestamp, client_order_id, symbol, side, order_type, quantity, limit_price, notional, status, details)
            VALUES (?, ?, ?, ?, 'limit', 1, 100.0, ?, ?, '{}')
            """,
            (f"{day}T10:00:00+00:00", f"seed-{symbol}-{side}-{day}", symbol, side, notional, status),
        )


# --- the pattern-day-trader guard ---------------------------------------------

# DURING_RTH (2026-08-31) is a Monday, so "the trailing 5 business days" is
# unambiguous: Mon 08-31 (today), Fri 08-28, Thu 08-27, Wed 08-26, Tue 08-25.
_PRIOR_DAY_TRADE_DAYS = ("2026-08-25", "2026-08-26", "2026-08-27")


def test_pdt_guard_blocks_the_fourth_day_trade_under_25k(tmp_path: Path) -> None:
    connector = FakeConnector(positions=[{"symbol": TEST_SYMBOL, "quantity": "1", "average_buy_price": "100.00"}])
    logger = SQLiteLogger(tmp_path / "agent.db")
    for day in _PRIOR_DAY_TRADE_DAYS:
        seed_order(logger, TEST_SYMBOL, "buy", day)
        seed_order(logger, TEST_SYMBOL, "sell", day)
    seed_order(logger, TEST_SYMBOL, "buy", "2026-08-31")  # today's opening leg
    broker = make_broker(connector, dry_run=False, confirm_live_order=True, logger=logger)
    # Account equity here is cash (500) + 1 share @ $100 = $600 -- well under $25k.

    with pytest.raises(RuntimeError, match="pattern-day-trader guard"):
        broker.place_limit_order(order(side="sell", quantity=0.25))

    assert connector.place_calls == []
    assert "pattern-day-trader guard" in logger.get_last_decision()["reason"]


def test_pdt_guard_allows_a_third_day_trade_under_25k(tmp_path: Path) -> None:
    connector = FakeConnector(positions=[{"symbol": TEST_SYMBOL, "quantity": "1", "average_buy_price": "100.00"}])
    logger = SQLiteLogger(tmp_path / "agent.db")
    for day in _PRIOR_DAY_TRADE_DAYS[:2]:
        seed_order(logger, TEST_SYMBOL, "buy", day)
        seed_order(logger, TEST_SYMBOL, "sell", day)
    seed_order(logger, TEST_SYMBOL, "buy", "2026-08-31")
    broker = make_broker(connector, dry_run=False, confirm_live_order=True, logger=logger)

    result = broker.place_limit_order(order(side="sell", quantity=0.25))

    assert result["submitted"] is True
    assert len(connector.place_calls) == 1


def test_pdt_guard_does_not_block_once_account_equity_clears_25k(tmp_path: Path) -> None:
    # Same 3 prior day trades plus today's opening leg as the blocked case
    # above, but enough held shares to put account equity at/over $25k.
    connector = FakeConnector(positions=[{"symbol": TEST_SYMBOL, "quantity": "250", "average_buy_price": "100.00"}])
    logger = SQLiteLogger(tmp_path / "agent.db")
    for day in _PRIOR_DAY_TRADE_DAYS:
        seed_order(logger, TEST_SYMBOL, "buy", day)
        seed_order(logger, TEST_SYMBOL, "sell", day)
    seed_order(logger, TEST_SYMBOL, "buy", "2026-08-31")
    broker = make_broker(connector, dry_run=False, confirm_live_order=True, logger=logger)

    result = broker.place_limit_order(order(side="sell", quantity=0.25))

    assert result["submitted"] is True
    assert len(connector.place_calls) == 1


def test_pdt_guard_is_re_checked_through_the_shared_lane(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TRADING_ENABLED", "true")
    connector = FakeConnector(positions=[{"symbol": TEST_SYMBOL, "quantity": "1", "average_buy_price": "100.00"}])
    broker, order_manager, risk_manager, logger, _ = build_lane(
        tmp_path, connector, dry_run=False, confirm_live_order=True
    )
    for day in _PRIOR_DAY_TRADE_DAYS:
        seed_order(logger, TEST_SYMBOL, "buy", day)
        seed_order(logger, TEST_SYMBOL, "sell", day)
    seed_order(logger, TEST_SYMBOL, "buy", "2026-08-31")
    sell_signal = TradeSignal(TEST_SYMBOL, "sell", 0.8, "test", None, None, "sell")

    with pytest.raises(RuntimeError, match="pattern-day-trader guard"):
        broker.submit_signal(
            order_manager,
            sell_signal,
            limit_price=100.0,
            mode="live",
            portfolio=Portfolio(cash_usd=1000.0, positions={TEST_SYMBOL: Position(TEST_SYMBOL, 1.0)}),
            daily_summary={"realized_pnl": 0, "trade_count": 0},
        )

    assert connector.place_calls == []


# --- shorts and margin are rejected -------------------------------------------


def test_a_sell_beyond_the_held_quantity_is_refused_as_a_short(tmp_path: Path) -> None:
    connector = FakeConnector(positions=[{"symbol": TEST_SYMBOL, "quantity": "0.1", "average_buy_price": "100.00"}])
    logger = SQLiteLogger(tmp_path / "agent.db")
    broker = make_broker(connector, dry_run=False, confirm_live_order=True, logger=logger)

    with pytest.raises(RuntimeError, match="short"):
        broker.place_limit_order(order(side="sell", quantity=0.25))

    assert connector.place_calls == []
    assert "long-only guard" in logger.get_last_decision()["reason"]


def test_a_sell_with_no_position_at_all_is_refused_as_a_short(tmp_path: Path) -> None:
    connector = FakeConnector(positions=[])
    logger = SQLiteLogger(tmp_path / "agent.db")
    broker = make_broker(connector, dry_run=False, confirm_live_order=True, logger=logger)

    with pytest.raises(RuntimeError, match="short"):
        broker.place_limit_order(order(side="sell", quantity=0.25))

    assert connector.place_calls == []


def test_a_buy_beyond_settled_cash_is_refused_as_margin(tmp_path: Path) -> None:
    # A cash account has no margin buying power -- a buy that does not fit
    # inside cash on hand is refused by the same guard that enforces
    # settlement, since there is nowhere else for it to draw from.
    connector = FakeConnector(positions=[])
    connector.get_accounts = lambda: {  # cash-poor account, well under the $25 order
        "accounts": [{**AGENT_ACCOUNT, "cash_available_for_trading": "1.00"}, DEFAULT_ACCOUNT]
    }
    logger = SQLiteLogger(tmp_path / "agent.db")
    broker = make_broker(connector, dry_run=False, confirm_live_order=True, logger=logger)

    with pytest.raises(RuntimeError, match="good-faith guard"):
        broker.place_limit_order(order(side="buy", quantity=0.25, notional=25.0))

    assert connector.place_calls == []
    assert "no margin" in logger.get_last_decision()["reason"]


def test_a_buy_that_would_spend_same_day_unsettled_sale_proceeds_is_refused(tmp_path: Path) -> None:
    connector = FakeConnector(positions=[])
    logger = SQLiteLogger(tmp_path / "agent.db")
    # Sold $500 worth earlier today; those proceeds have not settled yet
    # (T+1), so a buy funded by them today is a good-faith violation even
    # though the account shows $500 of cash on hand.
    seed_order(logger, TEST_SYMBOL, "sell", "2026-08-31", notional=500.0)
    broker = make_broker(connector, dry_run=False, confirm_live_order=True, logger=logger)

    with pytest.raises(RuntimeError, match="good-faith guard"):
        broker.place_limit_order(order(side="buy", quantity=0.25, notional=25.0))

    assert connector.place_calls == []


def test_reads_resolve_against_the_pinned_account() -> None:
    connector = FakeConnector()
    broker = make_broker(connector)

    assert broker.get_positions_payload() == {"positions": []}
    assert broker.get_account_payload()["account_number"] == AGENT_ACCOUNT["account_number"]
    broker.get_quotes(TEST_SYMBOL)
    assert connector.quote_calls == [{"symbols": [TEST_SYMBOL]}]
