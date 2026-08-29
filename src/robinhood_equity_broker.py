from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .kill_switch import KillSwitch
from .logger import SQLiteLogger
from .order_manager import OrderManager
from .portfolio import Portfolio, Position
from .robinhood_equity_client import AgentAccountMismatchError, RobinhoodEquityClient
from .strategy_engine import TradeSignal

VENUE = "robinhood_equities"

# Long-only plain equities. A ticker is 1-6 letters with an optional dot or
# dash class suffix; a 21-character OCC option symbol can never match it. That
# is how "no options in this lane" is enforced -- by shape, so no ticker is
# ever named in this module (tests/test_order_symbol_guard.py).
_EQUITY_SYMBOL = re.compile(r"^[A-Za-z][A-Za-z.\-]{0,5}$")

# Robinhood's time-in-force vocabulary. It is NOT Alpaca's: a day order is
# "gfd" here, where Alpaca calls it "day".
_TIME_IN_FORCE = {"gtc": "gtc", "gfd": "gfd", "day": "gfd", "ioc": "ioc", "opg": "opg"}

# Account fields that hold settled cash, most specific first. buying_power is
# the last resort and only for a cash account -- margin is forbidden on every
# lane, so an inflated buying power must never be read as available capital.
_CASH_FIELDS = ("cash_available_for_trading", "cash", "portfolio_cash", "buying_power")


def equity_portfolio(account_payload: Any, positions_payload: Any) -> Portfolio:
    """Build a Portfolio from the connector's account + positions payloads.

    Mirrors Portfolio.from_alpaca / from_robinhood, kept here so the crypto
    lane's Portfolio mapping is left untouched by this build.
    """
    cash = 0.0
    if isinstance(account_payload, dict):
        for field in _CASH_FIELDS:
            if account_payload.get(field) not in (None, ""):
                cash = float(account_payload[field])
                break

    rows: list[Any] = []
    if isinstance(positions_payload, dict):
        rows = positions_payload.get("positions", positions_payload.get("results", [])) or []
    elif isinstance(positions_payload, list):
        rows = positions_payload

    positions: dict[str, Position] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = row.get("symbol")
        quantity = float(row.get("quantity") or row.get("total_quantity") or 0)
        if not symbol or not quantity:
            continue
        positions[symbol] = Position(
            symbol=symbol,
            quantity=quantity,
            average_price=float(row.get("average_buy_price") or row.get("average_price") or 0),
            pnl=float(row.get("unrealized_pl") or row.get("total_return") or 0),
        )
    return Portfolio(cash_usd=cash, positions=positions)


