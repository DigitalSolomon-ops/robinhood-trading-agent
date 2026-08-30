from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from . import market_hours
from .equity_compliance import HISTORY_LOOKBACK_DAYS, PatternDayTraderGuard, SettlementGuard, assert_long_only
from .equity_symbols import equities_universe
from .kill_switch import KillSwitch
from .logger import SQLiteLogger
from .order_manager import OrderManager
from .portfolio import Portfolio, Position
from .robinhood_equity_client import AgentAccountMismatchError, RobinhoodEquityClient
from .strategy_engine import TradeSignal

VENUE = "robinhood_equities"

# The lane repo root, so a fail-closed default kill switch resolves to the SAME
# STOP_TRADING_EQUITIES file the runtime wires explicitly, regardless of CWD.
ROOT = Path(__file__).resolve().parents[1]


def _default_equities_kill_switch() -> KillSwitch:
    """Fail-closed default for a broker built without an explicit kill switch.

    A missing kill_switch must NEVER turn the broker into an unguarded submit
    path. Rather than skip the check (the old, wrong behavior), fall back to
    the equities lane's OWN ROOT-anchored switch -- STOP_TRADING_EQUITIES plus
    TRADING_ENABLED -- the same file equity_runtime.equity_kill_switch wires in
    production, so the emergency stop is honored no matter where the process
    was launched from. Never the crypto lane's STOP_TRADING (a different file)."""
    return KillSwitch(stop_file=str(ROOT / "STOP_TRADING_EQUITIES"), env_var="TRADING_ENABLED")


def _normalize_universe(symbols: Iterable[str]) -> frozenset[str]:
    """Universe membership keys: stripped and upper-cased, the same form
    assert_equity_symbol returns, so a lowercase literal ('gevity') is compared
    against the configured list rather than sliding past it on case."""
    return frozenset(
        str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()
    )


