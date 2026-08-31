from __future__ import annotations

import pytest

from src.robinhood_option_client import (
    AgentAccountIdentityError,
    AgentAccountMismatchError,
    DefinedRiskViolationError,
    NoAgentTradableAccountError,
    RobinhoodOptionClient,
)

# The agent-tradable account carries the SAME ground-truth identity the equities
# lane pins: nickname "Agentic", number ending 2092, marked agentic_allowed. The
# default ••2833 account is off-limits (agentic_allowed=False). Fixtures omit any
# legacy `agent_tradable` field so a regression to the old name fails fast.
AGENT_ACCOUNT = {"account_number": "RH-OPT-AGENTIC-2092", "nickname": "Agentic", "agentic_allowed": True}
DEFAULT_ACCOUNT = {"account_number": "RH-OPT-DEFAULT-2833", "nickname": "Default", "agentic_allowed": False}

# The out-of-band identity anchor (equities.expected_account), injected so these
# unit tests never depend on a config file on disk.
EXPECTED_ACCOUNT = {"nickname": "Agentic", "number_suffix": "2092"}

LONG_CALL = "OPT-AAPL-CALL"
SHORT_CALL = "OPT-AAPL-CALL-HIGHER"


def _assert_connector_leg_shape(legs):
    """The real Robinhood options MCP order schema requires each leg to carry
    `option_id` (the option instrument UUID) -- NOT the legacy `option` key. A
    permissive stub that accepted the wrong key let a mis-keyed payload look
    placed; rejecting it here makes the normalizer's option_id re-keying
    load-bearing (revert it and the connector stub raises)."""
    assert legs is not None, "connector received no legs"
    for leg in legs:
        assert "option_id" in leg, f"leg missing required 'option_id': {leg}"
        assert "option" not in leg, f"leg carries the wrong key 'option' (schema wants 'option_id'): {leg}"


class FakeArmStore:
    """Records nothing; just answers is_armed for the requested lane."""

    def __init__(self, armed: bool = False) -> None:
        self.armed = armed
        self.queried: list[str] = []

    def is_armed(self, lane: str) -> bool:
        self.queried.append(lane)
        return self.armed


class FakeConnector:
    """Records calls instead of reaching the real Robinhood options connector."""

    def __init__(self, accounts: list[dict] | None = None) -> None:
        self.accounts = accounts if accounts is not None else [AGENT_ACCOUNT, DEFAULT_ACCOUNT]
        self.chain_calls: list[dict] = []
        self.quote_calls: list[dict] = []
        self.position_calls: list[dict] = []
        self.level_calls: list[dict] = []
        self.review_calls: list[dict] = []
        self.place_calls: list[dict] = []
        self.cancel_calls: list[dict] = []

    def get_accounts(self):
        return {"accounts": self.accounts}

    def get_option_chains(self, **kwargs):
        self.chain_calls.append(kwargs)
        return {"chains": []}

    def get_option_quotes(self, instrument_ids):
        # Strict signature grounded on the real MCP schema: the argument is
        # `instrument_ids`, not `ids`. A revert to ids= raises TypeError here.
        self.quote_calls.append({"instrument_ids": instrument_ids})
        return {"quotes": []}

    def get_option_positions(self, account_number=None):
        self.position_calls.append({"account_number": account_number})
        return {"positions": []}

    def get_option_level_upgrade_info(self, **kwargs):
        self.level_calls.append(kwargs)
        return {"option_level": "level_3"}

    def review_option_order(self, **kwargs):
        _assert_connector_leg_shape(kwargs.get("legs"))
        self.review_calls.append(kwargs)
        return {"reviewed": True, **kwargs}

    def place_option_order(self, **kwargs):
        _assert_connector_leg_shape(kwargs.get("legs"))
        self.place_calls.append(kwargs)
        return {"order_id": "opt-order-1", "status": "accepted"}

    def cancel_option_order(self, order_id, account_number=None):
        self.cancel_calls.append({"order_id": order_id, "account_number": account_number})
        return {"order_id": order_id, "status": "cancel_requested"}


def make_client(connector: FakeConnector, armed: bool = True) -> RobinhoodOptionClient:
    return RobinhoodOptionClient(
        connector, arm_store=FakeArmStore(armed=armed), expected_account=EXPECTED_ACCOUNT
    )


def long_legs(contract: str = LONG_CALL) -> list[dict]:
    """A single defined-risk BUY-to-open leg (max loss = premium)."""
    return [{"side": "buy", "position_effect": "open", "ratio_quantity": 1, "option": contract}]


def vertical_legs() -> list[dict]:
    """A long + short opening pair. Presence-only 'coverage' -- the audit's
    starting point: this looks like a vertical but the lane cannot yet prove the
    short is covered, so it is refused."""
    return [
        {"side": "buy", "position_effect": "open", "ratio_quantity": 1, "option": LONG_CALL},
        {"side": "sell", "position_effect": "open", "ratio_quantity": 1, "option": SHORT_CALL},
    ]


