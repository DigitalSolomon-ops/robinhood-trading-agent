from __future__ import annotations

import httpx

from src.equity_intelligence.massive_client import MassiveClient, TickerDetails


def make_client(handler) -> MassiveClient:
    transport = httpx.MockTransport(handler)
    return MassiveClient(api_key="test-key", transport=transport, sleep=lambda _s: None)


def test_get_ticker_details_hits_v3_reference_and_parses_share_class():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "results": {
                    "ticker": "WINR",
                    "name": "Winner Inc",
                    "share_class_shares_outstanding": 10_000_000,
                    "weighted_shares_outstanding": 9_500_000,
                    "market_cap": 60_000_000,
                    "primary_exchange": "XNAS",
                }
            },
        )

    details = make_client(handler).get_ticker_details("WINR")

    assert seen["path"] == "/v3/reference/tickers/WINR"
    assert details == TickerDetails(
        ticker="WINR",
        name="Winner Inc",
        shares_outstanding=10_000_000.0,          # share_class preferred
        shares_basis="share_class_shares_outstanding",
        market_cap=60_000_000.0,
        primary_exchange="XNAS",
    )


def test_get_ticker_details_falls_back_to_weighted_shares():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": {
                    "ticker": "WGHT",
                    "name": "Weighted Co",
                    "weighted_shares_outstanding": 7_000_000,
                    "market_cap": 40_000_000,
                }
            },
        )

    details = make_client(handler).get_ticker_details("WGHT")
    assert details.shares_outstanding == 7_000_000.0
    assert details.shares_basis == "weighted_shares_outstanding"


def test_get_ticker_details_handles_missing_shares_and_market_cap():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": {"ticker": "NADA", "name": "No Data"}})

    details = make_client(handler).get_ticker_details("NADA")
    assert details.shares_outstanding is None
    assert details.shares_basis is None
    assert details.market_cap is None


def test_get_ticker_details_tolerates_empty_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": None})

    details = make_client(handler).get_ticker_details("EMPTY")
    assert details.ticker == "EMPTY"
    assert details.shares_outstanding is None
