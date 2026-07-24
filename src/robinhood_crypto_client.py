from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

import httpx

from .auth import RobinhoodAuth


class RobinhoodCryptoClient:
    """Official Crypto Trading API client using signed API-key authentication."""

    def __init__(
        self,
        api_key: str,
        private_key_base64: str,
        base_url: str = "https://trading.robinhood.com",
        api_version: str = "v2",
        timeout: float = 10.0,
    ) -> None:
        if api_version not in {"v1", "v2"}:
            raise ValueError("api_version must be v1 or v2")
        self.base_url = base_url.rstrip("/")
        self.api_version = api_version
        self.auth = RobinhoodAuth(api_key=api_key, private_key_base64=private_key_base64)
        self._client = httpx.Client(timeout=timeout)

    @property
    def has_credentials(self) -> bool:
        return self.auth.has_credentials()

    @staticmethod
    def _query(params: dict[str, Any] | None = None) -> str:
        if not params:
            return ""
        pairs: list[tuple[str, Any]] = []
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                pairs.extend((key, item) for item in value)
            else:
                pairs.append((key, value))
        return f"?{urlencode(pairs)}" if pairs else ""

    @staticmethod
    def _json_body(payload: dict[str, Any] | None) -> str:
        return json.dumps(payload or {}, separators=(",", ":")) if payload else ""

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        method = method.upper()
        body = self._json_body(payload)
        headers = dict(self.auth.headers(method, path, body))
        if body:
            headers["Content-Type"] = "application/json"
        response = self._client.request(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            content=body if body else None,
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    def _trading_path(self, resource: str, params: dict[str, Any] | None = None) -> str:
        return f"/api/{self.api_version}/crypto/trading/{resource}/{self._query(params)}"

    def _marketdata_path(self, resource: str, params: dict[str, Any] | None = None) -> str:
        base = "trading/estimated_price" if self.api_version == "v2" and resource == "estimated_price" else f"marketdata/{resource}"
        return f"/api/{self.api_version}/crypto/{base}/{self._query(params)}"

    def get_accounts(self) -> Any:
        resource = "accounts"
        return self.request("GET", self._trading_path(resource))

    def get_account(self) -> Any:
        return self.get_accounts()

    def get_trading_pairs(self, *symbols: str) -> Any:
        return self.request("GET", self._trading_path("trading_pairs", {"symbol": list(symbols) if symbols else None}))

    def get_holdings(self, account_number: str | None = None, *asset_codes: str) -> Any:
        params: dict[str, Any] = {"asset_code": list(asset_codes) if asset_codes else None}
        if self.api_version == "v2":
            params["account_number"] = account_number
        return self.request("GET", self._trading_path("holdings", params))

    def get_best_bid_ask(self, *symbols: str) -> Any:
        return self.request("GET", self._marketdata_path("best_bid_ask", {"symbol": list(symbols) if symbols else None}))

    def get_estimated_price(self, symbol: str, side: str, quantity: str) -> Any:
        return self.request("GET", self._marketdata_path("estimated_price", {"symbol": symbol, "side": side, "quantity": quantity}))

    def place_order(
        self,
        client_order_id: str,
        side: str,
        order_type: str,
        symbol: str,
        order_config: dict[str, str],
        account_number: str | None = None,
    ) -> Any:
        body = {
            "client_order_id": client_order_id,
            "side": side,
            "type": order_type,
            "symbol": symbol,
            f"{order_type}_order_config": order_config,
        }
        params = {"account_number": account_number} if self.api_version == "v2" else None
        return self.request("POST", self._trading_path("orders", params), body)

    def cancel_order(self, order_id: str) -> Any:
        return self.request("POST", self._trading_path(f"orders/{order_id}/cancel"))

    def get_order(self, order_id: str, account_number: str | None = None) -> Any:
        params = {"account_number": account_number} if self.api_version == "v2" else None
        return self.request("GET", self._trading_path(f"orders/{order_id}", params))

    def get_orders(self, account_number: str | None = None) -> Any:
        params = {"account_number": account_number} if self.api_version == "v2" else None
        return self.request("GET", self._trading_path("orders", params))