def ratio_legs() -> list[dict]:
    """A 10:1 ratio: buy 1 call, SELL 10 calls. A long leg is present, but the
    nine extra shorts are uncovered -- undefined risk that the old presence-only
    check waved through."""
    return [
        {"side": "buy", "position_effect": "open", "ratio_quantity": 1, "option": LONG_CALL},
        {"side": "sell", "position_effect": "open", "ratio_quantity": 10, "option": SHORT_CALL},
    ]


def sell_call_buy_put_legs() -> list[dict]:
    """A short call 'covered' by a long PUT. A buy leg is present, but a put does
    not cover a call -- the short call is naked. Undefined risk the old check
    passed as a defined_risk_spread."""
    return [
        {"side": "buy", "position_effect": "open", "ratio_quantity": 1, "option_type": "put", "option": "OPT-AAPL-PUT"},
        {"side": "sell", "position_effect": "open", "ratio_quantity": 1, "option_type": "call", "option": SHORT_CALL},
    ]


# --- account resolution + identity anchor (SAME as equities) ------------------


def test_resolves_and_pins_the_agent_tradable_account():
    client = make_client(FakeConnector())

    assert client.account_number == AGENT_ACCOUNT["account_number"]
    assert client.get_account() == AGENT_ACCOUNT


def test_raises_when_no_agent_tradable_account_exists():
    with pytest.raises(NoAgentTradableAccountError):
        make_client(FakeConnector(accounts=[DEFAULT_ACCOUNT]))


def test_raises_when_more_than_one_agent_tradable_account_exists():
    other = {"account_number": "RH-OPT-OTHER-9999", "nickname": "Agentic2", "agentic_allowed": True}
    with pytest.raises(NoAgentTradableAccountError):
        make_client(FakeConnector(accounts=[AGENT_ACCOUNT, other]))


def test_agentic_flag_on_wrong_number_is_rejected_not_pinned():
    """MUTATION TEST: a flipped agentic_allowed on the off-limits ••2833 account
    (wrong suffix) must RAISE, never pin. Reverting the identity cross-check
    silently pins ••2833 and fails here."""
    impostor = {"account_number": "RH-OPT-DEFAULT-2833", "nickname": "Agentic", "agentic_allowed": True}
    with pytest.raises(AgentAccountIdentityError):
        make_client(FakeConnector(accounts=[impostor]))


def test_agentic_flag_on_wrong_nickname_is_rejected_not_pinned():
    """MUTATION TEST: right suffix, wrong nickname must RAISE -- both must match."""
    impostor = {"account_number": "RH-OPT-SOMETHING-2092", "nickname": "Default", "agentic_allowed": True}
    with pytest.raises(AgentAccountIdentityError):
        make_client(FakeConnector(accounts=[impostor]))


def test_identity_anchor_is_loaded_from_equities_config_when_not_injected(tmp_path):
    """When no expected_account is injected, the anchor is read from
    config/trading_rules.yaml (equities.expected_account) -- the SAME anchor the
    equities lane uses -- so the check can never be skipped by omitting it."""
    import yaml

    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "trading_rules.yaml").write_text(
        yaml.safe_dump({"equities": {"expected_account": {"nickname": "Agentic", "number_suffix": "2092"}}}),
        encoding="utf-8",
    )

    client = RobinhoodOptionClient(
        FakeConnector(), arm_store=FakeArmStore(armed=True), config_root=tmp_path
    )
    assert client.account_number == AGENT_ACCOUNT["account_number"]

    impostor = {"account_number": "RH-OPT-DEFAULT-2833", "nickname": "Agentic", "agentic_allowed": True}
    with pytest.raises(AgentAccountIdentityError):
        RobinhoodOptionClient(
            FakeConnector(accounts=[impostor]), arm_store=FakeArmStore(armed=True), config_root=tmp_path
        )


# --- reads --------------------------------------------------------------------


def test_get_option_chains_passes_symbol_through():
    connector = FakeConnector()
    client = make_client(connector)

    client.get_option_chains("AAPL")

    assert connector.chain_calls == [{"symbol": "AAPL"}]


def test_get_option_quotes_passes_instrument_ids():
    """The connector's quote tool takes `instrument_ids` (the real MCP schema),
    not `ids`. Revert the client to ids= and the strict stub raises."""
    connector = FakeConnector()
    client = make_client(connector)

    client.get_option_quotes(LONG_CALL, SHORT_CALL)

    assert connector.quote_calls == [{"instrument_ids": [LONG_CALL, SHORT_CALL]}]


def test_get_option_positions_scopes_to_the_pinned_account():
    connector = FakeConnector()
    client = make_client(connector)

    client.get_option_positions()

    assert connector.position_calls == [{"account_number": AGENT_ACCOUNT["account_number"]}]


