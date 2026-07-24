from __future__ import annotations

import pytest

from src.alpaca_broker import AlpacaBroker
from src.alpaca_client import LIVE_BASE_URL, PAPER_BASE_URL, AlpacaClient
from src.portfolio import Portfolio

ACCOUNT = {"cash": "2500.00", "buying_power": "5000.00", "equity": "3100.00"}
POSITIONS = [
    {"symbol": "AAPL", "qty": "10", "avg_entry_price": "190.50", "unrealized_pl": "35.20"},
    {"symbol": "MSFT", "qty": "0", "avg_entry_price": "0", "unrealized_pl": "0"},
]


class StubClient:
    """Records calls instead of hitting Alpaca."""

    def __init__(self, paper: bool = True) -> None:
        self.paper = paper
        self.calls: list[dict] = []

    def get_account(self):
        return ACCOUNT

    def get_positions(self):
        return POSITIONS

    def place_order(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": "order-1", "status": "accepted"}

    def cancel_order(self, order_id):
        self.calls.append({"cancel": order_id})
        return {"id": order_id, "status": "canceled"}

    def get_order(self, order_id):
        return {"id": order_id, "status": "filled"}

    def get_option_contracts(self, **kwargs):
        self.calls.append(kwargs)
        return {"option_contracts": [{"symbol": "AAPL250117C00150000", "strike_price": "150"}]}


def order(**overrides):
    base = {
        "symbol": "AAPL",
        "side": "buy",
        "quantity": 5,
        "limit_price": 190.00,
        "time_in_force": "gtc",
        "client_order_id": "abc-123",
        "notional": 950.0,
    }
    return {**base, **overrides}


def test_client_defaults_to_paper_endpoint():
    client = AlpacaClient(api_key="k", api_secret="s")
    assert client.paper is True
    assert client.base_url == PAPER_BASE_URL


def test_client_live_endpoint_requires_explicit_opt_out():
    client = AlpacaClient(api_key="k", api_secret="s", paper=False)
    assert client.base_url == LIVE_BASE_URL


def test_missing_credentials_raise_before_any_request():
    client = AlpacaClient(api_key="", api_secret="")
    assert client.has_credentials is False
    with pytest.raises(ValueError):
        client.headers()


def test_portfolio_uses_cash_not_buying_power():
    # Margin must never be counted as available capital.
    portfolio = Portfolio.from_alpaca(ACCOUNT, POSITIONS)
    assert portfolio.cash_usd == 2500.00
    assert portfolio.cash_usd != float(ACCOUNT["buying_power"])


def test_portfolio_skips_zero_quantity_positions():
    portfolio = Portfolio.from_alpaca(ACCOUNT, POSITIONS)
    assert "AAPL" in portfolio.positions
    assert "MSFT" not in portfolio.positions
    assert portfolio.open_position_count == 1
    assert portfolio.positions["AAPL"].average_price == 190.50


def test_dry_run_never_submits():
    stub = StubClient()
    broker = AlpacaBroker(stub, dry_run=True)
    result = broker.place_limit_order(order())
    assert result["submitted"] is False
    assert result["status"] == "dry_run_order_preview"
    assert stub.calls == []


def test_live_submit_sends_limit_order():
    stub = StubClient()
    broker = AlpacaBroker(stub, dry_run=False)
    result = broker.place_limit_order(order())
    assert result["submitted"] is True
    assert stub.calls[0]["symbol"] == "AAPL"
    assert stub.calls[0]["order_type"] == "limit"
    assert stub.calls[0]["limit_price"] == "190.0"


def test_gtc_maps_to_alpaca_vocabulary():
    assert AlpacaBroker._time_in_force("gtc") == "gtc"
    assert AlpacaBroker._time_in_force("gfd") == "day"
    assert AlpacaBroker._time_in_force(None) == "day"
    assert AlpacaBroker._time_in_force("nonsense") == "day"


def test_option_order_is_day_only():
    stub = StubClient()
    broker = AlpacaBroker(stub, dry_run=False)
    broker.place_option_limit_order(order(symbol="AAPL250117C00150000", quantity=2, time_in_force="gtc"))
    # gtc is rejected on single-leg options; the broker must force day.
    assert stub.calls[0]["time_in_force"] == "day"
    assert stub.calls[0]["quantity"] == "2"


def test_option_order_rejects_fractional_contracts():
    broker = AlpacaBroker(StubClient(), dry_run=True)
    with pytest.raises(ValueError):
        broker.place_option_limit_order(order(symbol="AAPL250117C00150000", quantity=0.5))


def test_dry_run_option_order_does_not_submit():
    stub = StubClient()
    broker = AlpacaBroker(stub, dry_run=True)
    result = broker.place_option_limit_order(order(symbol="AAPL250117C00150000", quantity=1))
    assert result["submitted"] is False
    assert result["contracts"] == 1
    assert stub.calls == []


def test_find_option_contract_resolves_occ_symbol():
    broker = AlpacaBroker(StubClient(), dry_run=True)
    contract = broker.find_option_contract("AAPL", "2025-01-17", "call", 150.0)
    assert contract["symbol"] == "AAPL250117C00150000"


def test_cancel_is_inert_in_dry_run():
    stub = StubClient()
    broker = AlpacaBroker(stub, dry_run=True)
    assert broker.cancel_order("order-1")["status"] == "dry_run_cancel_prepared"
    assert stub.calls == []
