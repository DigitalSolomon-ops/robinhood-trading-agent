"""Robinhood OPTIONS broker over the authorized OAuth MCP connector.

Mirrors src/robinhood_equity_broker.py: the SAME OrderManager / RiskManager /
kill switch govern this lane -- there is no parallel risk path here. What the
options broker adds over the equities broker is the lane's arm gate: it reads
the shared ArmStore (src.shared_state.build_arm_store) OPTIONS lane and REFUSES
to submit unless that lane is armed, on top of the double human gate every lane
carries (dry_run off AND an explicit live confirmation).

Four things sit with the irreversible call inside submit_option_order, never in
a trusting caller -- exactly as the equities broker re-checks its universe, its
clock and its kill switch at the moment before the connector call:

  1. ACCOUNT. The order lands in the ONE Robinhood-designated agent-tradable
     account and nowhere else (the SAME account/identity anchor the equities
     lane pins, reused verbatim through RobinhoodOptionClient). Checked before
     the risk layer, because an order aimed at the off-limits default account is
     out of bounds regardless of what the rules would say.

  2. DOUBLE HUMAN GATE. Nothing is submitted unless BOTH dry_run is False AND
     confirm_live_order is True. Either flag alone, or neither, returns an
     unsubmitted payload preview and never reaches the connector. Compared by
     identity, not truthiness, so a stray non-boolean cannot arm the lane.

  3. OPTIONS LANE ARMED. Beyond the human gate, the shared ArmStore must report
     the OPTIONS lane armed. A disarmed lane -- or an absent / unreadable store
     -- refuses to submit even when both human-gate flags are set. This is the
     lane's hard rule: a real order needs confirm_live=True AND the options lane
     armed. Fail safe: an arm store that cannot answer reads as DISARMED.

  4. DEFINED RISK ONLY. Every leg set is validated before a gate is even
     consulted, by the ONE shared coverage-aware validator the client also uses
     (assert_defined_risk). Proving a short is genuinely covered needs
     strike-aware spread support this lane does not have yet, so until it lands
     ANY sell-to-open leg is refused outright -- a naked short, a ratio's
     uncovered shorts, a call 'covered' by a put, and a short of one underlying
     'covered' by a long of another all fail here. This is the runtime half of
     the static order-safety guard's property B
     (tests/test_option_order_safety_guard.py); an uncovered short is refused
     here before it can ever reach the connector.

READ-ONLY is the default posture: dry_run defaults to True, confirm_live_order
to False, so a broker built with no order flags cannot place an order. The kill
switch is re-consulted at the irreversible moment (STOP_TRADING_OPTIONS plus
TRADING_ENABLED), and a broker built without one still gets the lane's own
ROOT-anchored switch -- the emergency stop can never be silently skipped.

There is no API key and no base URL: the RobinhoodOptionClient's connector IS
the credential (session-bound OAuth); this broker only ever drives that client.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from .kill_switch import KillSwitch
from .logger import SQLiteLogger
from .option_risk_gates import (
    GateName,
    OptionOrderProposal,
    OptionRiskConfig,
    evaluate_option_order,
    resolve_granted_level,
)
from .order_manager import OrderManager
from .portfolio import Portfolio
from .robinhood_option_client import (
    OPTIONS_LANE,
    AgentAccountMismatchError,
    DefinedRiskViolationError,
    RobinhoodOptionClient,
    _dte_from_legs,
    assert_defined_risk,
)
from .shared_state import build_arm_store
from .strategy_engine import TradeSignal

# The DTE-family caps, relaxed only when an order's expiry is genuinely unknown
# at submit time (a bare-contract order). The dollar / size / approval-level caps
# are ALWAYS enforced; a real strategy-framed order carries a DTE and a KNOWN
# 0DTE is refused. Mirrors robinhood_option_client._DTE_GATE_NAMES.
_DTE_GATE_NAMES = frozenset({GateName.MIN_DTE, GateName.ZERO_DTE, GateName.EXPIRED})

VENUE = "robinhood_options"

# The lane repo root, so the fail-closed default kill switch and default arm
# store resolve to their own ROOT-anchored files regardless of the process's CWD.
# They are DIFFERENT files: the kill switch is STOP_TRADING_OPTIONS, the arm store
# is the POSITIVE ARM_STATE_OPTIONS marker -- the emergency stop and the arm gate
# are two independent controls, never the same file.
ROOT = Path(__file__).resolve().parents[1]


def _load_rules() -> dict[str, Any]:
    """config/trading_rules.yaml from ROOT, or {} if it cannot be read -- used to
    resolve the shared ArmStore's options stop-file convention. A missing config
    still yields a usable store (it falls back to the STOP_TRADING_OPTIONS
    default), never a crash."""
    try:
        with (ROOT / "config" / "trading_rules.yaml").open("r", encoding="utf-8") as handle:
            rules = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return rules if isinstance(rules, dict) else {}


def _default_options_kill_switch() -> KillSwitch:
    """Fail-closed default for a broker built without an explicit kill switch.

    A missing kill_switch must NEVER turn the broker into an unguarded submit
    path. Fall back to the options lane's OWN ROOT-anchored switch --
    STOP_TRADING_OPTIONS plus TRADING_ENABLED -- so the emergency stop is honored
    no matter where the process was launched from. Never the crypto lane's
    STOP_TRADING or the equities lane's STOP_TRADING_EQUITIES (different files)."""
    return KillSwitch(stop_file=str(ROOT / "STOP_TRADING_OPTIONS"), env_var="TRADING_ENABLED")


