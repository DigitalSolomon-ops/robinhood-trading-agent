from __future__ import annotations

import pytest

from src.robinhood_equity_client import (
    AgentAccountMismatchError,
    NoAgentTradableAccountError,
    RobinhoodEquityClient,
)

AGENT_ACCOUNT = {"account_number": "AGENT-ACCT-0001", "nickname": "Agentic", "agent_tradable": True}
DEFAULT_ACCOUNT = {"account_number": "DEFAULT-ACCT-0002", "nickname": "Default", "agent_tradable": False}


class FakeConnector:
    """Records calls instead of reaching the real Robinhood MCP connector."""

    def __init__(self, accounts: list[dict] | None = None) -> None:
        self.accounts = accounts if accounts is not None else [AGENT_ACCOUNT, DEFAULT_ACCOUNT]
        self.quote_calls: list[dict] = []
        self.position_calls: list[dict] = []
        self.review_calls: list[dict] = []
        self.place_calls: list[dict] = []
        self.cancel_calls: list[dict] = []

    def get_accounts(self):
        return {"accounts": self.accounts}

    def get_equity_quotes(self, symbols):
        self.quote_calls.append({"symbols": symbols})
        return {"quotes": [{"symbol": symbol, "price": "1.00"} for symbol in symbols]}

    def get_equity_positions(self, account_number=None):
        self.position_calls.append({"account_number": account_number})
        return {"positions": []}

    def review_equity_order(self, **kwargs):
        self.review_calls.append(kwargs)
        return {"reviewed": True, **kwargs}

    def place_equity_order(self, **kwargs):
        self.place_calls.append(kwargs)
        return {"order_id": "order-1", "status": "accepted"}

    def cancel_equity_order(self, order_id, account_number=None):
        self.cancel_calls.append({"order_id": order_id, "account_number": account_number})
        return {"order_id": order_id, "status": "cancel_requested"}


def order_kwargs(**overrides):
    base = {
        "symbol": "SYMBOL",
        "side": "buy",
        "quantity": "1",
        "limit_price": "1.00",
    }
    return {**base, **overrides}


# --- account resolution ------------------------------------------------------


def test_resolves_and_pins_the_agent_tradable_account():
    client = RobinhoodEquityClient(FakeConnector())

    assert client.account_number == AGENT_ACCOUNT["account_number"]
    assert client.get_account() == AGENT_ACCOUNT


def test_raises_when_no_agent_tradable_account_exists():
    connector = FakeConnector(accounts=[DEFAULT_ACCOUNT])

    with pytest.raises(NoAgentTradableAccountError):
        RobinhoodEquityClient(connector)


def test_raises_when_more_than_one_agent_tradable_account_exists():
    other_agent_account = {"account_number": "AGENT-ACCT-9999", "nickname": "Agentic2", "agent_tradable": True}
    connector = FakeConnector(accounts=[AGENT_ACCOUNT, other_agent_account])

    with pytest.raises(NoAgentTradableAccountError):
        RobinhoodEquityClient(connector)


# --- reads --------------------------------------------------------------------


def test_get_quotes_calls_connector_with_requested_symbols():
    connector = FakeConnector()
    client = RobinhoodEquityClient(connector)

    client.get_quotes("SYMBOL_A", "SYMBOL_B")

    assert connector.quote_calls == [{"symbols": ["SYMBOL_A", "SYMBOL_B"]}]


def test_get_positions_scopes_to_the_pinned_account():
    connector = FakeConnector()
    client = RobinhoodEquityClient(connector)

    client.get_positions()

    assert connector.position_calls == [{"account_number": AGENT_ACCOUNT["account_number"]}]


def test_get_accounts_passes_through_to_the_connector():
    connector = FakeConnector()
    client = RobinhoodEquityClient(connector)

    result = client.get_accounts()

    assert result == {"accounts": [AGENT_ACCOUNT, DEFAULT_ACCOUNT]}


def test_review_order_reaches_the_connector_without_placing():
    connector = FakeConnector()
    client = RobinhoodEquityClient(connector)

    result = client.review_order(**order_kwargs())

    assert result["reviewed"] is True
    assert connector.review_calls[0]["account_number"] == AGENT_ACCOUNT["account_number"]
    assert connector.place_calls == []