def test_get_option_level_upgrade_info_passes_through():
    connector = FakeConnector()
    client = make_client(connector)

    result = client.get_option_level_upgrade_info()

    assert result == {"option_level": "level_3"}
    assert connector.level_calls == [{}]


# --- defined-risk construction ------------------------------------------------


def test_build_single_leg_long_is_a_debit_buy_to_open():
    client = make_client(FakeConnector())

    payload = client.build_single_leg_long(LONG_CALL)

    assert payload["direction"] == "debit"
    assert payload["account_number"] == AGENT_ACCOUNT["account_number"]
    # The built payload carries the schema key `option_id`, re-keyed from the
    # `option` alias the caller supplied.
    assert payload["legs"] == [
        {"side": "buy", "position_effect": "open", "ratio_quantity": 1, "option_id": LONG_CALL}
    ]


def test_build_refuses_a_long_plus_short_opening_pair():
    """MUTATION TEST: a long + short opening pair is refused. The lane cannot yet
    prove the short is covered (no strike-aware spread support), so any
    sell-to-open leg -- even accompanied by a buy -- is refused outright. Revert
    the shared validator to the presence-only check and this builds instead."""
    client = make_client(FakeConnector())

    with pytest.raises(DefinedRiskViolationError):
        client.build_option_order(vertical_legs(), direction="debit")


def test_build_refuses_a_ten_to_one_ratio():
    """A 10:1 ratio has a long leg but nine uncovered extra shorts. The old
    presence-only 'there is a buy leg' check passed it; the coverage-aware
    validator refuses any opening sell, so it cannot build."""
    client = make_client(FakeConnector())

    with pytest.raises(DefinedRiskViolationError):
        client.build_option_order(ratio_legs(), direction="debit")


def test_build_refuses_a_short_call_covered_by_a_long_put():
    """A short call 'covered' by a long put is naked -- a put does not cover a
    call. A buy leg is present, so the old check passed it; the validator refuses
    the opening sell."""
    client = make_client(FakeConnector())

    with pytest.raises(DefinedRiskViolationError):
        client.build_option_order(sell_call_buy_put_legs(), direction="debit")


def test_normalize_leg_retains_the_contract_identifying_fields():
    """MUTATION TEST: the leg fields (option_type / underlying / strike / expiry)
    must survive normalization -- dropping them is what blinded the old coverage
    check. A long leg carrying them keeps them in the built payload."""
    client = make_client(FakeConnector())
    leg = {
        "side": "buy",
        "position_effect": "open",
        "ratio_quantity": 1,
        "option": LONG_CALL,
        "option_type": "call",
        "underlying": "AAPL",
        "strike_price": "190",
        "expiration_date": "2026-09-18",
    }

    payload = client.build_option_order([leg], direction="debit")

    built = payload["legs"][0]
    # The contract reference is re-keyed to the schema's option_id; the alias is gone.
    assert built["option_id"] == LONG_CALL
    assert "option" not in built
    assert built["option_type"] == "call"
    assert built["underlying"] == "AAPL"
    assert built["strike_price"] == "190"
    assert built["expiration_date"] == "2026-09-18"


# --- the shared coverage-aware validator (the guard), unit-tested directly -----


def test_shared_validator_refuses_the_ratio_and_the_mismatched_cover():
    """The ONE shared validator refuses ANY opening sell. Both undefined-risk
    shapes the old presence-only check passed -- the 10:1 ratio and the
    call-'covered'-by-a-put -- raise here."""
    from src.robinhood_option_client import assert_defined_risk

    with pytest.raises(DefinedRiskViolationError):
        assert_defined_risk(ratio_legs(), "debit")
    with pytest.raises(DefinedRiskViolationError):
        assert_defined_risk(sell_call_buy_put_legs(), "debit")


def test_shared_validator_allows_a_long_and_a_sell_to_close():
    """Precision: a lone long and a sell-to-CLOSE (exiting a held long) are not
    opening shorts and must pass, or the lane could not open or close a long."""
    from src.robinhood_option_client import assert_defined_risk

    assert_defined_risk(long_legs(), "debit")
    assert_defined_risk(
        [{"side": "sell", "position_effect": "close", "ratio_quantity": 1, "option": LONG_CALL}], "credit"
    )


def test_build_refuses_a_single_leg_sell_to_open_naked_short():
    """MUTATION TEST: a lone sell-to-open leg is a naked short. Removing the
    defined-risk check lets it build; the guard must raise instead."""
    client = make_client(FakeConnector())

    with pytest.raises(DefinedRiskViolationError):
        client.build_option_order(
            [{"side": "sell", "position_effect": "open", "ratio_quantity": 1, "option": SHORT_CALL}],
            direction="credit",
        )


def test_build_refuses_a_credit_opening_order_with_no_long_leg():
    """A credit opening order collecting premium with no covering buy-to-open leg
    is undefined risk and is refused."""
    client = make_client(FakeConnector())

    with pytest.raises(DefinedRiskViolationError):
        client.build_option_order(
            [{"side": "sell", "position_effect": "open", "ratio_quantity": 2, "option": SHORT_CALL}],
            direction="credit",
        )