def _default_options_arm_store() -> Any:
    """Fail-closed default arm store: the shared, ROOT-anchored ArmStore for the
    options lane. A broker built with no store still consults the real arm state
    (the POSITIVE ARM_STATE_OPTIONS marker locally -- a DIFFERENT file from the
    kill switch, DISARMED unless the marker is present -- or Firestore in the
    cloud) rather than silently skipping the arm gate. _is_options_lane_armed()
    still reads an absent / unreadable store as DISARMED, so the failure mode
    stays fail-safe."""
    return build_arm_store(ROOT, _load_rules())


class RobinhoodOptionBroker:
    """Robinhood options broker on the shared risk machinery, plus the arm gate.

    Wraps a RobinhoodOptionClient. Provides the OrderManager-facing
    place_limit_order(order, mode) so the SAME OrderManager drives this lane with
    no special-casing, a legs-based submit_option_order for defined-risk spreads,
    and submit_signal as the lane entry point -- each of them ultimately reaching
    the connector only under account + double-gate + ARM + kill-switch + defined
    -risk checks that sit with the irreversible call.

    The broker and its client MUST share the same ArmStore: the client re-checks
    the arm gate at its own layer too (defense in depth), so a broker that
    approves an armed submit hands it to a client that agrees.
    """

    def __init__(
        self,
        client: RobinhoodOptionClient,
        arm_store: Any | None = None,
        lane: str = OPTIONS_LANE,
        dry_run: bool = True,
        confirm_live_order: bool = False,
        kill_switch: KillSwitch | None = None,
        logger: SQLiteLogger | None = None,
        rules: dict[str, Any] | None = None,
    ) -> None:
        self.client = client
        self.lane = lane
        self.dry_run = dry_run
        self.confirm_live_order = confirm_live_order
        # The options risk caps (config/trading_rules.yaml options.risk). Loaded
        # from ROOT when not injected, so a broker built with no rules still
        # enforces the caps rather than silently skipping them.
        self.rules = rules if rules is not None else _load_rules()
        self.risk_config = OptionRiskConfig.from_rules(self.rules)
        # Fail closed: a broker built with no arm store still gets the shared,
        # ROOT-anchored one, so the arm gate can never be silently skipped.
        self.arm_store = arm_store if arm_store is not None else _default_options_arm_store()
        # Fail closed, exactly like the arm store: a broker built with no kill
        # switch still gets the lane's own ROOT-anchored switch.
        self.kill_switch = kill_switch if kill_switch is not None else _default_options_kill_switch()
        self.logger = logger

    @property
    def account_number(self) -> str:
        """The pinned agent-tradable account, resolved by the client."""
        return self.client.account_number

    @property
    def will_submit(self) -> bool:
        """True only when both human-gate flags are explicitly set.

        Compared against the exact booleans by identity rather than truthiness so
        a stray non-boolean ("yes", 1) cannot arm the lane. This is only the
        HUMAN gate -- a live submit ALSO needs the options lane armed, checked
        separately at the irreversible moment.
        """
        return self.dry_run is False and self.confirm_live_order is True

    def gate_reason(self) -> str:
        """Human-readable reason this broker will not submit, for the audit log."""
        blocking = []
        if self.dry_run is not False:
            blocking.append("dry_run is on")
        if self.confirm_live_order is not True:
            blocking.append("no explicit live confirmation")
        if not self._is_options_lane_armed():
            blocking.append("options lane is not armed")
        if not blocking:
            return "human gate cleared: dry_run off, live order confirmed, options lane armed"
        return "; ".join(blocking) + " -- a real option order needs dry_run off, an explicit live confirmation, and the options lane armed"

    # --- reads -------------------------------------------------------------

    def get_positions(self) -> Any:
        return self.client.get_option_positions()

    def get_chains(self, symbol: str, **kwargs: Any) -> Any:
        return self.client.get_option_chains(symbol, **kwargs)

    def get_quotes(self, *contract_ids: str, **kwargs: Any) -> Any:
        return self.client.get_option_quotes(*contract_ids, **kwargs)

    def get_level_upgrade_info(self, **kwargs: Any) -> Any:
        return self.client.get_option_level_upgrade_info(**kwargs)

    def review_order(self, legs: Sequence[Mapping[str, Any]], **kwargs: Any) -> Any:
        """Robinhood's review step is non-committal, so this always reaches the
        connector -- the human gate and the arm gate apply to a submit, not to a
        preview. The payload is still defined-risk-validated first."""
        return self.client.review_order(legs, **kwargs)

    # --- gates -------------------------------------------------------------

    def assert_agent_account(self, account_number: str | None) -> str:
        """Refuse any account but the pinned agent-tradable one.

        Public because this is the gate that must fire BEFORE the risk layer: an
        order aimed at the operator's default account is not a risk question, it
        is out of bounds regardless of what the rules would say.
        """
        target = account_number or self.account_number
        if target != self.account_number:
            raise AgentAccountMismatchError(
                f"refusing an option order for account {target!r}; "
                f"the agent may only trade {self.account_number!r}"
            )
        return target

    def _is_options_lane_armed(self) -> bool:
        """True only when the shared ArmStore reports THIS lane armed. An absent
        store, or one that fails to answer, reads as DISARMED (fail safe): a real
        submit then can never fire, exactly as a disarmed lane."""
        store = self.arm_store
        if store is None:
            return False
        try:
            return bool(store.is_armed(self.lane))
        except Exception:
            return False

    def _assert_defined_risk(self, legs: Sequence[Mapping[str, Any]], direction: str) -> None:
        """Refuse any leg set that is not provably defined-risk -- the runtime
        half of the static guard's property B.

        Delegates to the ONE shared validator (assert_defined_risk) the client
        also uses, so the coverage rule lives in exactly one place: any
        sell-to-open leg is refused outright until strike-aware spread support
        exists (a ratio, a type-mismatched 'cover', a cross-underlying 'cover',
        and a lone naked short all fail here). Checked BEFORE any gate, so it is
        refused even on a disarmed, preview-only broker and can never reach the
        connector or the payload the static guard scans. The refusal is logged
        first, then re-raised, so the audit trail keeps the reason.
        """
        try:
            assert_defined_risk(legs, direction)
        except DefinedRiskViolationError as exc:
            self._log_refusal(None, str(exc))
            raise

    def _option_caps_block_reason(
        self,
        legs: Sequence[Mapping[str, Any]],
        direction: str,
        quantity: str,
        price: str | None,
        days_to_expiry: int | None,
        open_premium_at_risk_usd: float,
        max_loss_per_contract_usd: float | None,
    ) -> str | None:
        """Run the options risk caps over this order; return a single blocking
        reason, or None when every applicable cap passes.

        Sits with the irreversible call, never trusted from a caller: the debit /
        total-at-risk / contract-count / approval-level caps are ALWAYS enforced
        before the connector is reached, and the DTE floor binds whenever the
        expiry is known (an explicit days_to_expiry or one stamped on a leg). This
        is the runtime mirror of the strategy mapper's framing-time gates -- an
        armed broker still cannot push a 0DTE / over-contract / over-debit /
        over-level order past the caps. The client re-checks too (defense in
        depth), exactly as the defined-risk and arm gates are mirrored.
        """
        dte = days_to_expiry if days_to_expiry is not None else _dte_from_legs(legs)
        try:
            qty_int = int(quantity)
        except (TypeError, ValueError):
            qty_int = 0
        try:
            premium = float(price) if price is not None else 0.0
        except (TypeError, ValueError):
            premium = 0.0
        proposal = OptionOrderProposal(
            legs=legs,
            net_premium_per_contract=premium,
            quantity=qty_int,
            days_to_expiry=dte,
            direction=str(direction).strip().lower(),
            max_loss_per_contract_usd=max_loss_per_contract_usd,
        )
        decision = evaluate_option_order(
            self.risk_config,
            proposal,
            resolve_granted_level(self.client),
            max(float(open_premium_at_risk_usd), 0.0),
        )
        blocking = decision.blocking
        if dte is None:
            blocking = [result for result in blocking if result.name not in _DTE_GATE_NAMES]
        if blocking:
            return "; ".join(f"[{result.name}] {result.reason}" for result in blocking)
        return None

    def build_option_limit_order(
        self,
        signal: TradeSignal,
        limit_price: float,
        contracts: int = 1,
        time_in_force: str | None = None,
        account_number: str | None = None,
    ) -> dict[str, Any]:
        """The OPTIONS lane's OWN order builder -- deliberately NOT the equities
        dollar path (order_manager.build_limit_order).

        `quantity` is a whole CONTRACT COUNT, and the notional / at-risk figure is
        the true premium x contract-multiplier (100) x contracts. The equities
        builder sizes quantity = dollars / price as if one unit were one share,
        which for an option under-counts the real exposure ~100x (a $2.50 option
        is $250 of risk per contract, not $2.50). Sizing on the multiplier here
        is what makes the contract-count and dollar caps see the true exposure.
        """
        multiplier = self.risk_config.contract_multiplier
        qty = max(int(contracts), 1)
        premium = float(limit_price)
        notional = round(premium * multiplier * qty, 2)
        tif = time_in_force or (self.rules.get("orders") or {}).get("time_in_force", "gtc")
        return {
            "client_order_id": str(uuid.uuid4()),
            "symbol": signal.symbol,
            "side": signal.side,
            "order_type": "limit",
            "limit_price": round(premium, 8),
            "quantity": qty,  # a contract count, never a share count
            "contracts": qty,
            "notional": notional,  # true premium x 100 x contracts
            "at_risk_usd": notional,
            "time_in_force": tif,
            "reason": signal.reason,
            "strategy_signal": getattr(signal, "strategy_signal", None),
            "account_number": account_number,
        }

    def _contract_count(
        self, limit_price: float, amount_usd: float | None, contracts: int | None
    ) -> int:
        """Whole contracts to trade: an explicit `contracts` wins; otherwise a
        dollar budget is divided by the TRUE per-contract cost (premium x 100),
        floored to whole contracts, at least one. Dividing by the real per-
        contract cost -- not the per-share price -- is what keeps a dollar budget
        from being read as ~100x too many contracts."""
        if contracts is not None:
            return max(int(contracts), 1)
        premium = float(limit_price)
        per_contract = premium * self.risk_config.contract_multiplier
        if amount_usd is not None and per_contract > 0:
            return max(int(float(amount_usd) // per_contract), 1)
        return 1

    def _assert_kill_switch_open(self, symbol: str | None) -> None:
        # No fail-open path: self.kill_switch is guaranteed non-None by __init__
        # (a fail-closed default is substituted when none is wired), so the
        # emergency stop is always consulted at the irreversible moment.
        halts = self.kill_switch.halt_reasons()
        if halts:
            reason = "kill switch is engaged: " + "; ".join(halts)
            self._log_refusal(symbol, reason)
            raise RuntimeError(reason)

    def _log_refusal(self, symbol: str | None, reason: str) -> None:
        if self.logger is None:
            return
        self.logger.log_decision(symbol, "option_order_refused", reason, {"venue": VENUE})

    @contextmanager
    def forced_preview(self) -> Iterator[None]:
        """Disarm the broker for the duration of a block, then restore it.

        OrderManager's live-dry-run path calls place_limit_order the same way its
        live path does, so an armed broker would otherwise submit a real order
        during a preview run. The lane is single-threaded (one agent-hosted
        session), so swapping the flags around the call is safe.
        """
        armed = (self.dry_run, self.confirm_live_order)
        self.dry_run, self.confirm_live_order = True, False
        try:
            yield
        finally:
            self.dry_run, self.confirm_live_order = armed

    # --- orders ------------------------------------------------------------

    def submit_option_order(
        self,
        legs: Sequence[Mapping[str, Any]],
        direction: str = "debit",
        quantity: str = "1",
        order_type: str = "limit",
        price: str | None = None,
        time_in_force: str = "gtc",
        account_number: str | None = None,
        mode: str | None = None,
        days_to_expiry: int | None = None,
        open_premium_at_risk_usd: float = 0.0,
        max_loss_per_contract_usd: float | None = None,
    ) -> dict[str, Any]:
        """Place a DEFINED-RISK option order; submit only on the full gate.

        The connector is reached ONLY when the human gate is cleared (dry_run
        False AND confirm_live_order True) AND the options lane is ARMED AND the
        order clears the options risk caps (debit / total-at-risk / DTE /
        contracts / approval level) AND the run is genuinely live. Every other
        combination returns an unsubmitted preview and never touches the
        connector.

        `mode` is a SECOND, independent brake on top of the flags: a non-live
        mode ("live-dry-run" above all) forces a preview even on an armed broker,
        so reaching this via a preview run can never place a real order.
        """
        account = self.assert_agent_account(account_number)
        # Defined-risk FIRST: a naked short is refused here, before any gate is
        # even consulted, so it is rejected even on a disarmed, preview broker.
        self._assert_defined_risk(legs, direction)

        # Human gate: dry_run off AND an explicit live confirmation, plus a
        # genuinely live run. Anything short of all three returns a preview.
        live_mode = mode is None or mode == "live"
        if not (self.will_submit and live_mode):
            human_gate = self.gate_reason()
            if self.will_submit and not live_mode:
                human_gate = f"forced preview: run mode is {mode!r}, not a genuine live run"
            return self._preview(legs, direction, quantity, order_type, price, time_in_force, account, human_gate)

        # Second, independent fact: the options lane must be ARMED. Kept beside
        # the irreversible call, never trusted from a caller. A disarmed (or
        # absent / unreadable) store returns the same preview -- no connector call.
        if not self._is_options_lane_armed():
            return self._preview(
                legs, direction, quantity, order_type, price, time_in_force, account,
                "options lane is not armed -- a real option order needs the options lane armed",
                status="options_lane_disarmed",
            )

        # Third fact, beside the irreversible call: the order must clear the
        # options risk caps. A 0DTE / over-contract / over-debit / over-total /
        # over-level order returns a preview and never reaches the connector,
        # even fully gated and armed. Logged first so the audit keeps the reason.
        caps_block = self._option_caps_block_reason(
            legs, direction, quantity, price, days_to_expiry, open_premium_at_risk_usd, max_loss_per_contract_usd
        )
        if caps_block is not None:
            self._log_refusal(None, caps_block)
            return self._preview(
                legs, direction, quantity, order_type, price, time_in_force, account,
                caps_block, status="options_risk_gate_blocked",
            )

        # Past this point the next call is irreversible: re-read the kill switch,
        # then hand the client both flags explicitly. The client re-validates
        # defined risk, re-checks the shared arm store, AND re-runs the caps
        # (defense in depth).
        self._assert_kill_switch_open(None)
        result = self.client.place_option_order(
            legs=legs,
            direction=direction,
            quantity=quantity,
            order_type=order_type,
            price=price,
            time_in_force=time_in_force,
            account_number=account,
            dry_run=False,
            confirm_live_order=True,
            days_to_expiry=days_to_expiry,
            open_premium_at_risk_usd=open_premium_at_risk_usd,
            max_loss_per_contract_usd=max_loss_per_contract_usd,
        )
        return {
            "submitted": True,
            "status": "submitted",
            "venue": VENUE,
            "account_number": account,
            "human_gate": self.gate_reason(),
            "response": result.get("response", result) if isinstance(result, dict) else result,
            "order_payload": result.get("order_payload") if isinstance(result, dict) else None,
        }

    def _preview(
        self,
        legs: Sequence[Mapping[str, Any]],
        direction: str,
        quantity: str,
        order_type: str,
        price: str | None,
        time_in_force: str,
        account: str,
        human_gate: str,
        status: str = "dry_run_order_preview",
    ) -> dict[str, Any]:
        """An unsubmitted preview: the defined-risk-validated payload, no submit.

        Built through the client's build path (never the submit path), so the
        preview carries the exact connector-shaped payload a real order would --
        and still nothing reaches the connector.
        """
        payload = self.client.build_option_order(
            legs, direction, quantity, order_type, price, time_in_force, account
        )
        return {
            "submitted": False,
            "status": status,
            "venue": VENUE,
            "account_number": account,
            "human_gate": human_gate,
            "order_payload": payload,
        }

    def place_long_call(
        self,
        contract: str,
        quantity: str = "1",
        price: str | None = None,
        time_in_force: str = "gtc",
        account_number: str | None = None,
        mode: str | None = None,
        days_to_expiry: int | None = None,
    ) -> dict[str, Any]:
        """The scout's bread-and-butter play: BUY-to-open a single call/put.

        A long option is defined-risk by construction (max loss = premium paid),
        so this is the safest order the lane places. Routed through the same
        gated submit path as any other order -- including the risk caps, so a
        known 0DTE or over-debit long is still refused.
        """
        long_leg = {"side": "buy", "position_effect": "open", "ratio_quantity": 1, "option": contract}
        return self.submit_option_order(
            legs=[long_leg],
            direction="debit",
            quantity=quantity,
            price=price,
            time_in_force=time_in_force,
            account_number=account_number,
            mode=mode,
            days_to_expiry=days_to_expiry,
        )

    def place_limit_order(self, order: dict[str, Any], mode: str | None = None) -> dict[str, Any]:
        """OrderManager-facing entry: translate its limit order to an option leg.

        Kept shape-compatible with the equities/crypto brokers so the SAME
        OrderManager drives this lane. A buy is a buy-to-open long (defined risk
        by construction); a sell is a sell-to-CLOSE of a held long (reduces
        exposure, defined risk). Both route through the gated submit path.
        """
        contract = str(order["symbol"])
        side = str(order["side"]).lower()
        if side not in {"buy", "sell"}:
            raise ValueError(f"unsupported option order side: {side!r}")
        # buy = open a long (debit); sell = close a held long (credit). Neither
        # opens uncovered short risk, so _assert_defined_risk clears both.
        if side == "buy":
            leg = {"side": "buy", "position_effect": "open", "ratio_quantity": 1, "option": contract}
            direction = "debit"
        else:
            leg = {"side": "sell", "position_effect": "close", "ratio_quantity": 1, "option": contract}
            direction = "credit"
        return self.submit_option_order(
            legs=[leg],
            direction=direction,
            quantity=str(order.get("quantity", "1")),
            price=str(order["limit_price"]) if order.get("limit_price") is not None else None,
            time_in_force=str(order.get("time_in_force", "gtc")),
            account_number=order.get("account_number"),
            mode=mode,
            days_to_expiry=order.get("days_to_expiry"),
            open_premium_at_risk_usd=float(order.get("open_premium_at_risk_usd", 0.0) or 0.0),
            max_loss_per_contract_usd=order.get("max_loss_per_contract_usd"),
        )

    def cancel_order(self, order_id: str) -> Any:
        """Cancel an open option order. Cancelling REDUCES exposure, so it is NOT
        held to the ARM fact (requiring the lane armed to cancel would lock open
        orders in when disarmed -- the opposite of a kill switch). It still
        passes the double human gate and the kill switch."""
        if not self.will_submit:
            return {
                "id": order_id,
                "account_number": self.account_number,
                "status": "dry_run_cancel_prepared",
                "human_gate": self.gate_reason(),
            }
        self._assert_kill_switch_open(None)
        return self.client.cancel_order(
            order_id,
            account_number=self.account_number,
            dry_run=False,
            confirm_live_order=True,
        )

    # --- lane entry point ---------------------------------------------------

    @staticmethod
    def _skip_rationale(signal: TradeSignal) -> str:
        """Human-readable reason no order was attempted, for a non-actionable
        signal (a strategy hold) that never reaches the risk/compliance gates."""
        return (
            f"no option order attempted for {signal.symbol}: strategy signal is "
            f"'{signal.side}' ({signal.reason}); risk and arm gates were not evaluated"
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
        days_to_expiry: int | None = None,
        contracts: int | None = None,
        open_premium_at_risk_usd: float = 0.0,
    ) -> dict[str, Any] | None:
        """Size on the CONTRACT MULTIPLIER, then route through the lane's own
        gated submit path -- deliberately NOT the equities dollar path.

        A non-actionable signal (a strategy hold) is logged and returned first,
        since there is no order to size. The account check comes next, ahead of
        sizing, because an order aimed at the off-limits default account must be
        refused whatever the caps would decide.

        Sizing then uses this broker's OWN builder (build_option_limit_order):
        quantity is a whole CONTRACT COUNT and the notional / at-risk is premium
        x contract-multiplier (100) x contracts, so the exposure the caps see is
        the true figure -- not the ~100x under-count the share-based OrderManager
        path (order_manager.build_limit_order) would produce for an option. The
        order routes through place_limit_order -> submit_option_order, which
        re-checks the account, defined-risk, arm gate, kill switch AND the
        options risk caps (max_contracts, debit, total-at-risk, approval level)
        at the irreversible moment. A `mode` other than "live" forces a preview,
        so an armed broker cannot turn a preview/paper run into a real order.

        `order_manager` is retained for signature parity and its audit logger;
        the equities RiskManager dollar path it drives is not used for options,
        whose risk model is the options caps above.
        """
        logger = self.logger or order_manager.logger

        if signal.side not in {"buy", "sell"}:
            logger.log_decision(
                signal.symbol,
                "option_signal_skipped",
                self._skip_rationale(signal),
                {"venue": VENUE, "side": signal.side, "profile": signal.profile},
            )
            return None

        try:
            self.assert_agent_account(account_number)
        except AgentAccountMismatchError as exc:
            logger.log_decision(signal.symbol, "option_order_refused", str(exc), {"venue": VENUE, "side": signal.side})
            raise

        # OPTIONS-lane sizing: whole contracts + true premium x100 x contracts.
        count = self._contract_count(limit_price, amount_usd, contracts)
        order = self.build_option_limit_order(signal, limit_price, contracts=count, account_number=account_number)
        order["days_to_expiry"] = days_to_expiry
        order["open_premium_at_risk_usd"] = open_premium_at_risk_usd

        result = self.place_limit_order(order, mode=mode)
        submitted = bool(isinstance(result, dict) and result.get("submitted"))
        logger.log_decision(
            signal.symbol,
            "option_order_submitted" if submitted else "option_order_preview",
            signal.reason,
            {
                "venue": VENUE,
                "side": signal.side,
                "contracts": count,
                "limit_price": float(limit_price),
                "notional": order["notional"],
                "status": result.get("status") if isinstance(result, dict) else None,
            },
        )
        return result
