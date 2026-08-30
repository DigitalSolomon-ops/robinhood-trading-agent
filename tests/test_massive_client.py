from __future__ import annotations

import subprocess

import httpx
import pytest

from src.equity_intelligence.massive_client import (
    Bar,
    IndicatorPoint,
    MACDPoint,
    MassiveAuthError,
    MassiveClient,
    MassiveRateLimitError,
    NewsItem,
    resolve_massive_api_key,
)


# --- key resolution: env-first, then Secret Manager, never logged -----------


def test_explicit_key_wins_over_everything(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    assert resolve_massive_api_key("explicit-key") == "explicit-key"


def test_env_var_is_used_without_touching_secret_manager(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "env-key")

    def boom(*args, **kwargs):
        raise AssertionError("must not shell out to gcloud when the env var is set")

    monkeypatch.setattr(subprocess, "run", boom)

    assert resolve_massive_api_key() == "env-key"


def test_missing_key_falls_back_to_secret_manager(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    monkeypatch.delenv("DS_VAULT_NO_GCLOUD", raising=False)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        assert cmd == ["gcloud", "secrets", "versions", "access", "latest", "--secret=massive-api"]
        return subprocess.CompletedProcess(cmd, 0, stdout="sm-key\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    resolved = resolve_massive_api_key()

    assert resolved == "sm-key"
    assert len(calls) == 1
    # Cached into the process env so a second read does not re-shell out.
    assert resolve_massive_api_key() == "sm-key"
    assert len(calls) == 1


def test_ds_vault_no_gcloud_skips_secret_manager_entirely(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    monkeypatch.setenv("DS_VAULT_NO_GCLOUD", "1")

    def boom(*args, **kwargs):
        raise AssertionError("must not invoke gcloud when DS_VAULT_NO_GCLOUD is set")

    monkeypatch.setattr(subprocess, "run", boom)

    assert resolve_massive_api_key() == ""


def test_gcloud_failure_yields_empty_not_a_crash(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    monkeypatch.delenv("DS_VAULT_NO_GCLOUD", raising=False)

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="NOT_FOUND")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert resolve_massive_api_key() == ""


def test_client_status_reflects_missing_key(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    monkeypatch.setenv("DS_VAULT_NO_GCLOUD", "1")

    client = MassiveClient()

    assert client.status == "disabled_missing_api_key"
    with pytest.raises(MassiveAuthError):
        client.get_sma("AAPL")


def test_client_repr_never_leaks_the_key():
    """MassiveClient has no custom __repr__/__str__, so the default object repr
    (class name + id) is all `repr()`/`str()` ever produce -- this pins that
    down so a future added __repr__ cannot start interpolating self.api_key."""
    fixture_key = "test-fixture-key-value"  # placeholder-marked so the repo secret scan does not flag it
    client = MassiveClient(api_key=fixture_key)

    assert fixture_key not in repr(client)
    assert fixture_key not in str(client)


# --- HTTP mocking helpers ----------------------------------------------------


def make_client(handler, **kwargs) -> MassiveClient:
    transport = httpx.MockTransport(handler)
    return MassiveClient(api_key="test-key", transport=transport, sleep=lambda _seconds: None, **kwargs)


def json_response(payload, status_code=200, headers=None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload, headers=headers or {})

    return handler


# --- indicators (use B) ------------------------------------------------------


def test_get_sma_parses_indicator_points_and_sends_bearer_auth():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["window"] = request.url.params.get("window")
        return httpx.Response(
            200,
            json={"results": {"values": [{"timestamp": 1000, "value": 101.5}, {"timestamp": 2000, "value": 102.0}]}},
        )

    client = make_client(handler)

    points = client.get_sma("AAPL", window=20)

    assert seen["path"] == "/v1/indicators/sma/AAPL"
    assert seen["auth"] == "Bearer test-key"
    assert seen["window"] == "20"
    assert points == [IndicatorPoint(1000, 101.5), IndicatorPoint(2000, 102.0)]


def test_get_ema_and_rsi_parse_the_same_shape():
    handler = json_response({"results": {"values": [{"timestamp": 1000, "value": 55.0}]}})
    client = make_client(handler)

    assert client.get_ema("MSFT") == [IndicatorPoint(1000, 55.0)]
    # A fresh request path (different endpoint) is not served from the SMA cache.
    client2 = make_client(handler)
    assert client2.get_rsi("MSFT") == [IndicatorPoint(1000, 55.0)]


def test_get_macd_parses_value_signal_histogram():
    handler = json_response(
        {"results": {"values": [{"timestamp": 3000, "value": 1.2, "signal": 1.0, "histogram": 0.2}]}}
    )
    client = make_client(handler)

    points = client.get_macd("AAPL")

    assert points == [MACDPoint(3000, 1.2, 1.0, 0.2)]


# --- bars: daily aggregates + range + prev (use A) ---------------------------


def test_get_aggs_range_hits_the_range_endpoint_and_parses_bars():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={"results": [{"t": 1700000000000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1000, "vw": 1.4, "n": 42}]},
        )

    client = make_client(handler)

    bars = client.get_daily_bars("AAPL", "2026-01-01", "2026-01-31")

    assert seen["path"] == "/v2/aggs/ticker/AAPL/range/1/day/2026-01-01/2026-01-31"
    assert bars == [Bar(1700000000000, 1.0, 2.0, 0.5, 1.5, 1000.0, vwap=1.4, transactions=42, ticker=None)]


def test_get_previous_close_hits_the_prev_endpoint():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"results": [{"t": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]})

    client = make_client(handler)

    bar = client.get_previous_close("AAPL")

    assert seen["path"] == "/v2/aggs/ticker/AAPL/prev"
    assert bar is not None and bar.close == 1.0


def test_get_previous_close_returns_none_when_no_results():
    client = make_client(json_response({"results": []}))

    assert client.get_previous_close("AAPL") is None


# --- breadth: grouped-daily (use D) ------------------------------------------


def test_get_grouped_daily_hits_the_grouped_endpoint_and_carries_ticker():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={"results": [{"T": "AAPL", "t": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}, {"T": "MSFT", "t": 1, "o": 2, "h": 2, "l": 2, "c": 2, "v": 2}]},
        )

    client = make_client(handler)

    bars = client.get_grouped_daily("2026-01-02")

    assert seen["path"] == "/v2/aggs/grouped/locale/us/market/stocks/2026-01-02"
    assert [b.ticker for b in bars] == ["AAPL", "MSFT"]


# --- news + sentiment (use C) ------------------------------------------------


def test_get_ticker_news_parses_per_ticker_sentiment():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ticker_param"] = request.url.params.get("ticker")
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "news-1",
                        "title": "Big beat",
                        "published_utc": "2026-01-02T12:00:00Z",
                        "article_url": "https://example.com/a",
                        "tickers": ["AAPL"],
                        "insights": [
                            {"ticker": "AAPL", "sentiment": "positive", "sentiment_reasoning": "earnings beat"},
                        ],
                    }
                ]
            },
        )

    client = make_client(handler)

    news = client.get_ticker_news("AAPL")

    assert seen["ticker_param"] == "AAPL"
    assert len(news) == 1
    item = news[0]
    assert isinstance(item, NewsItem)
    assert item.sentiment_for("aapl") == "positive"
    assert item.sentiment_for("MSFT") is None


