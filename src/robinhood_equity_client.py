from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Protocol

import yaml

# The repo root (agent/) relative to this file (agent/src/robinhood_equity_client.py),
# used to locate config/trading_rules.yaml when no expected identity is injected.
_DEFAULT_ROOT = Path(__file__).resolve().parents[1]


class EquityConnector(Protocol):
    """Duck-typed shape of the authorized Robinhood equities MCP connector.

    This is not an HTTP client and there is no key to hold -- the connector
    itself is the credential (agent/docs/rh-equities-binding.md). Whatever
    object the harness hands in at construction time only needs to expose
    these methods; a test can satisfy this with a plain stub.
    """

    def get_equity_quotes(self, symbols: list[str]) -> Any: ...
    def get_equity_positions(self, account_number: str | None = None) -> Any: ...
    def get_accounts(self) -> Any: ...
    def review_equity_order(self, **kwargs: Any) -> Any: ...
    def place_equity_order(self, **kwargs: Any) -> Any: ...
    def cancel_equity_order(self, order_id: str, account_number: str | None = None) -> Any: ...


class NoAgentTradableAccountError(RuntimeError):
    """The connector's account list has zero or more than one agentic-allowed account."""


class AgentAccountMismatchError(RuntimeError):
    """An order path targeted an account other than the pinned agent-tradable one."""


class AgentAccountIdentityError(RuntimeError):
    """The agentic_allowed account failed the out-of-band identity cross-check.

    The connector self-declares which account is agent-tradable with the
    ``agentic_allowed`` boolean. That flag is necessary but NOT sufficient: the
    flag-selected account must also match the expected identity configured out
    of band (nickname AND account-number suffix). A connector (or an attacker
    who can shape its payload) that flips ``agentic_allowed`` on the off-limits
    default account is refused here, never pinned.
    """


def _load_expected_account(config_root: Path | str | None) -> dict[str, str]:
    """Read the expected agent-account identity from config/trading_rules.yaml.

    This is the out-of-band anchor the self-declared ``agentic_allowed`` flag is
    checked against. It is mandatory: a missing or incomplete
    ``equities.expected_account`` is a hard error, so the identity check can
    never be silently skipped.
    """
    base = Path(config_root) if config_root is not None else _DEFAULT_ROOT
    path = base / "config" / "trading_rules.yaml"
    with path.open("r", encoding="utf-8") as handle:
        rules = yaml.safe_load(handle) or {}
    expected = (rules.get("equities") or {}).get("expected_account") or {}
    nickname = expected.get("nickname")
    number_suffix = expected.get("number_suffix")
    if not nickname or not number_suffix:
        raise AgentAccountIdentityError(
            "config equities.expected_account must set both nickname and number_suffix; "
            "the agent-account identity anchor cannot be skipped"
        )
    return {"nickname": str(nickname), "number_suffix": str(number_suffix)}


