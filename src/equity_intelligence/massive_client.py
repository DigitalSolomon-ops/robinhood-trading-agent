from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable

import httpx

from ..market_data_fields import (
    FIELD_CLOSE,
    FIELD_HIGH,
    FIELD_LOW,
    FIELD_OPEN,
    FIELD_TICKER,
    FIELD_TIMESTAMP,
    FIELD_TRANSACTIONS,
    FIELD_VOLUME,
    FIELD_VWAP,
)

# Massive (formerly Polygon.io). api.polygon.io is the same API and still
# works; MASSIVE_BASE_URL overrides either for a self-hosted/mirrored gateway.
DEFAULT_BASE_URL = "https://api.massive.com"
SECRET_MANAGER_NAME = "massive-api"

MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0

# EOD/delayed data feeds decisions and proving only -- the Robinhood connector
# stays the sole source of execution-time price. Nothing here is a quote used
# to size or time an order.


class MassiveAuthError(RuntimeError):
    """Raised when a request is attempted with no resolved API key."""


class MassiveRateLimitError(RuntimeError):
    """Raised when the API still returns 429 after every backoff retry."""


def _secret_from_manager(name: str) -> str | None:
    """SM fallback half of the house `secret()` pattern (secret-manager-credentials
    skill / TBFC config.py): env-first is the caller's job, this only covers the
    Secret Manager read via the gcloud CLI the operator already authenticates
    daily. Disabled on Cloud Run/CI via DS_VAULT_NO_GCLOUD=1, where ADC/the SDK
    is the only path and gcloud is not installed. Never raises and never logs
    the value -- an absent/misconfigured secret just leaves the client
    disabled, same as a missing env var.
    """
    if os.getenv("DS_VAULT_NO_GCLOUD"):
        return None
    try:
        result = subprocess.run(
            ["gcloud", "secrets", "versions", "access", "latest", f"--secret={name}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def resolve_massive_api_key(explicit: str | None = None) -> str:
    """Env-first, then Secret Manager secret `massive-api`. Never logs the value."""
    if explicit:
        return explicit
    value = os.getenv("MASSIVE_API_KEY", "")
    if value:
        return value
    fetched = _secret_from_manager(SECRET_MANAGER_NAME)
    if fetched:
        # Cache into the process env so a second client in the same run does not
        # re-shell out to gcloud for every read.
        os.environ.setdefault("MASSIVE_API_KEY", fetched)
        return fetched
    return ""


@dataclass(frozen=True)
class IndicatorPoint:
    """One SMA/EMA/RSI value at a timestamp."""

    timestamp_ms: int
    value: float


@dataclass(frozen=True)
class MACDPoint:
    """One MACD value/signal/histogram triple at a timestamp."""

    timestamp_ms: int
    value: float
    signal: float
    histogram: float


@dataclass(frozen=True)
class Bar:
    """One OHLCV aggregate (daily bar, prev-close bar, or grouped-daily row)."""

    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None
    transactions: int | None = None
    ticker: str | None = None  # populated on grouped-daily rows, absent otherwise


@dataclass(frozen=True)
class OptionContract:
    """One row from the options-contracts REFERENCE endpoint.

    This is reference metadata only -- ticker, strike, expiry, type. The free
    tier does NOT authorize option quotes, greeks, or IV, so nothing here is a
    price. The scout maps the level math it does on the UNDERLYING onto whichever
    of these contracts sits nearest the target; it never prices the contract.
    """

    ticker: str  # e.g. O:AAPL260831C00205000
    underlying_ticker: str
    contract_type: str  # "call" | "put"
    strike_price: float
    expiration_date: str  # YYYY-MM-DD


@dataclass(frozen=True)
class NewsInsight:
    """Per-ticker sentiment attached to a news item."""

    ticker: str
    sentiment: str | None
    sentiment_reasoning: str | None


@dataclass(frozen=True)
class NewsItem:
    id: str
    title: str
    published_utc: str | None
    article_url: str | None
    tickers: tuple[str, ...]
    insights: tuple[NewsInsight, ...]

    def sentiment_for(self, ticker: str) -> str | None:
        wanted = ticker.upper()
        for insight in self.insights:
            if insight.ticker.upper() == wanted:
                return insight.sentiment
        return None


def _indicator_values(payload: dict[str, Any]) -> list[Any]:
    results = payload.get("results", {}) if isinstance(payload, dict) else {}
    if isinstance(results, dict):
        return results.get("values", []) or []
    return []


def _bar_from_row(row: dict[str, Any]) -> Bar:
    return Bar(
        timestamp_ms=int(row.get(FIELD_TIMESTAMP, 0) or 0),
        open=float(row.get(FIELD_OPEN, 0.0) or 0.0),
        high=float(row.get(FIELD_HIGH, 0.0) or 0.0),
        low=float(row.get(FIELD_LOW, 0.0) or 0.0),
        close=float(row.get(FIELD_CLOSE, 0.0) or 0.0),
        volume=float(row.get(FIELD_VOLUME, 0.0) or 0.0),
        vwap=float(row[FIELD_VWAP]) if row.get(FIELD_VWAP) is not None else None,
        transactions=int(row[FIELD_TRANSACTIONS]) if row.get(FIELD_TRANSACTIONS) is not None else None,
        ticker=row.get(FIELD_TICKER),
    )


class MassiveClient:
    """Read-only client for Massive (formerly Polygon.io).

    Covers exactly the four uses the equities lane needs: indicators
    (SMA/EMA/RSI/MACD), daily/range bars + previous close, grouped-daily
    breadth, and ticker news+sentiment. Every response is cached per
    (endpoint, args, UTC day) since this is EOD/delayed data that cannot
    change again the same day, and a 429 triggers capped exponential backoff
    (Retry-After honored when present) rather than a hard failure -- the free
    tier is roughly 5 req/min.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self.api_key = resolve_massive_api_key(api_key)
        self.base_url = (base_url or os.getenv("MASSIVE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._transport = transport
        self._sleep = sleep
        self._max_retries = max_retries
        self._cache: dict[tuple[str, tuple[tuple[str, str], ...], str], dict[str, Any]] = {}

    @property
    def status(self) -> str:
        return "enabled" if self.api_key else "disabled_missing_api_key"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    @staticmethod
    def _cache_key(path: str, params: dict[str, Any]) -> tuple[str, tuple[tuple[str, str], ...], str]:
        normalized = tuple(sorted((str(k), str(v)) for k, v in params.items()))
        today = datetime.now(UTC).date().isoformat()
        return (path, normalized, today)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.api_key:
            raise MassiveAuthError(
                "MASSIVE_API_KEY is not set (checked env, then Secret Manager `massive-api`)"
            )
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        cache_key = self._cache_key(path, clean_params)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        payload = self._request_with_backoff(path, clean_params)
        self._cache[cache_key] = payload
        return payload

    def _request_with_backoff(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        delay = INITIAL_BACKOFF_SECONDS
        with httpx.Client(
            base_url=self.base_url, transport=self._transport, timeout=20, headers=self._headers()
        ) as client:
            for attempt in range(self._max_retries + 1):
                response = client.get(path, params=params)
                if response.status_code != 429:
                    response.raise_for_status()
                    return response.json()
                if attempt == self._max_retries:
                    break
                retry_after = response.headers.get("Retry-After")
                try:
                    wait_seconds = float(retry_after) if retry_after else delay
                except ValueError:
                    wait_seconds = delay
                self._sleep(min(wait_seconds, MAX_BACKOFF_SECONDS))
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)
        raise MassiveRateLimitError(f"Massive API rate limit exceeded for {path} after {self._max_retries} retries")

    # --- indicators (use B) --------------------------------------------------

    def _indicator(self, kind: str, ticker: str, **params: Any) -> dict[str, Any]:
        defaults = {"timespan": "day", "series_type": "close", "order": "desc", "limit": 50, "adjusted": "true"}
        query = {**defaults, **params}
        return self._get(f"/v1/indicators/{kind}/{ticker}", query)

    def get_sma(self, ticker: str, window: int = 50, **params: Any) -> list[IndicatorPoint]:
        payload = self._indicator("sma", ticker, window=window, **params)
        return self._parse_indicator_points(payload)

    def get_ema(self, ticker: str, window: int = 50, **params: Any) -> list[IndicatorPoint]:
        payload = self._indicator("ema", ticker, window=window, **params)
        return self._parse_indicator_points(payload)

    def get_rsi(self, ticker: str, window: int = 14, **params: Any) -> list[IndicatorPoint]:
        payload = self._indicator("rsi", ticker, window=window, **params)
        return self._parse_indicator_points(payload)

    def get_macd(
        self,
        ticker: str,
        short_window: int = 12,
        long_window: int = 26,
        signal_window: int = 9,
        **params: Any,
    ) -> list[MACDPoint]:
        payload = self._indicator(
            "macd",
            ticker,
            short_window=short_window,
            long_window=long_window,
            signal_window=signal_window,
            **params,
        )
        points = []
        for row in _indicator_values(payload):
            if not isinstance(row, dict):
                continue
            points.append(
                MACDPoint(
                    timestamp_ms=int(row.get("timestamp", 0) or 0),
                    value=float(row.get("value", 0.0) or 0.0),
                    signal=float(row.get("signal", 0.0) or 0.0),
                    histogram=float(row.get("histogram", 0.0) or 0.0),
                )
            )
        return points

    @staticmethod
    def _parse_indicator_points(payload: dict[str, Any]) -> list[IndicatorPoint]:
        points = []
        for row in _indicator_values(payload):
            if not isinstance(row, dict):
                continue
            points.append(
                IndicatorPoint(timestamp_ms=int(row.get("timestamp", 0) or 0), value=float(row.get("value", 0.0) or 0.0))
            )
        return points

    # --- bars: daily aggregates + historical range + prev (use A) ------------

    def get_aggs_range(
        self,
        ticker: str,
        multiplier: int,
        timespan: str,
        from_date: str,
        to_date: str,
        adjusted: bool = True,
        sort: str = "asc",
        limit: int = 5000,
    ) -> list[Bar]:
        path = f"/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from_date}/{to_date}"
        payload = self._get(path, {"adjusted": str(adjusted).lower(), "sort": sort, "limit": limit})
        return [_bar_from_row(row) for row in payload.get("results", []) or [] if isinstance(row, dict)]

    def get_daily_bars(self, ticker: str, from_date: str, to_date: str, adjusted: bool = True) -> list[Bar]:
        return self.get_aggs_range(ticker, 1, "day", from_date, to_date, adjusted=adjusted)

    def get_previous_close(self, ticker: str, adjusted: bool = True) -> Bar | None:
        payload = self._get(f"/v2/aggs/ticker/{ticker}/prev", {"adjusted": str(adjusted).lower()})
        rows = [row for row in payload.get("results", []) or [] if isinstance(row, dict)]
        return _bar_from_row(rows[0]) if rows else None

    # --- breadth: grouped-daily (use D) ---------------------------------------

    def get_grouped_daily(
        self, date_str: str, adjusted: bool = True, locale: str = "us", market: str = "stocks"
    ) -> list[Bar]:
        path = f"/v2/aggs/grouped/locale/{locale}/market/{market}/{date_str}"
        payload = self._get(path, {"adjusted": str(adjusted).lower()})
        return [_bar_from_row(row) for row in payload.get("results", []) or [] if isinstance(row, dict)]

    # --- news + sentiment (use C) ---------------------------------------------

    def get_ticker_news(self, ticker: str, limit: int = 10, **params: Any) -> list[NewsItem]:
        query = {"ticker": ticker, "limit": limit, **params}
        payload = self._get("/v2/reference/news", query)
        items = []
        for row in payload.get("results", []) or []:
            if not isinstance(row, dict):
                continue
            insights = tuple(
                NewsInsight(
                    ticker=str(insight.get("ticker", "")),
                    sentiment=insight.get("sentiment"),
                    sentiment_reasoning=insight.get("sentiment_reasoning"),
                )
                for insight in row.get("insights", []) or []
                if isinstance(insight, dict)
            )
            items.append(
                NewsItem(
                    id=str(row.get("id", "")),
                    title=str(row.get("title", "")),
                    published_utc=row.get("published_utc"),
                    article_url=row.get("article_url"),
                    tickers=tuple(row.get("tickers", []) or []),
                    insights=insights,
                )
            )
        return items

    # --- options contracts REFERENCE (metadata only, no price/greeks/IV) ------

    def get_option_contracts(
        self,
        underlying: str,
        contract_type: str | None = None,
        expiration_gte: str | None = None,
        expiration_lte: str | None = None,
        strike_gte: float | None = None,
        strike_lte: float | None = None,
        limit: int = 250,
    ) -> list[OptionContract]:
        """List listed option contracts for an underlying from the v3 reference
        endpoint (GET /v3/reference/options/contracts).

        Reference metadata ONLY -- returns ticker (O:...), strike_price,
        expiration_date and contract_type. This endpoint IS authorized on the
        free tier; option quotes/greeks/IV are NOT, so this client never fetches
        those. Cached and rate-limit-aware like every other read here.
        """
        params: dict[str, Any] = {
            "underlying_ticker": underlying,
            "contract_type": contract_type,
            "expiration_date.gte": expiration_gte,
            "expiration_date.lte": expiration_lte,
            "strike_price.gte": strike_gte,
            "strike_price.lte": strike_lte,
            "limit": limit,
        }
        payload = self._get("/v3/reference/options/contracts", params)
        contracts: list[OptionContract] = []
        for row in payload.get("results", []) or []:
            if not isinstance(row, dict):
                continue
            strike = row.get("strike_price")
            if strike is None:
                continue
            contracts.append(
                OptionContract(
                    ticker=str(row.get("ticker", "")),
                    underlying_ticker=str(row.get("underlying_ticker", underlying)),
                    contract_type=str(row.get("contract_type", "")),
                    strike_price=float(strike),
                    expiration_date=str(row.get("expiration_date", "")),
                )
            )
        return contracts
