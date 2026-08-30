from __future__ import annotations

import pytest

from src.robinhood_equity_client import (
    AgentAccountIdentityError,
    AgentAccountMismatchError,
    NoAgentTradableAccountError,
    RobinhoodEquityClient,
)

# The real connector payload marks the agent-tradable account with the boolean
# `agentic_allowed` (verified against the live Robinhood connector 2026-08-28)
# and NOT `agent_tradable`. These fixtures DELIBERATELY OMIT agent_tradable so a
# regression to the old field name fails fast here. The agent account carries the
# ground-truth identity: nickname "Agentic", number ending 2092. The default
# ••2833 account is off-limits (agentic_allowed=False).
AGENT_ACCOUNT = {"account_number": "RH-EQ-AGENTIC-2092", "nickname": "Agentic", "agentic_allowed": True}
DEFAULT_ACCOUNT = {"account_number": "RH-EQ-DEFAULT-2833", "nickname": "Default", "agentic_allowed": False}

# The out-of-band identity anchor from config/trading_rules.yaml (equities.expected_account),
# injected directly so these unit tests never depend on a config file on disk.
EXPECTED_ACCOUNT = {"nickname": "Agentic", "number_suffix": "2092"}


def make_client(connector: "FakeConnector") -> RobinhoodEquityClient:
    return RobinhoodEquityClient(connector, expected_account=EXPECTED_ACCOUNT)


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
    client = make_client(FakeConnector())

    assert client.account_number == AGENT_ACCOUNT["account_number"]
    assert client.get_account() == AGENT_ACCOUNT


def test_raises_when_no_agent_tradable_account_exists():
    connector = FakeConnector(accounts=[DEFAULT_ACCOUNT])

    with pytest.raises(NoAgentTradableAccountError):
        make_client(connector)


def test_raises_when_more_than_one_agent_tradable_account_exists():
    other_agent_account = {"account_number": "RH-EQ-OTHER-9999", "nickname": "Agentic2", "agentic_allowed": True}
    connector = FakeConnector(accounts=[AGENT_ACCOUNT, other_agent_account])

    with pytest.raises(NoAgentTradableAccountError):
        make_client(connector)


# --- identity anchor (agentic_allowed is necessary, NOT sufficient) -----------


def test_agentic_allowed_field_is_read_not_the_legacy_agent_tradable_field():
    """The real connector marks the tradable account with `agentic_allowed`.
    This fixture DELIBERATELY OMITS agent_tradable entirely; resolution must
    still succeed on the real field. If _resolve_agent_account regresses to
    reading agent_tradable, this raises NoAgentTradableAccountError."""
    only_agentic = {"account_number": "RH-EQ-AGENTIC-2092", "nickname": "Agentic", "agentic_allowed": True}
    assert "agent_tradable" not in only_agentic
    connector = FakeConnector(accounts=[only_agentic, DEFAULT_ACCOUNT])

    client = make_client(connector)

    assert client.account_number == "RH-EQ-AGENTIC-2092"
    assert client.nickname == "Agentic"


def test_agentic_flag_on_wrong_number_is_rejected_not_pinned():
    """MUTATION TEST: a payload that flips agentic_allowed on the off-limits
    ••2833 account (wrong number suffix) must RAISE, never pin. If the identity
    cross-check is reverted, the client would silently pin ••2833 and this fails."""
    impostor = {"account_number": "RH-EQ-DEFAULT-2833", "nickname": "Agentic", "agentic_allowed": True}
    connector = FakeConnector(accounts=[impostor])

    with pytest.raises(AgentAccountIdentityError):
        make_client(connector)


def test_agentic_flag_on_wrong_nickname_is_rejected_not_pinned():
    """MUTATION TEST: a payload with the right number suffix but the wrong
    nickname must RAISE. Both nickname AND suffix are required to match."""
    impostor = {"account_number": "RH-EQ-SOMETHING-2092", "nickname": "Default", "agentic_allowed": True}
    connector = FakeConnector(accounts=[impostor])

    with pytest.raises(AgentAccountIdentityError):
        make_client(connector)


