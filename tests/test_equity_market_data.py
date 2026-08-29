from __future__ import annotations

from pathlib import Path

from src.equity_market_data import EquityMarketDataService
from src.robinhood_equity_client import RobinhoodEquityClient

AGENT_ACCOUNT = {"account_number": "AGENT-ACCT-0001", "nickname": "Agentic", "agent_tradable": True}
DEFAULT_ACCOUNT = {"account_number": "DEFAULT-ACCT-0002", "nickname": "Default", "agent_tradable": False}


class FakeConnector:
    def __init__(self, quotes: dict[str, dict]) -> None:
        self.quotes = quotes
        self.quote_calls: list[list[str]] = []

    def get_accounts(self):
        return {"accounts": [AGENT_ACCOUNT, DEFAULT_ACCOUNT]}

    def get_equity_quotes(self, symbols):
        self.quote_calls.append(list(symbols))
        return {"quotes": [self.quotes[symbol] for symbol in symbols if symbol in self.quotes]}

    def get_equity_positions(self, account_number=None):
        return {"positions": []}

    def review_equity_order(self, **kwargs):
        return {"reviewed": True}

    def place_equity_order(self, **kwargs):
        raise AssertionError("market data must never place an order")

    def cancel_equity_order(self, order_id, account_number=None):
        raise AssertionError("market data must never cancel an order")


def test_get_latest_prices_only_calls_the_read_only_quote_tool(tmp_path: Path) -> None:
    connector = FakeConnector({"AAPL": {"symbol": "AAPL", "price": "150.25"}})
    client = RobinhoodEquityClient(connector)
    service = EquityMarketDataService(client, tmp_path / "equity_market_data.db")

    prices = service.get_latest_prices(["AAPL"])

    assert prices == {"AAPL": 150.25}
    assert connector.quote_calls == [["AAPL"]]


def test_missing_or_zero_price_rows_are_skipped(tmp_path: Path) -> None:
    connector = FakeConnector({"AAPL": {"symbol": "AAPL", "price": "0"}})
    client = RobinhoodEquityClient(connector)
    service = EquityMarketDataService(client, tmp_path / "equity_market_data.db")

    prices = service.get_latest_prices(["AAPL", "MSFT"])

    assert prices == {}
    assert service.total_rows() == 0


def test_recent_history_accumulates_across_calls_oldest_first(tmp_path: Path) -> None:
    connector = FakeConnector({"AAPL": {"symbol": "AAPL", "price": "100"}})
    client = RobinhoodEquityClient(connector)
    service = EquityMarketDataService(client, tmp_path / "equity_market_data.db")

    for price in ("100", "101", "102"):
        connector.quotes["AAPL"]["price"] = price
        service.get_latest_prices(["AAPL"])

    assert service.recent_history("AAPL") == [100.0, 101.0, 102.0]
    assert service.history_count("AAPL") == 3
    assert service.total_rows() == 3


def test_falls_back_through_price_field_aliases(tmp_path: Path) -> None:
    connector = FakeConnector({"AAPL": {"symbol": "AAPL", "last_trade_price": "199.5"}})
    client = RobinhoodEquityClient(connector)
    service = EquityMarketDataService(client, tmp_path / "equity_market_data.db")

    prices = service.get_latest_prices(["AAPL"])

    assert prices == {"AAPL": 199.5}
