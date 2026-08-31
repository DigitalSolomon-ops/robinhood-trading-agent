"""Robinhood OPTIONS client backed by the authorized OAuth MCP connector.

Mirrors src/robinhood_equity_client.py: account / positions / chain / quote reads,
a non-committal review path, and a place path that BUILDS a payload but reaches
the connector only under an explicit double human gate. Unlike the equities
client this lane also carries the OPTIONS-specific safety property -- DEFINED
RISK ONLY, no naked short -- enforced at construction of every order payload.

Three things make an option order irreversible-safe here, and all three sit with
the submit call inside place_option_order, never in a trusting caller:

  1. ACCOUNT. The same single agent-tradable account as the equities lane is
     resolved and pinned. The connector self-declares it with `agentic_allowed`,
     which is necessary but NOT sufficient: the flag-selected account is
     cross-checked against the out-of-band expected identity (nickname AND
     account-number suffix) loaded from config/trading_rules.yaml
     (equities.expected_account -- the SAME anchor, reused verbatim). A flipped
     flag on the off-limits default account is refused, never pinned.

  2. DOUBLE GATE + ARM. place_option_order submits ONLY when dry_run is exactly
     False AND confirm_live_order is exactly True AND the shared ArmStore reports
     the OPTIONS lane armed. Every other combination -- either flag alone, a
     truthy-but-not-True confirm, a disarmed lane -- returns the same unsubmitted
     preview and never touches the connector. This matches the lane's hard rule:
     a real order requires BOTH confirm_live=True AND the options lane armed.

  3. DEFINED RISK. Every order payload is validated before it is built: a
     sell-to-open leg with no covering buy-to-open leg is refused, as is a credit
     opening order with no long leg. Single-leg long calls/puts (buy-to-open,
     debit) and defined-risk vertical spreads (long + short, debit or credit,
     the short always covered by a long) are allowed; a naked / uncovered short
     is refused at build time, so it can never reach the payload the guard scans.

There is no API key and no base URL: the connector object IS the credential
(session-bound OAuth), and this class only ever calls its named tool methods.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

# Reuse the equities lane's account-identity anchor verbatim -- SAME account,
# SAME out-of-band expected identity, SAME exceptions. The options lane must not
# fork the anchor: a second copy is a second thing to keep in sync.
from .robinhood_equity_client import (
    AgentAccountIdentityError,
    AgentAccountMismatchError,
    NoAgentTradableAccountError,
    _load_expected_account,
)

# The lane this client's arm gate reads from the shared ArmStore.
OPTIONS_LANE = "options"

# Re-exported so callers can catch them from this module too.
__all__ = [
    "AgentAccountIdentityError",
    "AgentAccountMismatchError",
    "NoAgentTradableAccountError",
    "DefinedRiskViolationError",
    "OptionConnector",
    "RobinhoodOptionClient",
    "OPTIONS_LANE",
]


class OptionConnector(Protocol):
    """Duck-typed shape of the authorized Robinhood OPTIONS MCP connector.

    A test satisfies this with a plain stub that records calls; nothing here is
    an HTTP client and there is no key to hold.
    """

    def get_option_chains(self, **kwargs: Any) -> Any: ...
    def get_option_quotes(self, **kwargs: Any) -> Any: ...
    def get_option_positions(self, account_number: str | None = None) -> Any: ...
    def get_option_level_upgrade_info(self, **kwargs: Any) -> Any: ...
    def get_accounts(self) -> Any: ...
    def review_option_order(self, **kwargs: Any) -> Any: ...
    def place_option_order(self, **kwargs: Any) -> Any: ...
    def cancel_option_order(self, order_id: str, account_number: str | None = None) -> Any: ...


class _ArmStore(Protocol):
    def is_armed(self, lane: str) -> bool: ...


class DefinedRiskViolationError(RuntimeError):
    """An order payload would open a SELL leg with no covering BUY-to-open leg
    (a naked / uncovered short, or a credit opening order with no long leg).

    DEFINED RISK is the lane's floor. This is raised at BUILD time, before the
    payload exists, so an undefined-risk order can never be handed to the
    connector or reach the static order-safety guard as a literal.
    """


class RobinhoodOptionClient:
    """Robinhood options client over the authorized OAuth MCP connector.

    Resolves and pins the single agent-tradable account on construction, exposes
    read-only chain/quote/position/account tools, and gates every order path on
    the pinned account plus -- for a live submit -- the double confirm/arm gate.
    """

    def __init__(
        self,
        connector: OptionConnector,
        arm_store: _ArmStore | None = None,
        lane: str = OPTIONS_LANE,
        expected_account: Mapping[str, str] | None = None,
        config_root: Path | str | None = None,
    ) -> None:
        self._connector = connector
        # A missing arm store reads as DISARMED (fail safe): a real submit then
        # can never fire, exactly as a disarmed lane. The store is the shared
        # ArmStore (src.shared_state.build_arm_store) in production.
        self._arm_store = arm_store
        self._lane = lane
        self._expected_account = (
            dict(expected_account) if expected_account is not None else _load_expected_account(config_root)
        )
        self.account = self._resolve_agent_account()
        self.account_number = self.account["account_number"]
        self.nickname = self.account.get("nickname")

    # --- account resolution (identical anchor to the equities client) ---------

    def _resolve_agent_account(self) -> dict[str, Any]:
        payload = self._connector.get_accounts()
        if isinstance(payload, dict):
            accounts = payload.get("accounts", payload.get("results", []))
        else:
            accounts = payload or []
        agentic = [account for account in accounts if account.get("agentic_allowed")]
        if len(agentic) != 1:
            raise NoAgentTradableAccountError(
                f"connector reports {len(agentic)} agentic-allowed accounts; expected exactly 1"
            )
        account = agentic[0]
        self._assert_expected_identity(account)
        return account

    def _assert_expected_identity(self, account: Mapping[str, Any]) -> None:
        expected_nickname = self._expected_account["nickname"]
        expected_suffix = self._expected_account["number_suffix"]
        nickname = account.get("nickname")
        number = str(account.get("account_number", ""))
        if nickname != expected_nickname or not number.endswith(expected_suffix):
            raise AgentAccountIdentityError(
                f"agentic_allowed account {number!r} (nickname {nickname!r}) does not match the "
                f"expected agent identity (nickname {expected_nickname!r}, number ending {expected_suffix!r}); "
                "the self-declared agentic_allowed flag is necessary but not sufficient"
            )

    def _assert_agent_account(self, account_number: str | None) -> str:
        target = account_number or self.account_number
        if target != self.account_number:
            raise AgentAccountMismatchError(
                f"refusing option order for account {target!r}; the agent may only trade {self.account_number!r}"
            )
        return target

    def _is_options_lane_armed(self) -> bool:
        """True only when a shared ArmStore reports THIS lane armed. No store,
        or a store that fails to answer, reads as disarmed (fail safe)."""
        store = self._arm_store
        if store is None:
            return False
        try:
            return bool(store.is_armed(self._lane))
        except Exception:
            return False

    # --- reads ----------------------------------------------------------------

    def get_option_chains(self, symbol: str, **kwargs: Any) -> Any:
        return self._connector.get_option_chains(symbol=symbol, **kwargs)

    def get_option_quotes(self, *contract_ids: str, **kwargs: Any) -> Any:
        return self._connector.get_option_quotes(ids=list(contract_ids), **kwargs)

    def get_option_positions(self) -> Any:
        return self._connector.get_option_positions(account_number=self.account_number)

    def get_option_level_upgrade_info(self, **kwargs: Any) -> Any:
        return self._connector.get_option_level_upgrade_info(**kwargs)

    def get_accounts(self) -> Any:
        return self._connector.get_accounts()

    def get_account(self) -> dict[str, Any]:
        """The resolved, pinned agent-tradable account (cached from init)."""
        return self.account

    # --- leg / payload construction (DEFINED RISK ONLY) -----------------------

    @staticmethod
    def _normalize_leg(leg: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize one leg to the connector's shape, lower-casing the two
        classifying fields so the defined-risk check reads them uniformly."""
        side = leg.get("side")
        effect = leg.get("position_effect")
        normalized: dict[str, Any] = {
            "side": str(side).strip().lower() if side is not None else side,
            "position_effect": str(effect).strip().lower() if effect is not None else effect,
            "ratio_quantity": leg.get("ratio_quantity", 1),
        }
        # The contract reference under whichever key the caller supplied.
        for key in ("option", "option_id", "instrument", "contract_ticker"):
            if key in leg and leg[key] is not None:
                normalized["option"] = leg[key]
                break
        return normalized

    def _assert_defined_risk(self, legs: Sequence[Mapping[str, Any]], direction: str) -> None:
        """Refuse any leg set that opens uncovered short risk.

        The runtime half of the static order-safety guard's property B: an
        opening SELL leg is allowed ONLY when the same order also opens a BUY
        leg to cover it (a vertical spread), and a credit OPENING order with no
        long leg is refused outright. A covered call does NOT qualify -- the
        cover here must be an option leg, not stock.
        """
        opening = [leg for leg in legs if str(leg.get("position_effect")).strip().lower() == "open"]
        opening_sells = [leg for leg in opening if str(leg.get("side")).strip().lower() == "sell"]
        opening_buys = [leg for leg in opening if str(leg.get("side")).strip().lower() == "buy"]
        if opening_sells and not opening_buys:
            raise DefinedRiskViolationError(
                "refusing an uncovered short: a sell-to-open leg has no covering buy-to-open leg "
                "(naked short / undefined risk); this lane is defined-risk only"
            )
        if str(direction).strip().lower() == "credit" and opening_sells and not opening_buys:
            raise DefinedRiskViolationError(
                "refusing a credit opening order with no buy-to-open leg (undefined risk)"
            )

    def build_option_order(
        self,
        legs: Sequence[Mapping[str, Any]],
        direction: str = "debit",
        quantity: str = "1",
        order_type: str = "limit",
        price: str | None = None,
        time_in_force: str = "gtc",
        account_number: str | None = None,
    ) -> dict[str, Any]:
        """Validate + assemble an option order payload WITHOUT submitting it.

        This is the review/build path. It pins the account, refuses an
        undefined-risk leg set, and returns the connector-shaped payload. Callers
        that only want a preview use this or review_order; nothing here can reach
        place_option_order.
        """
        target = self._assert_agent_account(account_number)
        if not legs:
            raise DefinedRiskViolationError("refusing an option order with no legs")
        normalized = [self._normalize_leg(leg) for leg in legs]
        self._assert_defined_risk(normalized, direction)
        payload: dict[str, Any] = {
            "account_number": target,
            "direction": str(direction).strip().lower(),
            "legs": normalized,
            "quantity": str(quantity),
            "type": order_type,
            "time_in_force": time_in_force,
        }
        if price is not None:
            payload["price"] = str(price)
        return payload

    def build_single_leg_long(
        self,
        contract: str,
        quantity: str = "1",
        order_type: str = "limit",
        price: str | None = None,
        time_in_force: str = "gtc",
        account_number: str | None = None,
    ) -> dict[str, Any]:
        """The scout's bread-and-butter play: BUY-to-open a single call or put.

        A long option is defined-risk by construction (max loss = premium paid),
        so this is the safest order the lane places. The direction is a debit and
        the one leg is a buy-to-open; no short risk is opened.
        """
        long_leg = {"side": "buy", "position_effect": "open", "ratio_quantity": 1, "option": contract}
        return self.build_option_order(
            legs=[long_leg],
            direction="debit",
            quantity=quantity,
            order_type=order_type,
            price=price,
            time_in_force=time_in_force,
            account_number=account_number,
        )

    # --- review (non-committal, always reaches the connector) -----------------

    def review_order(
        self,
        legs: Sequence[Mapping[str, Any]],
        direction: str = "debit",
        quantity: str = "1",
        order_type: str = "limit",
        price: str | None = None,
        time_in_force: str = "gtc",
        account_number: str | None = None,
    ) -> Any:
        """Ask the connector to PREVIEW an order. Robinhood's review step is
        itself non-committal, so this always reaches review_option_order -- the
        human gate below applies to place_order, not to a preview. The payload
        is still built through the defined-risk validator first."""
        payload = self.build_option_order(
            legs, direction, quantity, order_type, price, time_in_force, account_number
        )
        return self._connector.review_option_order(**payload)

    # --- place (double gate + arm; the only path that can submit) -------------

    def place_option_order(
        self,
        legs: Sequence[Mapping[str, Any]],
        direction: str = "debit",
        quantity: str = "1",
        order_type: str = "limit",
        price: str | None = None,
        time_in_force: str = "gtc",
        account_number: str | None = None,
        dry_run: bool = True,
        confirm_live_order: bool = False,
    ) -> dict[str, Any]:
        """Build a DEFINED-RISK option order; submit it only on the full gate.

        READ-ONLY is the default posture. The connector's place_option_order is
        reached ONLY when dry_run is exactly False AND confirm_live_order is
        exactly True AND the shared ArmStore reports the options lane armed.
        Every other combination returns the same unsubmitted preview.

        Identity, not truthiness: `dry_run is False and confirm_live_order is
        True` -- a truthy non-boolean confirm ("yes") or a falsy non-boolean
        dry_run (0) must NOT arm the lane.
        """
        # Build (and defined-risk-validate) the payload first -- a naked short is
        # refused here, before any gate is even consulted.
        payload = self.build_option_order(
            legs, direction, quantity, order_type, price, time_in_force, account_number
        )
        if not (dry_run is False and confirm_live_order is True):
            return {
                "submitted": False,
                "status": "dry_run_order_preview",
                "venue": "robinhood_options",
                "order_payload": payload,
            }
        # Second, independent fact: the options lane must be ARMED. Kept beside
        # the irreversible call, never trusted from a caller. A disarmed (or
        # absent) store returns the same preview -- no connector call.
        if not self._is_options_lane_armed():
            return {
                "submitted": False,
                "status": "options_lane_disarmed",
                "venue": "robinhood_options",
                "order_payload": payload,
            }
        response = self._connector.place_option_order(**payload)
        return {
            "submitted": True,
            "status": "submitted",
            "venue": "robinhood_options",
            "order_payload": payload,
            "response": response,
        }

    def cancel_order(
        self,
        order_id: str,
        account_number: str | None = None,
        dry_run: bool = True,
        confirm_live_order: bool = False,
    ) -> Any:
        """Cancel an open option order. Cancelling REDUCES exposure, so it is not
        held to the ARM fact (requiring the lane armed to cancel would lock open
        orders in when disarmed -- the opposite of a kill switch). It still
        passes the same dry_run/confirm gate at runtime."""
        target = self._assert_agent_account(account_number)
        if not (dry_run is False and confirm_live_order is True):
            return {"id": order_id, "account_number": target, "status": "dry_run_cancel_prepared"}
        return self._connector.cancel_option_order(order_id=order_id, account_number=target)
