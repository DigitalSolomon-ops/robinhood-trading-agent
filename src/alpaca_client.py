from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL = "https://api.alpaca.markets"
DATA_BASE_URL = "https://data.alpaca.markets"


class AlpacaClient:
    """Alpaca Trading API client for equities and single-leg options.

    Defaults to the paper endpoint. Live trading requires paper=False, which
    the broker layer only passes once the same risk gates that govern the
    Robinhood crypto lane have approved the order.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        paper: bool = True,
        timeout: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.paper = paper
        self.base_url = PAPER_BASE_URL if paper else LIVE_BASE_URL
        self._client = httpx.Client(timeout=timeout)

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def headers(self) -> dict[str, str]:
        if not self.has_credentials:
            raise ValueError("Alpaca API credentials are missing")
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
            "accept": "application/json",
        }

    @staticmethod
    def _query(params: dict[str, Any] | None = None) -> str:
        if not params:
            return ""
        pairs = [(key, value) for key, value in params.items() if value is not None]
        return f"?{urlencode(pairs)}" if pairs else ""

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        base_url: str | None = None,
    ) -> Any:
        url = f"{base_url or self.base_url}{path}{self._query(params)}"
        response = self._client.request(method.upper(), url, headers=self.headers(), json=payload)
        response.raise_for_status()
        return response.json() if response.content else {}

    # --- account and positions ---

    def get_account(self) -> Any:
        return self.request("GET", "/v2/account")

    def get_positions(self) -> Any:
        return self.request("GET", "/v2/positions")

    def get_position(self, symbol: str) -> Any:
        return self.request("GET", f"/v2/positions/{symbol}")

    # --- orders ---

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str = "limit",
        quantity: str | None = None,
        notional: str | None = None,
        limit_price: str | None = None,
        stop_price: str | None = None,
        time_in_force: str = "day",
        client_order_id: str | None = None,
        extended_hours: bool = False,
    ) -> Any:
        body: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
        }
        if quantity is not None:
            body["qty"] = str(quantity)
        if notional is not None:
            body["notional"] = str(notional)
        if limit_price is not None:
            body["limit_price"] = str(limit_price)
        if stop_price is not None:
            body["stop_price"] = str(stop_price)
        if client_order_id:
            body["client_order_id"] = client_order_id
        if extended_hours:
            body["extended_hours"] = True
        return self.request("POST", "/v2/orders", payload=body)

    def cancel_order(self, order_id: str) -> Any:
        return self.request("DELETE", f"/v2/orders/{order_id}")

    def get_order(self, order_id: str) -> Any:
        return self.request("GET", f"/v2/orders/{order_id}")

    def get_orders(self, status: str = "open") -> Any:
        return self.request("GET", "/v2/orders", params={"status": status})

    # --- options ---

    def get_option_contracts(
        self,
        underlying_symbol: str,
        expiration_date: str | None = None,
        strike_price_gte: str | None = None,
        strike_price_lte: str | None = None,
        option_type: str | None = None,
        limit: int = 100,
    ) -> Any:
        """Find tradable option contracts. option_type is 'call' or 'put'."""
        return self.request(
            "GET",
            "/v2/options/contracts",
            params={
                "underlying_symbols": underlying_symbol,
                "expiration_date": expiration_date,
                "strike_price_gte": strike_price_gte,
                "strike_price_lte": strike_price_lte,
                "type": option_type,
                "limit": limit,
                "status": "active",
            },
        )

    # --- market data ---

    def get_latest_quote(self, symbol: str) -> Any:
        return self.request(
            "GET",
            f"/v2/stocks/{symbol}/quotes/latest",
            base_url=DATA_BASE_URL,
        )

    def get_latest_option_quote(self, option_symbol: str) -> Any:
        return self.request(
            "GET",
            "/v1beta1/options/quotes/latest",
            params={"symbols": option_symbol},
            base_url=DATA_BASE_URL,
        )