class RobinhoodEquityClient:
    """Robinhood equities client backed by the authorized OAuth MCP connector.

    Mirrors the INTERFACE shape of alpaca_client.AlpacaClient (account /
    positions reads, place_order, cancel_order) so a broker layer above this
    can swap venues. Unlike AlpacaClient there is no base URL and no API key:
    the backend is a connector object the harness supplies, and this class
    only ever calls its named tool methods.

    Robinhood confines agent trading to exactly one account (nickname
    "Agentic"). On construction this resolves that account from the
    connector's own account list and pins it; every order path below
    re-asserts the pinned account and refuses any other, so a caller bug can
    never route an order at the off-limits default account.
    """

    def __init__(
        self,
        connector: EquityConnector,
        expected_account: Mapping[str, str] | None = None,
        config_root: Path | str | None = None,
    ) -> None:
        self._connector = connector
        # The out-of-band identity the self-declared agentic_allowed flag is
        # cross-checked against. Injected directly (tests, an explicit harness)
        # or loaded from config; either way it is mandatory, never skipped.
        self._expected_account = (
            dict(expected_account) if expected_account is not None else _load_expected_account(config_root)
        )
        self.account = self._resolve_agent_account()
        self.account_number = self.account["account_number"]
        self.nickname = self.account.get("nickname")

    # --- account resolution ---

    def _resolve_agent_account(self) -> dict[str, Any]:
        payload = self._connector.get_accounts()
        if isinstance(payload, dict):
            accounts = payload.get("accounts", payload.get("results", []))
        else:
            accounts = payload or []
        # Step 1: the connector's self-declared agentic_allowed flag must
        # select exactly one account. Necessary, but not sufficient.
        agentic = [account for account in accounts if account.get("agentic_allowed")]
        if len(agentic) != 1:
            raise NoAgentTradableAccountError(
                f"connector reports {len(agentic)} agentic-allowed accounts; expected exactly 1"
            )
        account = agentic[0]
        # Step 2: cross-check the flag-selected account against the out-of-band
        # expected identity. A flipped flag on the wrong account is refused here.
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
                f"refusing order for account {target!r}; the agent may only trade {self.account_number!r}"
            )
        return target

    # --- reads ---

    def get_quotes(self, *symbols: str) -> Any:
        return self._connector.get_equity_quotes(symbols=list(symbols))

    def get_positions(self) -> Any:
        return self._connector.get_equity_positions(account_number=self.account_number)

    def get_accounts(self) -> Any:
        return self._connector.get_accounts()

    def get_account(self) -> dict[str, Any]:
        """The resolved, pinned agent-tradable account (cached from init)."""
        return self.account

    def review_order(
        self,
        symbol: str,
        side: str,
        quantity: str,
        order_type: str = "limit",
        limit_price: str | None = None,
        time_in_force: str = "gtc",
        account_number: str | None = None,
    ) -> Any:
        """Ask the connector to preview an order. Robinhood's review step is
        itself non-committal, so this always reaches the connector -- the
        human gate below applies to place_order, not to a preview."""
        target = self._assert_agent_account(account_number)
        payload = self._order_payload(target, symbol, side, quantity, order_type, limit_price, time_in_force)
        return self._connector.review_equity_order(**payload)

    # --- orders ---

    @staticmethod
    def _order_payload(
        account_number: str,
        symbol: str,
        side: str,
        quantity: str,
        order_type: str,
        limit_price: str | None,
        time_in_force: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "account_number": account_number,
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": str(quantity),
            "time_in_force": time_in_force,
        }
        if limit_price is not None:
            payload["limit_price"] = str(limit_price)
        return payload

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: str,
        order_type: str = "limit",
        limit_price: str | None = None,
        time_in_force: str = "gtc",
        account_number: str | None = None,
        dry_run: bool = True,
        confirm_live_order: bool = False,
    ) -> dict[str, Any]:
        """Build an order payload; submit it only on an explicit double flag.

        READ-ONLY is the default posture. The connector's place_equity_order
        is called only when dry_run is explicitly False AND confirm_live_order
        is explicitly True -- every other combination, including either flag
        alone, returns the same unsubmitted preview. A real order is a hard
        human gate, never a default and never a single accidental flag.
        """
        target = self._assert_agent_account(account_number)
        payload = self._order_payload(target, symbol, side, quantity, order_type, limit_price, time_in_force)
        # Identity, not truthiness: submit ONLY when dry_run is exactly False AND
        # confirm_live_order is exactly True. A truthy non-boolean confirm
        # ("yes") or a falsy non-boolean dry_run (0) must NOT arm the lane --
        # `if dry_run or not confirm_live_order` would let (dry_run=0,
        # confirm="yes") slip through to a live submit; this does not.
        if not (dry_run is False and confirm_live_order is True):
            return {
                "submitted": False,
                "status": "dry_run_order_preview",
                "venue": "robinhood_equities",
                "order_payload": payload,
            }
        response = self._connector.place_equity_order(**payload)
        return {
            "submitted": True,
            "status": "submitted",
            "venue": "robinhood_equities",
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
        target = self._assert_agent_account(account_number)
        # Same identity gate as place_order: only exact booleans arm the cancel.
        if not (dry_run is False and confirm_live_order is True):
            return {"id": order_id, "account_number": target, "status": "dry_run_cancel_prepared"}
        return self._connector.cancel_equity_order(order_id=order_id, account_number=target)
