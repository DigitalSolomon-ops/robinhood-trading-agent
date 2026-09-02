"""Minimal read-only Finnhub client for the two FREE endpoints the scout uses:
the earnings calendar and company news.

ANALYSIS ONLY. No order path. Every call is graceful: with no API key the client
is `enabled == False` and returns empty results, and any network/HTTP error
returns empty rather than raising -- enrichment must never break a scout email.
The free tier is ~60 req/min; an optional `min_interval` spaces requests, and a
429 backs off rather than failing hard.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable

import httpx

DEFAULT_BASE_URL = "https://finnhub.io/api/v1"
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 8.0


class FinnhubClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = MAX_RETRIES,
        min_interval: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api_key = (api_key or os.getenv("FINNHUB_API_KEY") or "").strip()
        self.base_url = (base_url or os.getenv("FINNHUB_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._transport = transport
        self._sleep = sleep
        self._max_retries = max_retries
        self._min_interval = min_interval
        self._clock = clock
        self._last_request_at: float | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        if self._last_request_at is not None:
            elapsed = self._clock() - self._last_request_at
            if elapsed < self._min_interval:
                self._sleep(self._min_interval - elapsed)
        self._last_request_at = self._clock()

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        """GET a Finnhub endpoint, returning parsed JSON or None. Never raises."""
        if not self.api_key:
            return None
        query = {k: v for k, v in params.items() if v is not None}
        query["token"] = self.api_key
        self._throttle()
        delay = INITIAL_BACKOFF_SECONDS
        try:
            with httpx.Client(
                base_url=self.base_url, transport=self._transport, timeout=15
            ) as client:
                for attempt in range(self._max_retries + 1):
                    response = client.get(path, params=query)
                    if response.status_code == 429 and attempt < self._max_retries:
                        retry_after = response.headers.get("Retry-After")
                        try:
                            wait = float(retry_after) if retry_after else delay
                        except ValueError:
                            wait = delay
                        self._sleep(min(wait, MAX_BACKOFF_SECONDS))
                        delay = min(delay * 2, MAX_BACKOFF_SECONDS)
                        continue
                    if response.status_code != 200:
                        return None
                    return response.json()
        except (httpx.HTTPError, ValueError):
            return None
        return None

    def get_earnings_calendar(
        self, from_date: str, to_date: str, symbol: str | None = None
    ) -> list[dict[str, Any]]:
        """Earnings events in [from_date, to_date]. Omit `symbol` to fetch the
        whole window in one call (then filter locally). Returns []."""
        data = self._get("/calendar/earnings", {"from": from_date, "to": to_date, "symbol": symbol})
        if isinstance(data, dict):
            events = data.get("earningsCalendar")
            if isinstance(events, list):
                return [e for e in events if isinstance(e, dict)]
        return []

    def get_company_news(self, symbol: str, from_date: str, to_date: str) -> list[dict[str, Any]]:
        """Company news items for `symbol` in [from_date, to_date], newest first
        as Finnhub returns them. Returns []."""
        data = self._get("/company-news", {"symbol": symbol, "from": from_date, "to": to_date})
        if isinstance(data, list):
            return [n for n in data if isinstance(n, dict)]
        return []
