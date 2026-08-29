from __future__ import annotations

from typing import Any, Protocol


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
    """The connector's account list has zero or more than one agent-tradable account."""


class AgentAccountMismatchError(RuntimeError):
    """An order path targeted an account other than the pinned agent-tradable one."""


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

    def __init__(self, connector: EquityConnector) -> None:
        self._connector = connector
        self.account = self._resolve_agent_account()
        self.account_number = self.account["account_number"]

    # --- account resolution ---

    def _resolve_agent_account(self) -> dict[str, Any]:
        payload = self._connector.get_accounts()
        if isinstance(payload, dict):
            accounts = payload.get("accounts", payload.get("results", []))
        else:
            accounts = payload or []
        agent_tradable = [account for account in accounts if account.get("agent_tradable")]
        if len(agent_tradable) != 1:
            raise NoAgentTradableAccountError(
                f"connector reports {len(agent_tradable)} agent-tradable accounts; expected exactly 1"
            )
        return agent_tradable[0]

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
        if dry_run or not confirm_live_order:
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
        if dry_run or not confirm_live_order:
            return {"id": order_id, "account_number": target, "status": "dry_run_cancel_prepared"}
        return self._connector.cancel_equity_order(order_id=order_id, account_number=target)
