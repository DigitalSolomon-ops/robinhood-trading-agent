from __future__ import annotations

import os
from typing import Any

import httpx


COINGECKO_IDS = {
    "BTC-USD": "bitcoin",
    "ETH-USD": "ethereum",
    "SOL-USD": "solana",
    "LINK-USD": "chainlink",
    "AVAX-USD": "avalanche-2",
    "AAVE-USD": "aave",
    "UNI-USD": "uniswap",
    "NEAR-USD": "near",
    "ARB-USD": "arbitrum",
    "OP-USD": "optimism",
    "SUI-USD": "sui",
    "INJ-USD": "injective-protocol",
    "SEI-USD": "sei-network",
    "TAO-USD": "bittensor",
    "RENDER-USD": "render-token",
    "ONDO-USD": "ondo-finance",
    "GRT-USD": "the-graph",
    "TIA-USD": "celestia",
    "KAS-USD": "kaspa",
    "HYPE-USD": "hyperliquid",
}


class CoinGeckoMarketClient:
    def __init__(self, api_key: str | None = None, base_url: str = "https://api.coingecko.com/api/v3") -> None:
        self.api_key = api_key if api_key is not None else os.getenv("COINGECKO_API_KEY", "")
        self.base_url = base_url.rstrip("/")

    @property
    def status(self) -> str:
        return "enabled" if self.api_key else "disabled_missing_api_key"

    def _headers(self) -> dict[str, str]:
        return {"x-cg-demo-api-key": self.api_key} if self.api_key else {}

    def fetch(self, symbols: list[str]) -> dict[str, Any]:
        if not self.api_key:
            return {"provider": "coingecko", "status": self.status, "global": {}, "coins": [], "submitted": False}
        ids = ",".join(COINGECKO_IDS.get(symbol, symbol.split("-")[0].lower()) for symbol in symbols)
        with httpx.Client(timeout=20, headers=self._headers()) as client:
            global_payload = client.get(f"{self.base_url}/global")
            global_payload.raise_for_status()
            coin_payload = client.get(
                f"{self.base_url}/coins/markets",
                params={
                    "vs_currency": "usd",
                    "ids": ids,
                    "price_change_percentage": "24h,7d,30d",
                    "per_page": "250",
                },
            )
            coin_payload.raise_for_status()
        return {
            "provider": "coingecko",
            "status": "ok",
            "global": global_payload.json(),
            "coins": coin_payload.json(),
            "submitted": False,
        }
