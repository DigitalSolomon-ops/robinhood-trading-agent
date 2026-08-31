"""The headless paper-proving connector: it exposes the pinned agent-account
identity so the client resolves the pin, and REFUSES every real-order path."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.equity_runtime import PaperProvingConnector, build_paper_proving_connector
from src.robinhood_equity_client import RobinhoodEquityClient

ROOT = Path(__file__).resolve().parents[1]


def test_connector_exposes_only_the_agentic_identity() -> None:
    c = build_paper_proving_connector(ROOT)
    accounts = c.get_accounts()["accounts"]
    assert len(accounts) == 1
    acct = accounts[0]
    assert acct["nickname"] == "Agentic"
    assert acct["account_number"].endswith("2092")
    assert acct["agentic_allowed"] is True


def test_positions_are_empty_and_quotes_only_signal_tradability() -> None:
    c = build_paper_proving_connector(ROOT)
    assert c.get_equity_positions()["positions"] == []
    # The connector quote is ONLY the tradability "is-it-active" signal (present +
    # active + positive price), never the price source -- Massive prices the run.
    q = c.get_equity_quotes(["AAPL"])["quotes"]
    assert len(q) == 1 and q[0]["symbol"] == "AAPL"
    assert q[0]["state"] == "active" and float(q[0]["price"]) > 0


@pytest.mark.parametrize("call", ["place", "review", "cancel"])
def test_every_real_order_path_is_refused(call: str) -> None:
    c = build_paper_proving_connector(ROOT)
    with pytest.raises(RuntimeError):
        if call == "place":
            c.place_equity_order(symbol="AAPL", side="buy", quantity=1)
        elif call == "review":
            c.review_equity_order(symbol="AAPL", side="buy", quantity=1)
        else:
            c.cancel_equity_order("order-1")


def test_client_resolves_and_pins_the_account_from_this_connector() -> None:
    # The real confinement code (field + identity anchor) must accept it.
    client = RobinhoodEquityClient(build_paper_proving_connector(ROOT), config_root=ROOT)
    assert client.account_number.endswith("2092")
