"""Scout enrichment: the Finnhub client + earnings-in-horizon / headline logic +
the options-email rendering of it.

ANALYSIS ONLY -- these assert read-only parsing/aggregation and HTML rendering;
nothing here trades.
"""

from __future__ import annotations

from datetime import date

import httpx

from src.options_scout.email import _enrichment_block
from src.scout_enrichment.enrichment import PlayEnrichment, enrich_symbols
from src.scout_enrichment.finnhub_client import FinnhubClient


# --- FinnhubClient -----------------------------------------------------------


def _mock_client(handler) -> FinnhubClient:
    return FinnhubClient(api_key="k", transport=httpx.MockTransport(handler))


def test_client_disabled_without_key_makes_no_calls():
    calls = {"n": 0}

    def handler(request):  # pragma: no cover - must never run
        calls["n"] += 1
        return httpx.Response(200, json={})

    client = FinnhubClient(api_key="", transport=httpx.MockTransport(handler))
    assert client.enabled is False
    assert client.get_earnings_calendar("2026-09-02", "2026-09-16") == []
    assert client.get_company_news("AAPL", "2026-09-01", "2026-09-02") == []
    assert calls["n"] == 0  # no HTTP attempted without a key


def test_client_parses_earnings_and_news():
    def handler(request):
        if "calendar/earnings" in request.url.path:
            return httpx.Response(200, json={"earningsCalendar": [{"symbol": "AAPL", "date": "2026-09-10"}]})
        if "company-news" in request.url.path:
            return httpx.Response(200, json=[{"headline": "Apple ships", "datetime": 123, "url": "u", "source": "Reuters"}])
        return httpx.Response(404)

    client = _mock_client(handler)
    assert client.enabled is True
    cal = client.get_earnings_calendar("2026-09-02", "2026-09-16")
    assert cal[0]["symbol"] == "AAPL"
    news = client.get_company_news("AAPL", "2026-09-01", "2026-09-02")
    assert news[0]["headline"] == "Apple ships"


def test_client_returns_empty_on_http_error():
    client = _mock_client(lambda request: httpx.Response(500))
    assert client.get_earnings_calendar("2026-09-02", "2026-09-16") == []
    assert client.get_company_news("AAPL", "2026-09-01", "2026-09-02") == []


# --- enrich_symbols ----------------------------------------------------------


class _FakeFinnhub:
    enabled = True

    def __init__(self, cal, news):
        self._cal = cal
        self._news = news

    def get_earnings_calendar(self, from_date, to_date, symbol=None):
        return self._cal

    def get_company_news(self, symbol, from_date, to_date):
        return self._news.get(symbol.upper(), [])


def test_enrich_symbols_flags_earnings_in_horizon_and_headline():
    cal = [
        {"symbol": "AAPL", "date": "2026-09-10"},   # 8 days out -> in horizon
        {"symbol": "MSFT", "date": "2026-09-30"},   # 28 days out -> outside horizon
    ]
    news = {"AAPL": [{"headline": "Apple ships", "datetime": 200, "url": "u", "source": "Reuters"}]}
    client = _FakeFinnhub(cal, news)

    out = enrich_symbols(["AAPL", "MSFT"], today=date(2026, 9, 2), earnings_horizon_days=14, client=client)

    assert out["AAPL"].earnings_in_horizon is True
    assert out["AAPL"].days_to_earnings == 8
    assert out["AAPL"].earnings_date == "2026-09-10"
    assert out["AAPL"].headline == "Apple ships"
    assert out["AAPL"].headline_source == "Reuters"
    # MSFT earnings exist but fall outside the horizon; still recorded, not flagged
    assert out["MSFT"].earnings_in_horizon is False
    assert out["MSFT"].days_to_earnings == 28
    assert out["MSFT"].headline is None


def test_enrich_symbols_picks_next_earnings_and_latest_headline():
    cal = [
        {"symbol": "NVDA", "date": "2026-09-20"},
        {"symbol": "NVDA", "date": "2026-09-08"},   # earlier -> should win
        {"symbol": "NVDA", "date": "2026-08-01"},   # past -> ignored
    ]
    news = {"NVDA": [
        {"headline": "old", "datetime": 100},
        {"headline": "newest", "datetime": 999},
        {"headline": "mid", "datetime": 500},
    ]}
    out = enrich_symbols(["NVDA"], today=date(2026, 9, 2), earnings_horizon_days=30, client=_FakeFinnhub(cal, news))
    assert out["NVDA"].earnings_date == "2026-09-08"
    assert out["NVDA"].headline == "newest"


def test_enrich_symbols_empty_without_key(monkeypatch):
    # No client passed + no key resolvable -> dormant, returns {}
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.setenv("DS_VAULT_NO_GCLOUD", "1")  # block the Secret Manager fallback
    out = enrich_symbols(["AAPL"], today=date(2026, 9, 2))
    assert out == {}


def test_enrich_symbols_empty_symbol_list():
    assert enrich_symbols([], today=date(2026, 9, 2), client=_FakeFinnhub([], {})) == {}


# --- email rendering ---------------------------------------------------------


def test_enrichment_block_renders_earnings_warning():
    html = _enrichment_block(PlayEnrichment(earnings_date="2026-09-10", days_to_earnings=8, earnings_in_horizon=True))
    assert "EARNINGS" in html and "2026-09-10" in html and "8d" in html


def test_enrichment_block_renders_headline_and_out_of_horizon_earnings():
    html = _enrichment_block(
        PlayEnrichment(earnings_date="2026-10-30", days_to_earnings=58, earnings_in_horizon=False,
                       headline="Big news", headline_source="WSJ")
    )
    assert "Next earnings" in html and "Big news" in html and "WSJ" in html
    assert "EARNINGS in" not in html  # not the in-horizon warning


def test_enrichment_block_none_is_empty():
    assert _enrichment_block(None) == ""