def test_build_refuses_an_empty_leg_set():
    client = make_client(FakeConnector())

    with pytest.raises(DefinedRiskViolationError):
        client.build_option_order([], direction="debit")


def test_build_refuses_a_non_agentic_account():
    client = make_client(FakeConnector())

    with pytest.raises(AgentAccountMismatchError):
        client.build_option_order(long_legs(), account_number=DEFAULT_ACCOUNT["account_number"])


# --- review (non-committal) ---------------------------------------------------


def test_review_order_reaches_the_connector_without_placing():
    connector = FakeConnector()
    client = make_client(connector)

    result = client.review_order(long_legs())

    assert result["reviewed"] is True
    assert connector.review_calls[0]["account_number"] == AGENT_ACCOUNT["account_number"]
    assert connector.place_calls == []


def test_review_order_still_refuses_a_naked_short():
    connector = FakeConnector()
    client = make_client(connector)

    with pytest.raises(DefinedRiskViolationError):
        client.review_order(
            [{"side": "sell", "position_effect": "open", "ratio_quantity": 1, "option": SHORT_CALL}],
            direction="credit",
        )
    assert connector.review_calls == []


# --- place_option_order account gate ------------------------------------------


def test_place_refuses_the_default_account():
    connector = FakeConnector()
    client = make_client(connector)

    with pytest.raises(AgentAccountMismatchError):
        client.place_option_order(
            long_legs(),
            account_number=DEFAULT_ACCOUNT["account_number"],
            dry_run=False,
            confirm_live_order=True,
        )
    assert connector.place_calls == []


def test_place_targets_the_pinned_account_by_default():
    connector = FakeConnector()
    client = make_client(connector)

    result = client.place_option_order(long_legs())

    assert result["order_payload"]["account_number"] == AGENT_ACCOUNT["account_number"]


# --- place double gate + arm --------------------------------------------------


def test_place_defaults_to_dry_run_and_returns_a_payload():
    connector = FakeConnector()
    client = make_client(connector)

    result = client.place_option_order(long_legs())

    assert result["submitted"] is False
    assert result["status"] == "dry_run_order_preview"
    assert result["order_payload"]["legs"][0]["option_id"] == LONG_CALL
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
    client = make_client(connector, armed=True)

    result = client.place_option_order(
        long_legs(), dry_run=dry_run, confirm_live_order=confirm_live_order
    )

    assert result["submitted"] is False, case
    assert connector.place_calls == [], case


def test_place_submits_only_when_dry_run_false_and_confirm_true_and_armed():
    connector = FakeConnector()
    client = make_client(connector, armed=True)

    result = client.place_option_order(
        long_legs(), price="1.00", days_to_expiry=30, dry_run=False, confirm_live_order=True
    )

    assert result["submitted"] is True
    assert result["status"] == "submitted"
    assert len(connector.place_calls) == 1
    assert connector.place_calls[0]["account_number"] == AGENT_ACCOUNT["account_number"]


def test_place_emits_option_id_leg_to_the_connector():
    """MUTATION TEST: a fully gated live submit hands the connector a leg keyed
    `option_id` (the real Robinhood options schema), never the legacy `option`.
    Revert the normalizer to emit `option` and the strict connector stub raises
    before recording the call."""
    connector = FakeConnector()
    client = make_client(connector, armed=True)

    result = client.place_option_order(
        long_legs(), price="1.00", days_to_expiry=30, dry_run=False, confirm_live_order=True
    )

    assert result["submitted"] is True
    placed_leg = connector.place_calls[0]["legs"][0]
    assert placed_leg["option_id"] == LONG_CALL
    assert "option" not in placed_leg


def test_place_refuses_to_submit_when_the_options_lane_is_disarmed():
    """MUTATION TEST: full confirm gate, but the OPTIONS lane is DISARMED. The
    connector must NOT be called -- a real order requires BOTH confirm AND armed.
    Dropping the arm check lets it submit and fails here (call-count)."""
    connector = FakeConnector()
    client = make_client(connector, armed=False)

    result = client.place_option_order(long_legs(), dry_run=False, confirm_live_order=True)

    assert result["submitted"] is False
    assert result["status"] == "options_lane_disarmed"
    assert connector.place_calls == []


def test_place_refuses_to_submit_when_no_arm_store_is_wired():
    """A missing arm store reads as DISARMED (fail safe): no live submit."""
    connector = FakeConnector()
    client = RobinhoodOptionClient(connector, arm_store=None, expected_account=EXPECTED_ACCOUNT)

    result = client.place_option_order(long_legs(), dry_run=False, confirm_live_order=True)

    assert result["submitted"] is False
    assert connector.place_calls == []


class _ExplodingArmStore:
    """An arm store whose is_armed RAISES -- an unreadable store at the
    irreversible moment (a real backend erroring under the connector call)."""

    def is_armed(self, lane: str) -> bool:
        raise RuntimeError("arm store unreadable")