def _default_equity_universe() -> frozenset[str]:
    """Fail-closed default universe for a broker built without an explicit one.

    Reads config/trading_rules.yaml's `equities.universe` from the lane ROOT --
    the SAME list src.equity_symbols.equities_universe returns and the same one
    equity_runtime hands the shared RiskManager as its allowlist, so the two
    layers can never disagree about what this lane may trade.

    A missing or unreadable config yields an EMPTY universe, which refuses
    every symbol. The failure mode of a universe check that cannot read its own
    list must be "trade nothing", never "trade anything".
    """
    try:
        with (ROOT / "config" / "trading_rules.yaml").open("r", encoding="utf-8") as handle:
            rules = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return frozenset()
    if not isinstance(rules, dict):
        return frozenset()
    return _normalize_universe(equities_universe(rules))

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
    3. at the irreversible moment, the kill switch is re-checked, the symbol is
       re-checked against the configured `equities.universe`, and a real order
       is refused outright if STOP_TRADING exists, TRADING_ENABLED is not true,
       or the ticker is not one this lane is configured to trade. The universe
       check duplicates the shared RiskManager allowlist ON PURPOSE: a caller
       that reaches place_limit_order directly, bypassing OrderManager, must
       still be unable to send Robinhood a ticker nobody approved -- and the
       comparison is case-folded, so a lowercase off-universe literal
       ('gevity') is refused rather than quietly up-cased into a live order;
    4. the Agentic account's own compliance shape: long-only (a sell beyond
       the held quantity is refused as a short), the FINRA pattern-day-trader
       guard (blocks a 4th same-symbol day trade in 5 business days while
       account equity is under $25k), and the cash-account good-faith /
       settlement guard (a buy cannot spend still-unsettled sale proceeds --
       this account has no margin to draw on instead). See equity_compliance.py.

    READ-ONLY is the default posture: dry_run defaults to True and
    confirm_live_order to False, so a broker built with no arguments cannot
    place an order. Flipping either is the operator's explicit call.

    Equities are not 24/7: `allow_extended_hours` is the config-gated
    opt-out for pre/post-market submission and defaults OFF, so a broker
    built with no arguments only ever submits inside regular trading hours
    (RTH) -- weekends and market holidays are refused regardless.
    """

    def __init__(
        self,
        client: RobinhoodEquityClient,
        dry_run: bool = True,
        confirm_live_order: bool = False,
        kill_switch: KillSwitch | None = None,
        logger: SQLiteLogger | None = None,
        allow_extended_hours: bool = False,
        clock: Callable[[], datetime] | None = None,
        universe: Iterable[str] | None = None,
    ) -> None:
        self.client = client
        self.dry_run = dry_run
        self.confirm_live_order = confirm_live_order
        # Fail closed, exactly like the kill switch below: a broker built with
        # no universe still gets one -- config's equities.universe, read from
        # ROOT -- so the membership check can never be silently skipped. An
        # unreadable config leaves this empty, which refuses every symbol.
        self.universe = (
            _normalize_universe(universe) if universe is not None else _default_equity_universe()
        )
        # Fail closed: a broker built with no kill switch still gets one -- the
        # ROOT-anchored equities switch -- so the irreversible-moment re-check
        # below can never be silently skipped.
        self.kill_switch = kill_switch if kill_switch is not None else _default_equities_kill_switch()
        self.logger = logger
        self.allow_extended_hours = allow_extended_hours
        self._clock = clock or (lambda: datetime.now(market_hours.EASTERN))

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

    def assert_in_universe(self, symbol: str, side: str | None = None) -> str:
        """Refuse a ticker the configured `equities.universe` does not list.

        Independent of the shared RiskManager allowlist on purpose. RiskManager
        checks membership for orders that arrive through OrderManager; this
        checks it again at the broker, immediately before the connector call,
        so a caller that reaches place_limit_order directly still cannot send
        Robinhood a symbol nobody approved. Comparison is on the upper-cased,
        stripped form both sides normalize to, so 'gevity' and 'GEVITY' are the
        same lookup -- up-casing a literal must not be what makes it tradable.
        """
        candidate = str(symbol).strip().upper()
        if candidate not in self.universe:
            listed = ", ".join(sorted(self.universe)) or "(none configured)"
            reason = (
                f"refusing an equity order for {candidate}: it is not in this lane's configured "
                f"equities universe [{listed}]"
            )
            self._log_refusal(symbol, side, reason)
            raise RuntimeError(reason)
        return candidate

    @staticmethod
    def time_in_force(value: str | None) -> str:
        return _TIME_IN_FORCE.get(str(value or "gfd").lower(), "gfd")

    @staticmethod
    def _order_notional(order: dict[str, Any]) -> float:
        """The order's notional, recomputed from the AUTHORITATIVE fields
        (quantity * limit_price) rather than trusted from a caller-supplied
        `notional` key. The anti-margin / good-faith guard leans on this to
        decide whether a buy fits inside settled cash; a caller that omits
        notional (or passes 0) must not be able to slip an order past that
        guard, so a missing or non-positive value RAISES here instead of
        defaulting to 0.0."""
        try:
            quantity = float(order["quantity"])
            limit_price = float(order["limit_price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "cannot compute order notional: both quantity and limit_price are required"
            ) from exc
        notional = quantity * limit_price
        if notional <= 0:
            raise ValueError(
                f"order notional must be positive; got quantity={quantity} limit_price={limit_price}"
            )
        return notional

    def _assert_kill_switch_open(self, symbol: str | None, side: str | None) -> None:
        # No fail-open path: self.kill_switch is guaranteed non-None by __init__
        # (a fail-closed default is substituted when none is wired), so the
        # emergency stop is always consulted at the irreversible moment.
        halts = self.kill_switch.halt_reasons()
        if halts:
            reason = "kill switch is engaged: " + "; ".join(halts)
            self._log_refusal(symbol, side, reason)
            raise RuntimeError(reason)

    def _assert_regular_hours(self, symbol: str | None, side: str | None) -> None:
        """Refuse a real order outside RTH (or the extended-hours window,
        when explicitly opted in). Weekends and holidays are handled inside
        market_hours.blocked_reason without raising anything but this clean,
        human-readable RuntimeError -- there is no crash path here, just a
        refusal like every other gate in this broker."""
        reason = market_hours.blocked_reason(self._clock(), allow_extended_hours=self.allow_extended_hours)
        if reason:
            self._log_refusal(symbol, side, reason)
            raise RuntimeError(reason)

    def _order_history(self, as_of: datetime) -> list[dict[str, Any]]:
        if self.logger is None:
            return []
        cutoff = (as_of - timedelta(days=HISTORY_LOOKBACK_DAYS)).isoformat()
        return self.logger.get_orders_since(cutoff)

    def _assert_long_only(self, symbol: str, side: str, order_quantity: float, portfolio: Portfolio) -> None:
        """No shorts, ever -- checked here regardless of what trading_rules.yaml's
        allow_shorting flag says, since that flag is the crypto lane's to set."""
        decision = assert_long_only(symbol, side, order_quantity, portfolio.quantity_for(symbol))
        if not decision.allowed:
            self._log_refusal(symbol, side, decision.reason)
            raise RuntimeError(decision.reason)

    def _assert_pdt_guard(
        self, symbol: str, side: str, portfolio: Portfolio, history: list[dict[str, Any]], as_of: datetime
    ) -> None:
        decision = PatternDayTraderGuard(history).evaluate(symbol, side, portfolio.equity(), as_of)
        if not decision.allowed:
            self._log_refusal(symbol, side, decision.reason)
            raise RuntimeError(decision.reason)

    def _assert_settlement_guard(
        self, symbol: str, side: str, notional: float, portfolio: Portfolio, history: list[dict[str, Any]], as_of: datetime
    ) -> None:
        """Also the account's anti-margin check: the Agentic account is cash-only,
        so a buy that does not fit inside settled cash has nowhere else to draw from."""
        decision = SettlementGuard(history).evaluate(side, notional, portfolio.cash_usd, as_of)
        if not decision.allowed:
            self._log_refusal(symbol, side, decision.reason)
            raise RuntimeError(decision.reason)

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

    def place_limit_order(self, order: dict[str, Any], mode: str | None = None) -> dict[str, Any]:
        """Place an equity limit order. `order` is the OrderManager payload.

        Kept shape-compatible with the crypto LiveBroker so OrderManager drives
        this lane with no special-casing.

        `mode` is the run mode OrderManager is in. It is a SECOND, independent
        brake on top of the dry_run/confirm_live_order flags: a real order is
        submitted only when the run is genuinely live (mode is "live" or
        unspecified). Any non-live mode -- "live-dry-run" above all -- forces a
        preview even on an armed broker, so reaching this method directly via
        process_signal(mode="live-dry-run") (bypassing forced_preview) can
        never place a real order.
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

        # A non-live mode forces a preview regardless of how the flags are set.
        live_mode = mode is None or mode == "live"
        if not (self.will_submit and live_mode):
            human_gate = self.gate_reason()
            if self.will_submit and not live_mode:
                human_gate = f"forced preview: run mode is {mode!r}, not a genuine live run"
            return {
                **order,
                "submitted": False,
                "status": "dry_run_order_preview",
                "venue": VENUE,
                "account_number": account_number,
                "human_gate": human_gate,
                "order_payload": order_payload,
            }

        # Past this point the next call is irreversible: re-check what this lane
        # is allowed to trade, re-read the market clock and the kill switch,
        # then hand the client both flags explicitly.
        self.assert_in_universe(symbol, side)
        self._assert_regular_hours(symbol, side)
        self._assert_kill_switch_open(symbol, side)
        as_of = self._clock()
        history = self._order_history(as_of)
        portfolio_snapshot = self.get_portfolio()
        # Authoritative notional -- never the caller's own `notional` field --
        # so the settlement/anti-margin guard cannot be defeated by omitting it.
        notional = self._order_notional(order)
        self._assert_long_only(symbol, side, float(order["quantity"]), portfolio_snapshot)
        self._assert_pdt_guard(symbol, side, portfolio_snapshot, history, as_of)
        self._assert_settlement_guard(symbol, side, notional, portfolio_snapshot, history, as_of)
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

    @staticmethod
    def _skip_rationale(signal: TradeSignal) -> str:
        """Human-readable reason no order was attempted, for a signal that
        never reaches the risk/compliance gates below because it is not
        actionable in the first place (a strategy hold). Read back the
        signal's own reason so the audit log says WHY the strategy held,
        not just that it did."""
        return (
            f"no order attempted for {signal.symbol}: strategy signal is "
            f"'{signal.side}' ({signal.reason}); risk and compliance gates were not evaluated"
        )

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
        amount_usd: float | None = None,
    ) -> dict[str, Any] | None:
        """Gate the account, then hand the signal to the SHARED OrderManager.

        A non-actionable signal (a strategy hold) is logged and returned here,
        before the account/risk gates, since there is no order to gate -- this
        is the "skip" half of every equities decision writing a readable
        rationale; the "act" half is the existing OrderManager/RiskManager
        below, unchanged.

        The account check happens next, ahead of the risk layer, because an
        order aimed at the off-limits default account must be refused whatever
        the risk rules would have decided. Everything after it -- kill switch,
        caps, allowlist, cooldown, audit rationale -- is the existing
        OrderManager/RiskManager, unchanged.

        `has_api_credentials` defaults True because the OAuth connector IS the
        credential for this lane (agent/docs/rh-equities-binding.md); the
        client already proved it by resolving the account list through it.

        `amount_usd` is an optional REDUCED per-trade cap for this one signal
        (the market-regime brake in src/equity_intelligence/market_regime.py
        passes a scaled-down cap in a weak market). It is handed straight to
        OrderManager and changes nothing else: RiskManager still measures the
        resulting notional against the configured risk.max_trade_amount_usd, so
        this can only shrink an order, never enlarge one or excuse it a gate.
        """
        logger = self.logger or order_manager.logger

        if signal.side not in {"buy", "sell"}:
            logger.log_decision(
                signal.symbol,
                "equity_signal_skipped",
                self._skip_rationale(signal),
                {"venue": VENUE, "side": signal.side, "profile": signal.profile},
            )
            return None

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
                amount_usd=amount_usd,
            )

        if mode == "live":
            return route()
        # Any other mode is a preview or a paper fill; an armed broker must not
        # turn one into a real order.
        with self.forced_preview():
            return route()
