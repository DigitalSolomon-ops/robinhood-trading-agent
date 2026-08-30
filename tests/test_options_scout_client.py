from __future__ import annotations

import httpx

from src.equity_intelligence.massive_client import MassiveClient, OptionContract


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