def test_place_refuses_to_submit_when_the_arm_store_raises():
    """MUTATION TEST: a wired arm store whose is_armed() RAISES reads as DISARMED
    (fail safe) -- the connector is never reached. Remove the `except Exception:
    return False` guard in _is_options_lane_armed and this exception propagates
    out of place_option_order instead of returning an unsubmitted preview; the
    pytest.raises below then fails because no exception escapes."""
    connector = FakeConnector()
    client = RobinhoodOptionClient(
        connector, arm_store=_ExplodingArmStore(), expected_account=EXPECTED_ACCOUNT
    )

    # With the guard in place: no exception escapes, nothing is submitted.
    result = client.place_option_order(
        long_legs(), price="1.00", days_to_expiry=30, dry_run=False, confirm_live_order=True
    )
    assert result["submitted"] is False
    assert result["status"] == "options_lane_disarmed"
    assert connector.place_calls == []

    # And the guard itself swallows the raise (proves the branch is exercised).
    assert client._is_options_lane_armed() is False


def test_client_rejects_a_non_options_arm_lane():
    """MUTATION TEST: the leveraged options client refuses any arm lane but
    'options'. crypto/equities are arm-tracked by ABSENCE of a stop file (ARMED by
    default), so consulting one here would silently fail OPEN. Drop the lane guard
    in __init__ and this construction succeeds instead of raising."""
    connector = FakeConnector()
    for bad_lane in ("equities", "crypto", "bogus", ""):
        with pytest.raises(ValueError, match="fail-closed"):
            RobinhoodOptionClient(
                connector,
                arm_store=FakeArmStore(armed=False),
                lane=bad_lane,
                expected_account=EXPECTED_ACCOUNT,
            )


def test_truthy_nonboolean_flags_do_not_arm_the_lane():
    """MUTATION TEST: the gate is identity, not truthiness. dry_run=0 (falsy) and
    confirm_live_order='yes' (truthy) must NOT submit. An `if dry_run or not
    confirm` guard would slip a live order through; `is False`/`is True` refuses."""
    connector = FakeConnector()
    client = make_client(connector, armed=True)

    result = client.place_option_order(long_legs(), dry_run=0, confirm_live_order="yes")

    assert result["submitted"] is False
    assert result["status"] == "dry_run_order_preview"
    assert connector.place_calls == []


def test_inverted_confirm_caller_still_cannot_submit():
    """A caller with an inverted-if bug can't trick the client -- the gate is
    enforced inside place_option_order, not by trusting caller polarity."""
    connector = FakeConnector()
    client = make_client(connector, armed=True)

    def buggy_caller(confirm: bool):
        if not confirm:
            return client.place_option_order(
                long_legs(), dry_run=False, confirm_live_order=confirm
            )
        return None

    result = buggy_caller(confirm=False)

    assert result["submitted"] is False
    assert connector.place_calls == []


def test_place_still_refuses_a_naked_short_even_fully_gated():
    """Defined-risk is validated at BUILD time, before the gate: a naked short is
    refused even with dry_run=False, confirm=True and the lane armed."""
    connector = FakeConnector()
    client = make_client(connector, armed=True)

    with pytest.raises(DefinedRiskViolationError):
        client.place_option_order(
            [{"side": "sell", "position_effect": "open", "ratio_quantity": 1, "option": SHORT_CALL}],
            direction="credit",
            dry_run=False,
            confirm_live_order=True,
        )
    assert connector.place_calls == []


def test_place_refuses_a_ten_to_one_ratio_even_fully_gated():
    """The audit's 10:1 ratio: a long leg plus nine uncovered shorts. Fully gated
    (dry_run False, confirm True, armed) and it STILL never reaches the connector
    -- the coverage-aware validator refuses the opening sell before any gate.
    Revert to the presence-only check and place_calls is non-empty."""
    connector = FakeConnector()
    client = make_client(connector, armed=True)

    with pytest.raises(DefinedRiskViolationError):
        client.place_option_order(
            ratio_legs(), direction="debit", dry_run=False, confirm_live_order=True
        )
    assert connector.place_calls == []


def test_place_refuses_a_short_call_covered_by_a_long_put_even_fully_gated():
    """The audit's call-'covered'-by-a-put: a buy leg is present, so the old
    check passed it to the connector. Fully gated, it must never place."""
    connector = FakeConnector()
    client = make_client(connector, armed=True)

    with pytest.raises(DefinedRiskViolationError):
        client.place_option_order(
            sell_call_buy_put_legs(), direction="debit", dry_run=False, confirm_live_order=True
        )
    assert connector.place_calls == []


