from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import httpx


SYMBOL_TO_CURRENCY = {
    "BTC-USD": "BTC",
    "ETH-USD": "ETH",
    "SOL-USD": "SOL",
    "LINK-USD": "LINK",
    "AVAX-USD": "AVAX",
    "AAVE-USD": "AAVE",
    "UNI-USD": "UNI",
    "NEAR-USD": "NEAR",
    "ARB-USD": "ARB",
    "OP-USD": "OP",
    "SUI-USD": "SUI",
    "INJ-USD": "INJ",
    "SEI-USD": "SEI",
    "TAO-USD": "TAO",
    "RENDER-USD": "RENDER",
    "ONDO-USD": "ONDO",
    "GRT-USD": "GRT",
    "TIA-USD": "TIA",
    "KAS-USD": "KAS",
    "HYPE-USD": "HYPE",
}


class CryptoPanicNewsClient:
    def __init__(self, api_key: str | None = None, base_url: str = "https://cryptopanic.com/api/v1/posts/") -> None:
        self.api_key = api_key if api_key is not None else os.getenv("CRYPTOPANIC_API_KEY", "")
        self.base_url = base_url

    @property
    def status(self) -> str:
        return "enabled" if self.api_key else "disabled_missing_api_key"

    def fetch(self, symbols: list[str]) -> dict[str, Any]:
        if not self.api_key:
            return {"provider": "cryptopanic", "status": self.status, "events": [], "submitted": False}
        currencies = ",".join(SYMBOL_TO_CURRENCY.get(symbol, symbol.split("-")[0]) for symbol in symbols)
        params = {"auth_token": self.api_key, "public": "true", "currencies": currencies}
        with httpx.Client(timeout=15) as client:
            response = client.get(self.base_url, params=params)
            response.raise_for_status()
            payload = response.json()
        return {"provider": "cryptopanic", "status": "ok", "events": self._events_from_payload(payload, symbols), "submitted": False}

    def _events_from_payload(self, payload: dict[str, Any], symbols: list[str]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        results = payload.get("results", []) if isinstance(payload, dict) else []
        for item in results:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", ""))
            votes = item.get("votes", {}) if isinstance(item.get("votes"), dict) else {}
            sentiment_value = float(votes.get("positive", 0) or 0) - float(votes.get("negative", 0) or 0)
            if item.get("kind") == "bearish":
                sentiment_value -= 1
            if item.get("kind") == "bullish":
                sentiment_value += 1
            sentiment = "positive" if sentiment_value > 0 else "negative" if sentiment_value < 0 else "neutral"
            currencies = item.get("currencies", [])
            matched: set[str] = set()
            if isinstance(currencies, list):
                codes = {str(currency.get("code", "")).upper() for currency in currencies if isinstance(currency, dict)}
                matched = {symbol for symbol in symbols if SYMBOL_TO_CURRENCY.get(symbol, symbol.split("-")[0]) in codes}
            if not matched:
                upper_title = title.upper()
                matched = {symbol for symbol in symbols if SYMBOL_TO_CURRENCY.get(symbol, symbol.split("-")[0]) in upper_title}
            if not matched:
                matched = set(symbols)
            for symbol in sorted(matched):
                events.append(
                    {
                        "timestamp": item.get("published_at") or datetime.now(UTC).isoformat(),
                        "symbol": symbol,
                        "provider": "cryptopanic",
                        "title": title,
                        "url": item.get("url"),
                        "sentiment": sentiment,
                        "sentiment_value": sentiment_value,
                        "important": bool(item.get("important")),
                        "details": {"source": "cryptopanic", "kind": item.get("kind")},
                    }
                )
        return events
