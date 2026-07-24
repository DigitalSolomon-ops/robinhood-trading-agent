from __future__ import annotations

from typing import Any

from .portfolio import Portfolio
from .robinhood_crypto_client import RobinhoodCryptoClient


class LiveBroker:
    def __init__(self, client: RobinhoodCryptoClient, dry_run: bool = False, account_number: str | None = None) -> None:
        self.client = client
        self.dry_run = dry_run
        self.account_number = account_number

    def get_account_payload(self) -> Any:
        return self.client.get_accounts()

    def get_holdings_payload(self) -> Any:
        return self.client.get_holdings(self.account_number)

    def get_portfolio(self) -> Portfolio:
        return Portfolio.from_robinhood(self.get_account_payload(), self.get_holdings_payload())

    def place_limit_order(self, order: dict[str, Any]) -> dict[str, Any]:
        order_config = {
            "asset_quantity": str(order["quantity"]),
            "limit_price": str(order["limit_price"]),
            "time_in_force": order["time_in_force"],
        }
        if order.get("notional"):
            order_config["quote_amount"] = str(order["notional"])
        order_payload = {
            "client_order_id": order["client_order_id"],
            "side": order["side"],
            "type": "limit",
            "symbol": order["symbol"],
            "limit_order_config": order_config,
        }
        if self.account_number:
            order_payload["account_number_suffix"] = self.account_number[-4:]
        if self.dry_run:
            return {
                **order,
                "submitted": False,
                "status": "dry_run_order_preview",
                "order_config": order_config,
                "order_payload": order_payload,
            }
        response = self.client.place_order(
            client_order_id=order["client_order_id"],
            side=order["side"],
            order_type="limit",
            symbol=order["symbol"],
            order_config=order_config,
            account_number=self.account_number,
        )
        return {"submitted": True, "status": "submitted", "response": response, **order}

    def cancel_order(self, order_id: str) -> Any:
        if self.dry_run:
            return {"id": order_id, "status": "dry_run_cancel_prepared"}
        return self.client.cancel_order(order_id)

    def get_order_status(self, order_id: str) -> Any:
        return self.client.get_order(order_id, self.account_number)