def test_identity_anchor_is_loaded_from_config_when_not_injected(tmp_path):
    """When no expected_account is injected, the anchor is read from
    config/trading_rules.yaml under the given config_root -- so the check can
    never be silently skipped by omitting the argument."""
    import yaml

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "trading_rules.yaml").write_text(
        yaml.safe_dump({"equities": {"expected_account": {"nickname": "Agentic", "number_suffix": "2092"}}}),
        encoding="utf-8",
    )

    client = RobinhoodEquityClient(FakeConnector(), config_root=tmp_path)

    assert client.account_number == AGENT_ACCOUNT["account_number"]

    # An impostor is still rejected when the anchor comes from config.
    impostor = {"account_number": "RH-EQ-DEFAULT-2833", "nickname": "Agentic", "agentic_allowed": True}
    with pytest.raises(AgentAccountIdentityError):
        RobinhoodEquityClient(FakeConnector(accounts=[impostor]), config_root=tmp_path)


# --- reads --------------------------------------------------------------------


def test_get_quotes_calls_connector_with_requested_symbols():
    connector = FakeConnector()
    client = make_client(connector)

    client.get_quotes("SYMBOL_A", "SYMBOL_B")

    assert connector.quote_calls == [{"symbols": ["SYMBOL_A", "SYMBOL_B"]}]


def test_get_positions_scopes_to_the_pinned_account():
    connector = FakeConnector()
    client = make_client(connector)

    client.get_positions()

    assert connector.position_calls == [{"account_number": AGENT_ACCOUNT["account_number"]}]


def test_get_accounts_passes_through_to_the_connector():
    connector = FakeConnector()
    client = make_client(connector)

    result = client.get_accounts()

    assert result == {"accounts": [AGENT_ACCOUNT, DEFAULT_ACCOUNT]}


def test_review_order_reaches_the_connector_without_placing():
    connector = FakeConnector()
    client = make_client(connector)

    result = client.review_order(**order_kwargs())

    assert result["reviewed"] is True
    assert connector.review_calls[0]["account_number"] == AGENT_ACCOUNT["account_number"]
    assert connector.place_calls == []


def test_review_order_refuses_a_different_account():
    client = make_client(FakeConnector())

    with pytest.raises(AgentAccountMismatchError):
        client.review_order(**order_kwargs(account_number=DEFAULT_ACCOUNT["account_number"]))


# --- place_order account gate --------------------------------------------------


def test_place_order_refuses_the_default_account():
    connector = FakeConnector()
    client = make_client(connector)

    with pytest.raises(AgentAccountMismatchError):
        client.place_order(
            **order_kwargs(account_number=DEFAULT_ACCOUNT["account_number"]),
            dry_run=False,
            confirm_live_order=True,
        )

    assert connector.place_calls == []


def test_place_order_targets_the_pinned_account_by_default():
    connector = FakeConnector()
    client = make_client(connector)

    result = client.place_order(**order_kwargs())

    assert result["order_payload"]["account_number"] == AGENT_ACCOUNT["account_number"]


# --- place_order dry-run / confirm double gate --------------------------------


def test_place_order_defaults_to_dry_run_and_returns_a_payload():
    connector = FakeConnector()
    client = make_client(connector)

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
    client = make_client(connector)

    result = client.place_order(
        **order_kwargs(),
        dry_run=dry_run,
        confirm_live_order=confirm_live_order,
    )

    assert result["submitted"] is False, case
    assert connector.place_calls == [], case


def test_place_order_submits_only_when_dry_run_false_and_confirm_true():
    connector = FakeConnector()
    client = make_client(connector)

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
    client = make_client(connector)

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
    client = make_client(connector)

    result = client.cancel_order("order-1")

    assert result["status"] == "dry_run_cancel_prepared"
    assert connector.cancel_calls == []


def test_cancel_order_refuses_the_default_account():
    connector = FakeConnector()
    client = make_client(connector)

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
    client = make_client(connector)

    result = client.cancel_order("order-1", dry_run=False, confirm_live_order=True)

    assert result["status"] == "cancel_requested"
    assert connector.cancel_calls == [{"order_id": "order-1", "account_number": AGENT_ACCOUNT["account_number"]}]