class RobinhoodEquityBroker:
    """Robinhood equities broker over the authorized OAuth MCP connector.

    Mirrors AlpacaBroker / LiveBroker so the SAME OrderManager, RiskManager and
    kill switch govern this lane -- there is no parallel risk path here. The
    broker's own job is the three things the shared layers cannot know about:

    1. the order lands in the ONE Robinhood-designated agent-tradable account
       and nowhere else (checked before the risk layer via submit_signal);
    2. nothing is submitted unless BOTH human-gate flags are set -- dry_run
       False AND confirm_live_order True. Either flag alone, or neither,
       returns an unsubmitted payload preview and never reaches the connector;
    3. at the irreversible moment, the kill switch is re-checked and a real
       order is refused outright if STOP_TRADING exists or TRADING_ENABLED is
       not true.

    READ-ONLY is the default posture: dry_run defaults to True and
    confirm_live_order to False, so a broker built with no arguments cannot
    place an order. Flipping either is the operator's explicit call.
    """

    def __init__(
        self,
        client: RobinhoodEquityClient,
        dry_run: bool = True,
        confirm_live_order: bool = False,
        kill_switch: KillSwitch | None = None,
        logger: SQLiteLogger | None = None,
    ) -> None:
        self.client = client
        self.dry_run = dry_run
        self.confirm_live_order = confirm_live_order
        self.kill_switch = kill_switch
        self.logger = logger

    @property
    def account_number(self) -> str:
        """The pinned agent-tradable account, resolved by the client."""
        return self.client.account_number

    @property
    def will_submit(self) -> bool:
        """True only when both human-gate flags are explicitly set.

        Compared against True by identity rather than truthiness so a stray
        non-boolean ("yes", 1) cannot arm the lane.
        """
        return self.dry_run is False and self.confirm_live_order is True

    def gate_reason(self) -> str:
        """Human-readable reason this broker will not submit, for the audit log."""
        blocking = []
        if self.dry_run is not False:
            blocking.append("dry_run is on")
        if self.confirm_live_order is not True:
            blocking.append("no explicit live confirmation")
        if not blocking:
            return "human gate cleared: dry_run off and live order confirmed"
        return "; ".join(blocking) + " -- a real order needs both dry_run off and an explicit live confirmation"

    # --- reads -------------------------------------------------------------

    def get_account_payload(self) -> Any:
        return self.client.get_account()

    def get_positions_payload(self) -> Any:
        return self.client.get_positions()

    def get_portfolio(self) -> Portfolio:
        return equity_portfolio(self.get_account_payload(), self.get_positions_payload())

    def get_quotes(self, *symbols: str) -> Any:
        return self.client.get_quotes(*symbols)

    # --- gates -------------------------------------------------------------

    def assert_agent_account(self, account_number: str | None) -> str:
        """Refuse any account but the pinned agent-tradable one.

        Public because this is the gate that must fire BEFORE the risk layer:
        an order aimed at the operator's default account is not a risk
        question, it is out of bounds regardless of what the rules would say.
        """
        target = account_number or self.account_number
        if target != self.account_number:
            raise AgentAccountMismatchError(
                f"refusing an equity order for account {target!r}; "
                f"the agent may only trade {self.account_number!r}"
            )
        return target

    @staticmethod
    def assert_equity_symbol(symbol: str) -> str:
        """Plain equity tickers only -- no options contracts in this lane."""
        candidate = str(symbol).strip()
        if not _EQUITY_SYMBOL.match(candidate):
            raise ValueError(
                f"{candidate!r} is not a plain equity ticker; this lane is long-only equities, no options"
            )
        return candidate.upper()

    @staticmethod
    def time_in_force(value: str | None) -> str:
        return _TIME_IN_FORCE.get(str(value or "gfd").lower(), "gfd")

    def _assert_kill_switch_open(self, symbol: str | None, side: str | None) -> None:
        if self.kill_switch is None:
            return
        halts = self.kill_switch.halt_reasons()
        if halts:
            reason = "kill switch is engaged: " + "; ".join(halts)
            self._log_refusal(symbol, side, reason)
            raise RuntimeError(reason)

    @contextmanager
    def forced_preview(self) -> Iterator[None]:
        """Disarm the broker for the duration of a block, then restore it.

        OrderManager's live-dry-run path calls place_limit_order the same way
        its live path does, so an armed broker would otherwise submit a real
        order during a preview run. The lane is single-threaded (one
        agent-hosted session), so swapping the flags around the call is safe.
        """
        armed = (self.dry_run, self.confirm_live_order)
        self.dry_run, self.confirm_live_order = True, False
        try:
            yield
        finally:
            self.dry_run, self.confirm_live_order = armed

    def _log_refusal(self, symbol: str | None, side: str | None, reason: str) -> None:
        if self.logger is None:
            return
        self.logger.log_decision(symbol, "equity_order_refused", reason, {"venue": VENUE, "side": side})

    # --- orders ------------------------------------------------------------

    def place_limit_order(self, order: dict[str, Any]) -> dict[str, Any]:
        """Place an equity limit order. `order` is the OrderManager payload.

        Identical in shape to AlpacaBroker.place_limit_order so OrderManager
        drives this lane with no special-casing.
        """
        symbol = self.assert_equity_symbol(order["symbol"])
        account_number = self.assert_agent_account(order.get("account_number"))
        side = str(order["side"]).lower()
        if side not in {"buy", "sell"}:
            raise ValueError(f"unsupported equity order side: {side!r}")

        order_payload = {
            "account_number": account_number,
            "symbol": symbol,
            "side": side,
            "type": "limit",
            "quantity": str(order["quantity"]),
            "limit_price": str(order["limit_price"]),
            "time_in_force": self.time_in_force(order.get("time_in_force")),
        }

        if not self.will_submit:
            return {
                **order,
                "submitted": False,
                "status": "dry_run_order_preview",
                "venue": VENUE,
                "account_number": account_number,
                "human_gate": self.gate_reason(),
                "order_payload": order_payload,
            }

        # Past this point the next call is irreversible: re-read the kill
        # switch, then hand the client both flags explicitly.
        self._assert_kill_switch_open(symbol, side)
        result = self.client.place_order(
            symbol=symbol,
            side=side,
            quantity=order_payload["quantity"],
            order_type="limit",
            limit_price=order_payload["limit_price"],
            time_in_force=order_payload["time_in_force"],
            account_number=account_number,
            dry_run=False,
            confirm_live_order=True,
        )
        return {
            **order,
            "submitted": True,
            "status": "submitted",
            "venue": VENUE,
            "account_number": account_number,
            "human_gate": self.gate_reason(),
            "order_payload": order_payload,
            "response": result.get("response", result) if isinstance(result, dict) else result,
        }

    def cancel_order(self, order_id: str) -> Any:
        if not self.will_submit:
            return {
                "id": order_id,
                "account_number": self.account_number,
                "status": "dry_run_cancel_prepared",
                "human_gate": self.gate_reason(),
            }
        self._assert_kill_switch_open(None, None)
        return self.client.cancel_order(
            order_id,
            account_number=self.account_number,
            dry_run=False,
            confirm_live_order=True,
        )

    # --- lane entry point ---------------------------------------------------

    def submit_signal(
        self,
        order_manager: OrderManager,
        signal: TradeSignal,
        limit_price: float,
        mode: str,
        portfolio: Portfolio,
        daily_summary: dict[str, Any],
        account_number: str | None = None,
        has_api_credentials: bool = True,
    ) -> dict[str, Any] | None:
        """Gate the account, then hand the signal to the SHARED OrderManager.

        The account check happens here, ahead of the risk layer, because an
        order aimed at the off-limits default account must be refused whatever
        the risk rules would have decided. Everything after it -- kill switch,
        caps, allowlist, cooldown, audit rationale -- is the existing
        OrderManager/RiskManager, unchanged.

        `has_api_credentials` defaults True because the OAuth connector IS the
        credential for this lane (agent/docs/rh-equities-binding.md); the
        client already proved it by resolving the account list through it.
        """
        logger = self.logger or order_manager.logger
        try:
            self.assert_agent_account(account_number)
        except AgentAccountMismatchError as exc:
            logger.log_decision(signal.symbol, "equity_order_refused", str(exc), {"venue": VENUE, "side": signal.side})
            raise

        def route() -> dict[str, Any] | None:
            return order_manager.process_signal(
                signal=signal,
                limit_price=limit_price,
                mode=mode,
                portfolio=portfolio,
                daily_summary=daily_summary,
                has_api_credentials=has_api_credentials,
            )

        if mode == "live":
            return route()
        # Any other mode is a preview or a paper fill; an armed broker must not
        # turn one into a real order.
        with self.forced_preview():
            return route()
