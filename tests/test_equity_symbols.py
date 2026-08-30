from __future__ import annotations

import yaml

from src.equity_symbols import equities_universe, validate_equity_symbols
from src.robinhood_equity_client import RobinhoodEquityClient

AGENT_ACCOUNT = {"account_number": "RH-EQ-AGENTIC-2092", "nickname": "Agentic", "agentic_allowed": True}

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


class FakeConnector:
    """Records quote calls; returns a canned row per symbol from `quotes`."""

    def __init__(self, quotes: dict[str, dict] | None = None) -> None:
        self.quotes = quotes or {}
        self.quote_calls: list[list[str]] = []

    def get_accounts(self):
        return {"accounts": [AGENT_ACCOUNT]}

    def get_equity_quotes(self, symbols):
        self.quote_calls.append(list(symbols))
        rows = []
        for symbol in symbols:
            row = self.quotes.get(symbol)
            if row is not None:
                rows.append({"symbol": symbol, **row})
        return {"quotes": rows}

    def get_equity_positions(self, account_number=None):
        return {"positions": []}

    def review_equity_order(self, **kwargs):
        return {"reviewed": True}

    def place_equity_order(self, **kwargs):
        return {"order_id": "order-1"}

    def cancel_equity_order(self, order_id, account_number=None):
        return {"status": "cancel_requested"}


def load_real_rules() -> dict:
    with (ROOT / "config" / "trading_rules.yaml").open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


# --- universe is its own list, separate from crypto -----------------------


def test_equities_universe_reads_the_equities_config_key():
    rules = {"equities": {"universe": ["AAPL", "msft"]}}

    assert equities_universe(rules) == ["AAPL", "MSFT"]


def test_equities_universe_is_disjoint_from_the_crypto_allowed_symbols():
    rules = load_real_rules()

    crypto_symbols = set(rules["trading"]["allowed_symbols"])
    equities = set(equities_universe(rules))

    assert equities, "equities.universe must be configured"
    assert equities.isdisjoint(crypto_symbols)
    # Sanity: this is a plain-equity universe, not crypto pairs.
    assert all("-USD" not in symbol for symbol in equities)


def test_equities_universe_is_small_and_liquid_large_caps():
    rules = load_real_rules()

    equities = equities_universe(rules)

    assert 1 <= len(equities) <= 10
    assert set(equities) <= {"AAPL", "MSFT", "AMZN", "GOOGL", "GOOG", "NVDA", "META", "TSLA", "AVGO", "JPM"}


# --- validate_equity_symbols confirms an active connector quote per ticker ---


def test_validate_marks_symbol_available_when_quote_has_a_positive_price():
    connector = FakeConnector(quotes={"AAPL": {"price": "225.10"}})
    client = RobinhoodEquityClient(connector)
    rules = {"equities": {"universe": ["AAPL"]}}

    result = validate_equity_symbols(client, rules)

    assert result["available"] == ["AAPL"]
    assert result["unavailable"] == []
    assert connector.quote_calls == [["AAPL"]]


def test_validate_marks_symbol_unavailable_when_connector_returns_no_quote():
    connector = FakeConnector(quotes={})
    client = RobinhoodEquityClient(connector)
    rules = {"equities": {"universe": ["NVDA"]}}

    result = validate_equity_symbols(client, rules)

    assert result["unavailable"] == ["NVDA"]
    assert result["available"] == []
    assert result["details"]["NVDA"]["reason"] == "connector returned no quote"


def test_validate_marks_symbol_unavailable_when_quote_is_halted():
    connector = FakeConnector(quotes={"MSFT": {"price": "410.00", "state": "halted"}})
    client = RobinhoodEquityClient(connector)
    rules = {"equities": {"universe": ["MSFT"]}}

    result = validate_equity_symbols(client, rules)

    assert result["unavailable"] == ["MSFT"]


def test_validate_marks_symbol_unavailable_when_price_is_zero_or_missing():
    connector = FakeConnector(quotes={"AMZN": {"price": "0"}, "GOOGL": {}})
    client = RobinhoodEquityClient(connector)
    rules = {"equities": {"universe": ["AMZN", "GOOGL"]}}

    result = validate_equity_symbols(client, rules)

    assert set(result["unavailable"]) == {"AMZN", "GOOGL"}
    assert result["available"] == []


def test_validate_checks_every_universe_ticker_in_one_connector_call():
    connector = FakeConnector(quotes={"AAPL": {"price": "1"}, "MSFT": {"price": "2"}})
    client = RobinhoodEquityClient(connector)
    rules = {"equities": {"universe": ["AAPL", "MSFT"]}}

    result = validate_equity_symbols(client, rules)

    assert sorted(result["available"]) == ["AAPL", "MSFT"]
    assert connector.quote_calls == [["AAPL", "MSFT"]]


def test_validate_against_the_real_configured_universe_all_available():
    rules = load_real_rules()
    universe = equities_universe(rules)
    connector = FakeConnector(quotes={symbol: {"price": "100.00"} for symbol in universe})
    client = RobinhoodEquityClient(connector)

    result = validate_equity_symbols(client, rules)

    assert result["universe"] == universe
    assert sorted(result["available"]) == sorted(universe)
    assert result["unavailable"] == []


def test_validate_logs_a_human_readable_rationale_when_a_logger_is_given():
    connector = FakeConnector(quotes={"AAPL": {"price": "225.10"}})
    client = RobinhoodEquityClient(connector)
    rules = {"equities": {"universe": ["AAPL"]}}

    calls: list[tuple] = []

    class FakeLogger:
        def log_decision(self, symbol, action, reason, details=None):
            calls.append((symbol, action, reason, details))

    validate_equity_symbols(client, rules, logger=FakeLogger())

    assert len(calls) == 1
    symbol, action, reason, details = calls[0]
    assert symbol is None
    assert action == "equity_symbols_validated"
    assert "1/1" in reason
    assert details["available"] == ["AAPL"]
