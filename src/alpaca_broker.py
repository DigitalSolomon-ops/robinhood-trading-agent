from __future__ import annotations

from typing import Any

from .alpaca_client import AlpacaClient
from .portfolio import Portfolio


class AlpacaBroker:
    """Equities and single-leg options broker.

    Mirrors LiveBroker so the same OrderManager, RiskManager, and kill switch
    govern this lane. dry_run builds the payload without submitting it.
    """

    def __init__(self, client: AlpacaClient, dry_run: bool = False) -> None:
        self.client = client
        self.dry_run = dry_run

    @property
    def paper(self) -> bool:
        return self.client.paper

    def get_account_payload(self) -> Any:
        return self.client.get_account()

    def get_positions_payload(self) -> Any:
        return self.client.get_positions()

    def get_portfolio(self) -> Portfolio:
        return Portfolio.from_alpaca(self.get_account_payload(), self.get_positions_payload())

    @staticmethod
    def _time_in_force(value: str | None) -> str:
        """Map config TIF to Alpaca's vocabulary. Alpaca uses 'day', not 'gfd'."""
        mapping = {"gtc": "gtc", "gfd": "day", "day": "day", "ioc": "ioc", "fok": "fok"}
        return mapping.get(str(value or "day").lower(), "day")

    def place_limit_order(self, order: dict[str, Any]) -> dict[str, Any]:
        """Place an equity limit order. `order` is the OrderManager payload."""
        order_payload = {
            "symbol": order["symbol"],
            "side": order["side"],
            "type": "limit",
            "qty": str(order["quantity"]),
            "limit_price": str(order["limit_price"]),
            "time_in_force": self._time_in_force(order.get("time_in_force")),
            "client_order_id": order.get("client_order_id"),
        }
        if self.dry_run:
            return {
                **order,
                "submitted": False,
                "status": "dry_run_order_preview",
                "venue": "alpaca",
                "paper": self.paper,
                "order_payload": order_payload,
            }
        response = self.client.place_order(
            symbol=order["symbol"],
            side=order["side"],
            order_type="limit",
            quantity=str(order["quantity"]),
            limit_price=str(order["limit_price"]),
            time_in_force=self._time_in_force(order.get("time_in_force")),
            client_order_id=order.get("client_order_id"),
        )
        return {
            "submitted": True,
            "status": "submitted",
            "venue": "alpaca",
            "paper": self.paper,
            "response": response,
            **order,
        }

    def place_option_limit_order(self, order: dict[str, Any]) -> dict[str, Any]:
        """Place a single-leg option limit order.

        `order["symbol"]` must be an OCC option symbol (e.g. AAPL250117C00150000)
        and `order["quantity"]` is a contract count. Multi-leg spreads are not
        supported here, matching the single-leg constraint of the crypto lane's
        one-position-at-a-time model.
        """
        quantity = int(float(order["quantity"]))
        if quantity < 1:
            raise ValueError("option orders require a whole contract count of at least 1")
        order_payload = {
            "symbol": order["symbol"],
            "side": order["side"],
            "type": "limit",
            "qty": str(quantity),
            "limit_price": str(order["limit_price"]),
            # Alpaca options are day-only; gtc is rejected on single-leg options.
            "time_in_force": "day",
            "client_order_id": order.get("client_order_id"),
        }
        if self.dry_run:
            return {
                **order,
                "submitted": False,
                "status": "dry_run_order_preview",
                "venue": "alpaca_options",
                "paper": self.paper,
                "contracts": quantity,
                "order_payload": order_payload,
            }
        response = self.client.place_order(
            symbol=order["symbol"],
            side=order["side"],
            order_type="limit",
            quantity=str(quantity),
            limit_price=str(order["limit_price"]),
            time_in_force="day",
            client_order_id=order.get("client_order_id"),
        )
        return {
            "submitted": True,
            "status": "submitted",
            "venue": "alpaca_options",
            "paper": self.paper,
            "contracts": quantity,
            "response": response,
            **order,
        }

    def find_option_contract(
        self,
        underlying: str,
        expiration_date: str,
        option_type: str,
        strike_price: float | None = None,
    ) -> dict[str, Any] | None:
        """Resolve an OCC option symbol from human-readable terms."""
        params: dict[str, Any] = {
            "underlying_symbol": underlying,
            "expiration_date": expiration_date,
            "option_type": option_type,
        }
        if strike_price is not None:
            params["strike_price_gte"] = str(strike_price)
            params["strike_price_lte"] = str(strike_price)
        payload = self.client.get_option_contracts(**params)
        contracts = payload.get("option_contracts", []) if isinstance(payload, dict) else []
        return contracts[0] if contracts else None

    def cancel_order(self, order_id: str) -> Any:
        if self.dry_run:
            return {"id": order_id, "status": "dry_run_cancel_prepared"}
        return self.client.cancel_order(order_id)

    def get_order_status(self, order_id: str) -> Any:
        return self.client.get_order(order_id)