# --- options risk caps mirrored in the client (defense in depth) --------------
# The place path runs the option risk caps (option_risk_gates) before the
# connector call, mirroring the broker. A fully gated + armed order that violates
# a cap returns status "options_risk_gate_blocked" and never places. Caps are
# read from the repo config (max_debit $500, max_contracts 5, 0DTE blocked). Each
# FAILS if the client's caps mirror is reverted (the connector would be hit).


def test_place_within_caps_submits():
    connector = FakeConnector()
    client = make_client(connector, armed=True)

    result = client.place_option_order(
        long_legs(), quantity="2", price="2.50", days_to_expiry=30, dry_run=False, confirm_live_order=True
    )

    assert result["submitted"] is True
    assert len(connector.place_calls) == 1


def test_place_refuses_a_zero_dte_order_even_fully_gated():
    connector = FakeConnector()
    client = make_client(connector, armed=True)

    result = client.place_option_order(
        long_legs(), quantity="1", price="1.00", days_to_expiry=0, dry_run=False, confirm_live_order=True
    )

    assert result["submitted"] is False
    assert result["status"] == "options_risk_gate_blocked"
    assert "zero_dte" in result["risk_gate"]
    assert connector.place_calls == []


def test_place_refuses_an_over_contract_order_even_fully_gated():
    connector = FakeConnector()
    client = make_client(connector, armed=True)

    result = client.place_option_order(
        long_legs(), quantity="6", price="0.50", days_to_expiry=30, dry_run=False, confirm_live_order=True
    )

    assert result["submitted"] is False
    assert result["status"] == "options_risk_gate_blocked"
    assert "max_contracts_per_order" in result["risk_gate"]
    assert connector.place_calls == []


def test_place_refuses_an_over_debit_order_even_fully_gated():
    connector = FakeConnector()
    client = make_client(connector, armed=True)

    result = client.place_option_order(
        long_legs(), quantity="1", price="6.00", days_to_expiry=30, dry_run=False, confirm_live_order=True
    )

    assert result["submitted"] is False
    assert result["status"] == "options_risk_gate_blocked"
    assert "max_debit_premium_per_trade" in result["risk_gate"]
    assert connector.place_calls == []


def test_client_caps_block_method_flags_each_cap():
    """Pin the client's OWN caps method directly, so reverting the client mirror
    fails here independently of the broker."""
    connector = FakeConnector()
    client = make_client(connector, armed=True)
    zero_dte = client._option_caps_block(long_legs(), "debit", "1", "1.00", 0, 0.0, None)
    over_ct = client._option_caps_block(long_legs(), "debit", "6", "0.50", 30, 0.0, None)
    over_debit = client._option_caps_block(long_legs(), "debit", "1", "6.00", 30, 0.0, None)
    within = client._option_caps_block(long_legs(), "debit", "2", "2.50", 30, 0.0, None)
    assert "zero_dte" in (zero_dte or "")
    assert "max_contracts_per_order" in (over_ct or "")
    assert "max_debit_premium_per_trade" in (over_debit or "")
    assert within is None


# --- submit-hardening: a None / <= 0 / non-numeric price is refused -----------
# A live limit order whose price is None / non-numeric / <= 0 makes BOTH dollar
# caps read $0 of new risk (max(premium, 0) = 0), so neither the debit cap nor
# the total-at-risk cap can bind. Such an order must be refused at the submit
# path, fully gated + armed, and never reach the connector. Each test FAILS if
# the price guard is reverted: the order would then submit on a void cap.


@pytest.mark.parametrize("bad_price", [None, "0", "0.00", "-1.00", "abc", "nan", "inf"])
def test_place_refuses_a_none_or_nonpositive_price_even_fully_gated(bad_price):
    """MUTATION TEST: fully gated + armed, but the limit price is None / <= 0 /
    non-numeric. The order returns status 'invalid_limit_price' and the connector
    is never called. Revert the price guard and a $0-priced order submits with
    both dollar caps voided."""
    connector = FakeConnector()
    client = make_client(connector, armed=True)

    result = client.place_option_order(
        long_legs(), price=bad_price, days_to_expiry=30, dry_run=False, confirm_live_order=True
    )

    assert result["submitted"] is False
    assert result["status"] == "invalid_limit_price"
    assert connector.place_calls == []


def test_positive_limit_price_predicate_pins_the_boundary():
    """The predicate the submit path checks: only a positive, finite number is a
    valid limit price. Pinned directly so neutering it fails here regardless of
    the submit path."""
    from src.robinhood_option_client import _positive_limit_price

    assert _positive_limit_price("2.50") == 2.50
    assert _positive_limit_price(1) == 1.0
    for bad in (None, "0", 0, "0.00", "-0.01", -5, "abc", "", float("nan"), float("inf")):
        assert _positive_limit_price(bad) is None, bad


# --- submit-hardening: the connector leg carries ONLY the schema keys ----------
# The real Robinhood options MCP order schema is additionalProperties:false --
# a leg may carry ONLY option_id / side / position_effect / ratio_quantity. The
# build path retains extra contract-identifying fields (option_type / underlying
# / strike / expiration) for the caps/DTE math, but the leg handed to the
# connector must be stripped to exactly the allowed set, or the venue rejects it.


