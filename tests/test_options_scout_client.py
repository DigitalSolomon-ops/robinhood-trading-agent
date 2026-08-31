from __future__ import annotations

import httpx

from src.equity_intelligence.massive_client import (
    MassiveClient,
    OptionContract,
    OptionSnapshot,
)


def make_client(handler) -> MassiveClient:
    transport = httpx.MockTransport(handler)
    return MassiveClient(api_key="test-key", transport=transport, sleep=lambda _s: None)


def test_get_option_contracts_hits_v3_reference_and_parses():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "ticker": "O:AAPL260831C00205000",
                        "underlying_ticker": "AAPL",
                        "contract_type": "call",
                        "strike_price": 205.0,
                        "expiration_date": "2026-08-31",
                    },
                    {
                        "ticker": "O:AAPL260831C00210000",
                        "underlying_ticker": "AAPL",
                        "contract_type": "call",
                        "strike_price": 210.0,
                        "expiration_date": "2026-08-31",
                    },
                ]
            },
        )

    client = make_client(handler)
    contracts = client.get_option_contracts(
        "AAPL", contract_type="call", expiration_gte="2026-08-20", strike_gte=190, strike_lte=220
    )

    assert seen["path"] == "/v3/reference/options/contracts"
    assert seen["params"]["underlying_ticker"] == "AAPL"
    assert seen["params"]["contract_type"] == "call"
    assert seen["params"]["expiration_date.gte"] == "2026-08-20"
    assert seen["params"]["strike_price.gte"] == "190"
    assert seen["params"]["strike_price.lte"] == "220"
    assert contracts[0] == OptionContract(
        ticker="O:AAPL260831C00205000",
        underlying_ticker="AAPL",
        contract_type="call",
        strike_price=205.0,
        expiration_date="2026-08-31",
    )
    assert len(contracts) == 2


def test_get_option_contracts_skips_rows_without_strike():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": [{"ticker": "O:X", "contract_type": "put", "expiration_date": "2026-09-18"}]},
        )

    contracts = make_client(handler).get_option_contracts("X", contract_type="put")
    assert contracts == []


def test_get_option_contracts_empty_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    assert make_client(handler).get_option_contracts("X") == []


# --- option snapshot (Options plan: premium / OI / greeks / IV) --------------


def test_get_option_snapshot_market_hours_has_greeks():
    """Market-hours read: last_quote midpoint is the premium, greeks + IV
    populate. Hits the v3 snapshot endpoint under the underlying."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "results": {
                    "ticker": "O:AAPL260918C00210000",
                    "open_interest": 4213,
                    "implied_volatility": 0.2841,
                    "day": {"close": 5.10, "volume": 1875, "vwap": 5.2},
                    "last_quote": {"bid": 5.15, "ask": 5.35, "midpoint": 5.25},
                    "greeks": {"delta": 0.42, "gamma": 0.03, "theta": -0.08, "vega": 0.12},
                    "details": {"ticker": "O:AAPL260918C00210000", "strike_price": 210.0},
                }
            },
        )

    snap = make_client(handler).get_option_snapshot("AAPL", "O:AAPL260918C00210000")

    assert seen["path"] == "/v3/snapshot/options/AAPL/O:AAPL260918C00210000"
    assert isinstance(snap, OptionSnapshot)
    # Premium prefers the live-quote midpoint over the day close.
    assert snap.premium == 5.25
    assert snap.premium_source == "last_quote_midpoint"
    assert snap.open_interest == 4213
    assert snap.day_volume == 1875
    assert snap.day_close == 5.10
    assert snap.has_greeks is True
    assert snap.delta == 0.42
    assert snap.theta == -0.08
    assert snap.implied_volatility == 0.2841


def test_get_option_snapshot_weekend_greeks_null_but_premium_and_oi_present():
    """Weekend read: greeks + IV come back null (no live quotes to compute
    from), but premium (falling back to day close) and open interest still
    populate. The typed result must carry None greeks, not crash."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": {
                    "ticker": "O:AAPL260918C00210000",
                    "open_interest": 4213,
                    "implied_volatility": None,
                    "day": {"close": 5.10, "volume": 1875},
                    # No midpoint and no two-sided quote off-hours.
                    "last_quote": {"bid": 0, "ask": 0, "midpoint": None},
                    "greeks": {"delta": None, "gamma": None, "theta": None, "vega": None},
                }
            },
        )

    snap = make_client(handler).get_option_snapshot("AAPL", "O:AAPL260918C00210000")

    assert snap is not None
    # Falls back to the day close for premium.
    assert snap.premium == 5.10
    assert snap.premium_source == "day_close"
    assert snap.open_interest == 4213
    assert snap.day_volume == 1875
    # Greeks and IV are None off market hours, and has_greeks reflects that.
    assert snap.has_greeks is False
    assert snap.delta is None
    assert snap.implied_volatility is None


def test_get_option_snapshot_derives_midpoint_from_two_sided_quote():
    """When the endpoint omits midpoint but a live two-sided quote exists, the
    premium is derived from (bid+ask)/2 rather than dropping to the day close."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": {
                    "ticker": "O:AAPL260918C00210000",
                    "open_interest": 10,
                    "day": {"close": 4.00, "volume": 5},
                    "last_quote": {"bid": 5.00, "ask": 5.40},
                    "greeks": {"delta": 0.5, "gamma": 0.02, "theta": -0.05, "vega": 0.1},
                    "implied_volatility": 0.3,
                }
            },
        )

    snap = make_client(handler).get_option_snapshot("AAPL", "O:AAPL260918C00210000")
    assert snap.premium == 5.20  # (5.00 + 5.40) / 2, not the 4.00 day close
    assert snap.premium_source == "last_quote_midpoint"


def test_get_option_snapshot_no_results_returns_none():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": None})

    assert make_client(handler).get_option_snapshot("X", "O:X") is None