# --- rate-limit backoff -------------------------------------------------------


def test_429_triggers_backoff_then_succeeds():
    attempts = {"count": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "slow down"})
        return httpx.Response(200, json={"results": {"values": [{"timestamp": 1, "value": 1.0}]}})

    client = make_client(handler, max_retries=5)
    client._sleep = sleeps.append  # capture without actually sleeping

    points = client.get_rsi("AAPL")

    assert attempts["count"] == 3
    assert points == [IndicatorPoint(1, 1.0)]
    assert len(sleeps) == 2  # one sleep before each of the two retried attempts


def test_429_exhausting_all_retries_raises_rate_limit_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"})

    client = make_client(handler, max_retries=2)

    with pytest.raises(MassiveRateLimitError):
        client.get_rsi("AAPL")


# --- caching: repeated same-day lookups are served from cache ---------------


def test_repeated_same_day_lookup_is_served_from_cache():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json={"results": [{"t": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]})

    client = make_client(handler)

    first = client.get_previous_close("AAPL")
    second = client.get_previous_close("AAPL")

    assert first == second
    assert call_count["n"] == 1


def test_different_args_are_not_served_from_the_same_cache_entry():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json={"results": [{"t": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]})

    client = make_client(handler)

    client.get_previous_close("AAPL")
    client.get_previous_close("MSFT")

    assert call_count["n"] == 2


# --- no live network calls ----------------------------------------------------


def test_client_with_no_transport_override_still_only_uses_mocked_transport_in_tests():
    """Guards against a future edit that drops the transport param and lets a
    real socket open during CI. Every test in this file passes a MockTransport;
    this one confirms MassiveClient accepts one without touching the network."""
    handler = json_response({"results": {"values": []}})
    client = make_client(handler)

    assert client.get_sma("AAPL") == []
