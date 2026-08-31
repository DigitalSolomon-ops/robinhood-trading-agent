"""MassiveClient.get_current_price -- the delayed MINUTE-AGGREGATE read the
entry-hit alerter polls (the Options plan does not authorize snapshot/last-trade,
but stock minute aggregates ARE authorized, ~15-min delayed). All network is
mocked. NOTIFICATION-ONLY read; no order path.
"""

from __future__ import annotations

import httpx

from src.equity_intelligence.massive_client import MassiveClient


def make_client(handler) -> MassiveClient:
    transport = httpx.MockTransport(handler)
    return MassiveClient(api_key="test-key", transport=transport, sleep=lambda _s: None)


def test_uses_latest_minute_bar_close_newest_first():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"results": [{"c": 205.12, "t": 1}]})

    price = make_client(handler).get_current_price("AAPL", on_date="2026-08-31")
    assert seen["path"] == "/v2/aggs/ticker/AAPL/range/1/minute/2026-08-31/2026-08-31"
    assert seen["params"]["sort"] == "desc"
    assert seen["params"]["limit"] == "1"
    assert price == 205.12


def test_falls_back_to_previous_close_when_no_intraday_bars():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/prev" in request.url.path:
            return httpx.Response(200, json={"results": [{"c": 198.0, "t": 1}]})
        return httpx.Response(200, json={"results": []})  # empty intraday (pre-open)

    price = make_client(handler).get_current_price("AAPL", on_date="2026-08-31")
    assert price == 198.0


def test_none_when_neither_intraday_nor_prev_has_a_price():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    assert make_client(handler).get_current_price("X", on_date="2026-08-31") is None


def test_zero_minute_close_falls_through_to_prev():
    def handler(request: httpx.Request) -> httpx.Response:
        if "/prev" in request.url.path:
            return httpx.Response(200, json={"results": [{"c": 11.0, "t": 1}]})
        return httpx.Response(200, json={"results": [{"c": 0, "t": 1}]})

    assert make_client(handler).get_current_price("X", on_date="2026-08-31") == 11.0


def test_free_tier_unauthorized_raises_like_any_other_read():
    """On the free (EOD-only) tier the intraday minute read is NOT_AUTHORIZED and
    403s -- surfaced as an HTTPStatusError the alerter catches (reports no price,
    never fires)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"status": "NOT_AUTHORIZED"})

    try:
        make_client(handler).get_current_price("AAPL", on_date="2026-08-31")
    except httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 403
    else:
        raise AssertionError("expected a 403 HTTPStatusError")