def test_review_order_refuses_a_different_account():
    client = RobinhoodEquityClient(FakeConnector())

    with pytest.raises(AgentAccountMismatchError):
        client.review_order(**order_kwargs(account_number=DEFAULT_ACCOUNT["account_number"]))


# --- place_order account gate --------------------------------------------------


def test_place_order_refuses_the_default_account():
    connector = FakeConnector()
    client = RobinhoodEquityClient(connector)

    with pytest.raises(AgentAccountMismatchError):
        client.place_order(
            **order_kwargs(account_number=DEFAULT_ACCOUNT["account_number"]),
            dry_run=False,
            confirm_live_order=True,
        )

    assert connector.place_calls == []


def test_place_order_targets_the_pinned_account_by_default():
    connector = FakeConnector()
    client = RobinhoodEquityClient(connector)

    result = client.place_order(**order_kwargs())

    assert result["order_payload"]["account_number"] == AGENT_ACCOUNT["account_number"]


# --- place_order dry-run / confirm double gate --------------------------------


def test_place_order_defaults_to_dry_run_and_returns_a_payload():
    connector = FakeConnector()
    client = RobinhoodEquityClient(connector)

    result = client.place_order(**order_kwargs())

    assert result["submitted"] is False
    assert result["status"] == "dry_run_order_preview"
    assert result["order_payload"]["symbol"] == "SYMBOL"
    assert connector.place_calls == []


@pytest.mark.parametrize(
    "dry_run,confirm_live_order,case",
    [
        (True, False, "defaults"),
        (True, True, "confirm-only (dry_run still True)"),
        (False, False, "dry_run-only (no confirm)"),
    ],
)
def test_every_combination_but_both_flags_stays_unsubmitted(dry_run, confirm_live_order, case):
    connector = FakeConnector()
    client = RobinhoodEquityClient(connector)

    result = client.place_order(
        **order_kwargs(),
        dry_run=dry_run,
        confirm_live_order=confirm_live_order,
    )

    assert result["submitted"] is False, case
    assert connector.place_calls == [], case


def test_place_order_submits_only_when_dry_run_false_and_confirm_true():
    connector = FakeConnector()
    client = RobinhoodEquityClient(connector)

    result = client.place_order(**order_kwargs(), dry_run=False, confirm_live_order=True)

    assert result["submitted"] is True
    assert result["status"] == "submitted"
    assert len(connector.place_calls) == 1
    assert connector.place_calls[0]["account_number"] == AGENT_ACCOUNT["account_number"]


def test_inverted_confirm_caller_still_cannot_submit():
    """A caller with an inverted-if bug (`if not confirm: place(...)`) must not
    be able to trick the client into submitting -- the gate is enforced inside
    place_order itself, not by trusting caller-side polarity."""
    connector = FakeConnector()
    client = RobinhoodEquityClient(connector)

    def buggy_caller(confirm: bool):
        if not confirm:
            return client.place_order(**order_kwargs(), dry_run=False, confirm_live_order=confirm)
        return None

    result = buggy_caller(confirm=False)

    assert result["submitted"] is False
    assert connector.place_calls == []


# --- cancel_order mirrors the same gates --------------------------------------


def test_cancel_order_defaults_to_dry_run_and_does_not_submit():
    connector = FakeConnector()
    client = RobinhoodEquityClient(connector)

    result = client.cancel_order("order-1")

    assert result["status"] == "dry_run_cancel_prepared"
    assert connector.cancel_calls == []


def test_cancel_order_refuses_the_default_account():
    connector = FakeConnector()
    client = RobinhoodEquityClient(connector)

    with pytest.raises(AgentAccountMismatchError):
        client.cancel_order(
            "order-1",
            account_number=DEFAULT_ACCOUNT["account_number"],
            dry_run=False,
            confirm_live_order=True,
        )

    assert connector.cancel_calls == []


def test_cancel_order_submits_only_when_dry_run_false_and_confirm_true():
    connector = FakeConnector()
    client = RobinhoodEquityClient(connector)

    result = client.cancel_order("order-1", dry_run=False, confirm_live_order=True)

    assert result["status"] == "cancel_requested"
    assert connector.cancel_calls == [{"order_id": "order-1", "account_number": AGENT_ACCOUNT["account_number"]}]