def test_place_strips_schema_forbidden_leg_keys_at_the_connector():
    """MUTATION TEST: a fully gated live submit whose leg carries identifying
    fields (option_type / underlying / strike / expiration) hands the connector a
    leg with EXACTLY {option_id, side, position_effect, ratio_quantity} and no
    other key. Revert the wire projection and the connector sees schema-forbidden
    keys (additionalProperties:false) and would reject the order at the venue."""
    connector = FakeConnector()
    client = make_client(connector, armed=True)
    rich_leg = {
        "side": "buy",
        "position_effect": "open",
        "ratio_quantity": 1,
        "option": LONG_CALL,
        "option_type": "call",
        "underlying": "AAPL",
        "strike_price": "190",
        "expiration_date": "2026-09-18",
    }

    result = client.place_option_order(
        [rich_leg], price="1.00", days_to_expiry=30, dry_run=False, confirm_live_order=True
    )

    assert result["submitted"] is True
    placed_leg = connector.place_calls[0]["legs"][0]
    assert set(placed_leg) == {"option_id", "side", "position_effect", "ratio_quantity"}
    assert placed_leg["option_id"] == LONG_CALL
    # The built payload the client returns still carries the identifying fields --
    # only the wire leg handed to the connector is stripped.
    assert result["order_payload"]["legs"][0]["option_type"] == "call"


def test_review_also_strips_schema_forbidden_leg_keys():
    """Review is connector-bound too (same additionalProperties:false schema), so
    its leg is projected to the allowed key set as well."""
    connector = FakeConnector()
    client = make_client(connector)
    rich_leg = {
        "side": "buy",
        "position_effect": "open",
        "ratio_quantity": 1,
        "option": LONG_CALL,
        "option_type": "call",
        "underlying": "AAPL",
    }

    client.review_order([rich_leg])

    reviewed_leg = connector.review_calls[0]["legs"][0]
    assert set(reviewed_leg) == {"option_id", "side", "position_effect", "ratio_quantity"}


# --- direction is a lane-internal hint, omitted on the single-leg wire payload -
# The connector's single-leg order schema does not read `direction` (debit/credit)
# -- it is a lane-internal hint the caps/DTE math use. It must not be sent to the
# connector for a single-leg order, though the BUILT payload keeps it.


def test_place_omits_direction_on_the_single_leg_wire_payload():
    """MUTATION TEST: a fully gated single-leg live submit hands the connector a
    payload with NO `direction` key. Drop the wire projection's direction pop and
    the connector sees a lane-internal `direction` field it does not accept."""
    connector = FakeConnector()
    client = make_client(connector, armed=True)

    result = client.place_option_order(
        long_legs(), price="1.00", days_to_expiry=30, dry_run=False, confirm_live_order=True
    )

    assert result["submitted"] is True
    assert "direction" not in connector.place_calls[0]
    # The BUILT payload the client returns still carries it (the caps/DTE math read it).
    assert result["order_payload"]["direction"] == "debit"


def test_review_omits_direction_on_the_single_leg_wire_payload():
    """Review is connector-bound too, so its single-leg payload also omits the
    lane-internal `direction` hint."""
    connector = FakeConnector()
    client = make_client(connector)

    client.review_order(long_legs(), direction="debit")

    assert "direction" not in connector.review_calls[0]


# --- ref_id is a stable idempotency key so a retry cannot double-submit --------
# Every built payload carries a `ref_id`; a retried submit of the SAME order
# reuses the SAME key (the venue de-dupes it), and distinct orders differ.


def test_build_stamps_a_ref_id_that_is_stable_across_identical_rebuilds():
    """MUTATION TEST: two builds of the SAME order carry the SAME ref_id, so a
    retry de-dupes at the venue. Make the key non-deterministic (e.g. uuid4) and
    the two rebuilds diverge and a retry double-submits."""
    client = make_client(FakeConnector())

    first = client.build_option_order(long_legs(), direction="debit", quantity="1", price="1.00")
    second = client.build_option_order(long_legs(), direction="debit", quantity="1", price="1.00")

    assert first["ref_id"]  # present and non-empty
    assert first["ref_id"] == second["ref_id"]


def test_ref_id_differs_when_the_order_content_differs():
    """A different order (here a different quantity) must mint a DIFFERENT key, or
    two genuinely distinct orders would collide and the second be dropped."""
    client = make_client(FakeConnector())

    one = client.build_option_order(long_legs(), direction="debit", quantity="1", price="1.00")
    two = client.build_option_order(long_legs(), direction="debit", quantity="2", price="1.00")

    assert one["ref_id"] != two["ref_id"]


def test_place_sends_the_ref_id_to_the_connector():
    """MUTATION TEST: the idempotency key reaches the connector on a live submit.
    Drop ref_id from the payload and the venue cannot de-dupe a retry."""
    connector = FakeConnector()
    client = make_client(connector, armed=True)

    result = client.place_option_order(
        long_legs(), price="1.00", days_to_expiry=30, dry_run=False, confirm_live_order=True
    )

    assert result["submitted"] is True
    sent_ref = connector.place_calls[0].get("ref_id")
    assert sent_ref
    # A retry of the identical order carries the identical key.
    assert sent_ref == result["order_payload"]["ref_id"]


def test_explicit_ref_id_overrides_the_derived_key():
    """A caller may supply its own stable client-order-id; it wins over the
    derived key and is what reaches the connector."""
    connector = FakeConnector()
    client = make_client(connector, armed=True)

    result = client.place_option_order(
        long_legs(), price="1.00", days_to_expiry=30, dry_run=False, confirm_live_order=True,
        ref_id="operator-supplied-key-123",
    )

    assert result["submitted"] is True
    assert connector.place_calls[0]["ref_id"] == "operator-supplied-key-123"


# --- cancel mirrors the same dry_run/confirm gate (no arm required) -----------


def test_cancel_defaults_to_dry_run_and_does_not_submit():
    connector = FakeConnector()
    client = make_client(connector)

    result = client.cancel_order("opt-order-1")

    assert result["status"] == "dry_run_cancel_prepared"
    assert connector.cancel_calls == []


def test_cancel_refuses_the_default_account():
    connector = FakeConnector()
    client = make_client(connector)

    with pytest.raises(AgentAccountMismatchError):
        client.cancel_order(
            "opt-order-1",
            account_number=DEFAULT_ACCOUNT["account_number"],
            dry_run=False,
            confirm_live_order=True,
        )
    assert connector.cancel_calls == []


def test_cancel_submits_only_when_dry_run_false_and_confirm_true():
    """Cancel REDUCES exposure, so it is not held to the arm fact -- a disarmed
    client can still cancel to flatten. It still needs the confirm gate."""
    connector = FakeConnector()
    client = make_client(connector, armed=False)

    result = client.cancel_order("opt-order-1", dry_run=False, confirm_live_order=True)

    assert result["status"] == "cancel_requested"
    assert connector.cancel_calls == [
        {"order_id": "opt-order-1", "account_number": AGENT_ACCOUNT["account_number"]}
    ]


# --- caps-at-submit: the DEBIT cap keys off leg polarity, not the caller's label
# debit_premium_usd returns $0 for a 'credit' order, so trusting the caller's
# direction label would let a long (buy-to-open) mislabeled 'credit' escape the
# per-trade debit cap and submit at up to the looser total-at-risk cap. At the
# submit path defined risk has already refused every opening short, so any buy
# leg reaching the caps is a real debit and must be capped as one.


def test_place_refuses_a_long_mislabeled_credit_over_the_debit_cap():
    """MUTATION TEST: a single buy-to-open leg at $6.00 (= $600 debit, over the
    $500 per-trade cap) labeled direction='credit', fully gated + armed. It must
    be blocked with status 'options_risk_gate_blocked' naming the debit cap, and
    the connector never called. Revert caps_direction (trust the 'credit' label)
    and debit_premium_usd reads $0, the debit cap is voided, and the $600 long
    submits."""
    connector = FakeConnector()
    client = make_client(connector, armed=True)

    result = client.place_option_order(
        long_legs(), direction="credit", quantity="1", price="6.00", days_to_expiry=30,
        dry_run=False, confirm_live_order=True,
    )

    assert result["submitted"] is False
    assert result["status"] == "options_risk_gate_blocked"
    assert "max_debit_premium_per_trade" in result["risk_gate"]
    assert connector.place_calls == []


def test_caps_direction_forces_debit_for_a_buy_leg_but_spares_a_sell_to_close():
    """Pin caps_direction directly: any BUY leg forces 'debit' (the debit cap must
    see the real premium); an all-sell reducing order keeps 'credit' (a close
    genuinely collects, so the debit cap stays off). Revert it to return the
    caller's label and the buy leg keeps 'credit', voiding the debit cap."""
    from src.option_risk_gates import caps_direction

    assert caps_direction(long_legs(), "credit") == "debit"
    sell_to_close = [{"side": "sell", "position_effect": "close", "ratio_quantity": 1, "option": LONG_CALL}]
    assert caps_direction(sell_to_close, "credit") == "credit"


def test_client_caps_spare_a_genuine_sell_to_close_credit_from_the_debit_cap():
    """Precision: the polarity correction must NOT over-block a real close. A
    sell-to-close at $6.00 collects premium, so the debit cap must stay off and
    the caps pass (None)."""
    connector = FakeConnector()
    client = make_client(connector, armed=True)
    sell_to_close = [{"side": "sell", "position_effect": "close", "ratio_quantity": 1, "option": LONG_CALL}]

    assert client._option_caps_block(sell_to_close, "credit", "1", "6.00", 30, 0.0, None) is None
