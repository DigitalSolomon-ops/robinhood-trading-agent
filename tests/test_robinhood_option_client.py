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


# The EXACT top-level key set the real review_option_order schema accepts
# (additionalProperties:false). Notably it carries NO `ref_id` -- the idempotency
# key is a place-only field -- so a review payload carrying ref_id would
# InputValidationError at the venue. A permissive stub that swallowed ref_id let
# that regression hide; this set makes the review wire's key projection
# load-bearing.
_REVIEW_ALLOWED_KEYS = frozenset(
    {
        "account_number",
        "chain_symbol",
        "direction",
        "legs",
        "market_hours",
        "price",
        "quantity",
        "stop_price",
        "time_in_force",
        "type",
        "underlying_type",
    }
)


def _assert_review_schema_keys(kwargs):
    """The real review_option_order schema is additionalProperties:false and takes
    no ref_id. Reject any key outside the allowed set (chiefly a place-only ref_id)
    so a review wire that ships ref_id fails here exactly as it would at the venue."""
    assert "ref_id" not in kwargs, f"review payload carries place-only 'ref_id': {sorted(kwargs)}"
    extra = set(kwargs) - _REVIEW_ALLOWED_KEYS
    assert not extra, f"review payload carries schema-forbidden keys {sorted(extra)} (additionalProperties:false)"


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


class FakeKillSwitch:
    """A test double for the options kill switch: HALTED exactly when it is given
    halt reasons, OPEN otherwise. The real KillSwitch reads STOP_TRADING_OPTIONS +
    TRADING_ENABLED; injecting this keeps the client tests off the real filesystem
    and process env while still exercising the client's irreversible-moment
    re-check via halt_reasons()."""

    def __init__(self, halts: list[str] | None = None) -> None:
        self._halts = list(halts or [])
        self.queried = 0

    def halt_reasons(self) -> list[str]:
        self.queried += 1
        return list(self._halts)


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
        _assert_review_schema_keys(kwargs)
        self.review_calls.append(kwargs)
        return {"reviewed": True, **kwargs}

    def place_option_order(self, **kwargs):
        _assert_connector_leg_shape(kwargs.get("legs"))
        self.place_calls.append(kwargs)
        return {"order_id": "opt-order-1", "status": "accepted"}

    def cancel_option_order(self, order_id, account_number=None):
        self.cancel_calls.append({"order_id": order_id, "account_number": account_number})
        return {"order_id": order_id, "status": "cancel_requested"}


def make_client(
    connector: FakeConnector, armed: bool = True, kill_switch: object | None = None
) -> RobinhoodOptionClient:
    # Inject an OPEN kill switch by default so a fully-gated + armed order can
    # submit under test without depending on the real STOP_TRADING_OPTIONS file or
    # a process-wide TRADING_ENABLED (the broker tests achieve the same by anchoring
    # the stop file to tmp_path and setting the env). A test that wants to exercise
    # the halt path passes a FakeKillSwitch with halt reasons.
    return RobinhoodOptionClient(
        connector,
        arm_store=FakeArmStore(armed=armed),
        expected_account=EXPECTED_ACCOUNT,
        kill_switch=kill_switch if kill_switch is not None else FakeKillSwitch(),
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


# --- ref_id is PLACE-only: the review wire must not ship it --------------------
# review_option_order is additionalProperties:false and takes no ref_id, while the
# built payload (and the place wire) carry one. The shared _wire_payload builds
# both, so the review wire must strip ref_id (and any other place-only key) or a
# real review call InputValidationErrors at the venue.


def test_review_wire_payload_omits_ref_id_and_carries_only_review_schema_keys():
    """MUTATION TEST: a review call hands the connector a payload with NO ref_id
    and NO other key outside the review schema's allowed set. Drop the for_review
    strip (ship the place wire to review) and ref_id rides along -- the FakeConnector's
    review-schema guard fires exactly as the real additionalProperties:false venue would."""
    connector = FakeConnector()
    client = make_client(connector)

    client.review_order(long_legs(), direction="debit", quantity="1", price="1.00")

    reviewed = connector.review_calls[0]
    assert "ref_id" not in reviewed
    # A single-leg long: direction is a lane hint dropped at the wire too, so the
    # review payload is exactly account_number / legs / quantity / type /
    # time_in_force / price -- every one an allowed review-schema key.
    assert set(reviewed) == {"account_number", "legs", "quantity", "type", "time_in_force", "price"}
    assert set(reviewed) <= _REVIEW_ALLOWED_KEYS


def test_place_wire_still_carries_ref_id_while_review_wire_does_not():
    """Precision: the place-only strip is REVIEW-only. The very same built order,
    sent to place, keeps its ref_id (so a retry de-dupes at the venue); sent to
    review, drops it. Proves _wire_payload's for_review branch, not a blanket removal."""
    connector = FakeConnector()
    client = make_client(connector, armed=True)

    built = client.build_option_order(long_legs(), direction="debit", quantity="1", price="1.00")
    place_wire = client._wire_payload(built)
    review_wire = client._wire_payload(built, for_review=True)

    assert place_wire["ref_id"] == built["ref_id"]
    assert "ref_id" not in review_wire


def test_review_wire_keeps_direction_but_omits_ref_id_on_a_multi_leg_order():
    """A multi-leg order retains `direction` on the wire (a spread's net direction
    is meaningful and the review schema accepts it), yet still drops the place-only
    ref_id. Guards against a strip that keys off leg count instead of place-vs-review."""
    connector = FakeConnector()
    client = make_client(connector)
    # A long multi-leg (two buy-to-open legs) -- defined risk, so it clears the build.
    multi = [
        {"side": "buy", "position_effect": "open", "ratio_quantity": 1, "option": LONG_CALL},
        {"side": "buy", "position_effect": "open", "ratio_quantity": 1, "option": SHORT_CALL},
    ]

    client.review_order(multi, direction="debit", quantity="1", price="2.00")

    reviewed = connector.review_calls[0]
    assert reviewed["direction"] == "debit"
    assert "ref_id" not in reviewed
    assert set(reviewed) <= _REVIEW_ALLOWED_KEYS


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


# --- client-irreversible-parity: mirror the broker's irreversible-moment gates
# A DIRECT client.place_option_order (the public, connector-calling submit) must
# enforce the SAME irreversible-moment gates the broker does, so a direct client
# submit is exactly as safe as one routed through the broker. Each test below is
# a MUTATION TEST: it FAILS if the mirrored guard is reverted.


class PositionsConnector(FakeConnector):
    """A connector that reports fixed OPEN option positions, so the client's own
    _open_premium_at_risk_from_positions has real standing exposure to source."""

    def __init__(self, positions: list[dict], **kwargs) -> None:
        super().__init__(**kwargs)
        self._positions = positions

    def get_option_positions(self, account_number=None):
        self.position_calls.append({"account_number": account_number})
        return {"positions": self._positions}


class NonAcceptingConnector(FakeConnector):
    """A connector whose place_option_order does NOT raise but returns a response
    that carries no acceptance signal -- the phantom-fill trap the client's
    response inspection closes."""

    def __init__(self, response, **kwargs) -> None:
        super().__init__(**kwargs)
        self._response = response

    def place_option_order(self, **kwargs):
        _assert_connector_leg_shape(kwargs.get("legs"))
        self.place_calls.append(kwargs)
        return self._response


# (1) KILL SWITCH -------------------------------------------------------------


def test_place_refuses_to_submit_when_the_options_kill_switch_is_halted():
    """MUTATION TEST (item 1): fully gated + armed + priced + within caps, but the
    options kill switch is HALTED. The client must consult it at the irreversible
    moment and refuse -- status 'kill_switch_engaged', the connector never called.
    Revert the kill-switch re-check in place_option_order and this order submits."""
    connector = FakeConnector()
    client = make_client(
        connector, armed=True, kill_switch=FakeKillSwitch(["STOP_TRADING_OPTIONS exists"])
    )

    result = client.place_option_order(
        long_legs(), quantity="1", price="1.00", days_to_expiry=30,
        dry_run=False, confirm_live_order=True,
    )

    assert result["submitted"] is False
    assert result["status"] == "kill_switch_engaged"
    assert "STOP_TRADING_OPTIONS" in result["kill_switch"]
    assert connector.place_calls == []


def test_place_fails_closed_when_the_kill_switch_is_unreadable():
    """An emergency stop that RAISES on read must read as HALTED (fail closed), so
    a broken switch can never leave the client's submit path unguarded."""

    class RaisingKillSwitch:
        def halt_reasons(self):
            raise RuntimeError("switch unreadable")

    connector = FakeConnector()
    client = make_client(connector, armed=True, kill_switch=RaisingKillSwitch())

    result = client.place_option_order(
        long_legs(), quantity="1", price="1.00", days_to_expiry=30,
        dry_run=False, confirm_live_order=True,
    )

    assert result["submitted"] is False
    assert result["status"] == "kill_switch_engaged"
    assert connector.place_calls == []


# (2) DTE FAIL-CLOSED ---------------------------------------------------------


def test_place_refuses_an_unknown_dte_live_long_even_fully_gated():
    """MUTATION TEST (item 2): fully gated + armed + priced, a single long call
    with NO explicit days_to_expiry and NO leg-stamped expiry. The client must
    FAIL CLOSED -- status 'options_risk_gate_blocked' naming MIN_DTE -- and never
    reach the connector. Revert the fail-closed DTE handling (strip the DTE gates
    when the expiry is unknown) and a known 0DTE / short-dated long slips through
    the manual place/build helpers by omitting the DTE, submitting here."""
    connector = FakeConnector()
    client = make_client(connector, armed=True)

    result = client.place_option_order(
        long_legs(), quantity="1", price="1.00",  # no days_to_expiry, no leg expiry
        dry_run=False, confirm_live_order=True,
    )

    assert result["submitted"] is False
    assert result["status"] == "options_risk_gate_blocked"
    assert "min_days_to_expiry" in result["risk_gate"]
    assert connector.place_calls == []


def test_place_binds_dte_from_a_leg_stamped_expiry_and_submits():
    """Precision: the fail-closed refusal is only for a GENUINELY unknown expiry.
    A leg stamping a far-dated expiration derives a known DTE, so a fully gated +
    armed order submits with no explicit days_to_expiry -- the refusal must not
    over-block an order that names its expiry on the leg."""
    from datetime import UTC, datetime, timedelta

    far = (datetime.now(UTC).date() + timedelta(days=30)).isoformat()
    connector = FakeConnector()
    client = make_client(connector, armed=True)
    stamped_leg = {**long_legs()[0], "expiration_date": far}

    result = client.place_option_order(
        [stamped_leg], quantity="1", price="1.00",  # no explicit days_to_expiry
        dry_run=False, confirm_live_order=True,
    )

    assert result["submitted"] is True
    assert len(connector.place_calls) == 1


# (3) STANDING AT-RISK from open positions ------------------------------------


def test_place_refuses_when_open_positions_push_total_at_risk_over_cap():
    """MUTATION TEST (item 3): the caller passes the 0.0 open-at-risk default, but
    the connector reports open positions worth $1,500 already at risk (3 x $5.00 x
    100). A new $200 long (1 x $2.00 x 100) makes $1,700 total, over the $1,500
    cap. The client must SELF-SOURCE the standing exposure and block -- status
    'options_risk_gate_blocked' naming the total-at-risk cap, connector never
    called. Revert the self-sourcing (default standing exposure to 0.0) and the
    order under-counts to $200 and submits."""
    connector = PositionsConnector(
        positions=[{"quantity": "3", "average_open_price": "5.00", "option_id": LONG_CALL}]
    )
    client = make_client(connector, armed=True)

    result = client.place_option_order(
        long_legs(), quantity="1", price="2.00", days_to_expiry=30,
        dry_run=False, confirm_live_order=True,  # open_premium_at_risk_usd defaults 0.0
    )

    assert result["submitted"] is False
    assert result["status"] == "options_risk_gate_blocked"
    assert "max_total_premium_at_risk" in result["risk_gate"]
    assert connector.place_calls == []


def test_client_open_exposure_from_positions_sums_premium_and_max_loss():
    """Pin the client's OWN sourcing method directly: a position's standing risk
    is its recorded max_loss when present, else premium x 100 x contracts; a
    closed (qty 0) position is ignored. Revert the method and this fails
    independently of the end-to-end cap test."""
    connector = PositionsConnector(
        positions=[
            {"quantity": "2", "average_open_price": "1.50"},  # 2 x 1.50 x 100 = 300
            {"quantity": "1", "max_loss_usd": "250"},          # recorded max loss = 250
            {"quantity": "0", "average_open_price": "9.99"},   # closed -> ignored
        ]
    )
    client = make_client(connector, armed=True)
    assert client._open_premium_at_risk_from_positions() == 550.0


# The REAL Robinhood get_option_positions response: a `results` list (alongside a
# `next` pagination field), each position carrying the venue's ACTUAL field names
# and string-formatted values -- not a tidy, permissive self-authored fake. The
# standing-exposure sourcing is proven against THIS shape so it cannot silently
# read $0 (and void the total-at-risk cap) against the payload it will really see.
_RECORDED_OPTION_POSITIONS = {
    "next": None,
    "results": [
        {  # an OPEN long: 4 contracts, $2.50 premium/share -> 2.50 x 100 x 4 = $1,000
            "account": "https://api.robinhood.com/accounts/1AB23456/",
            "average_price": "2.5000",
            "chain_id": "b1e2c3d4-5678-90ab-cdef-1234567890ab",
            "chain_symbol": "AAPL",
            "created_at": "2026-08-20T14:31:09.123456Z",
            "id": "pos-1",
            "option": "https://api.robinhood.com/options/instruments/uuid-1/",
            "option_id": "uuid-1",
            "pending_buy_quantity": "0.0000",
            "pending_sell_quantity": "0.0000",
            "quantity": "4.0000",
            "intraday_quantity": "0.0000",
            "intraday_average_open_price": "0.0000",
            "trade_value_multiplier": "100.0000",
            "type": "long",
            "updated_at": "2026-08-20T14:31:09.123456Z",
            "url": "https://api.robinhood.com/options/positions/pos-1/",
        },
        {  # a second OPEN long: 2 contracts, $2.00 premium/share -> 2.00 x 100 x 2 = $400
            "account": "https://api.robinhood.com/accounts/1AB23456/",
            "average_price": "2.0000",
            "chain_symbol": "MSFT",
            "option_id": "uuid-2",
            "quantity": "2.0000",
            "intraday_average_open_price": "0.0000",
            "trade_value_multiplier": "100.0000",
            "type": "long",
        },
        {  # CLOSED: the real API returns zero-quantity positions too -> ignored
            "average_price": "9.9900",
            "chain_symbol": "TSLA",
            "option_id": "uuid-3",
            "quantity": "0.0000",
            "trade_value_multiplier": "100.0000",
            "type": "long",
        },
    ],
}


class RecordedShapePositionsConnector(FakeConnector):
    """Returns the realistic recorded get_option_positions payload verbatim, so the
    sourcing is exercised against the venue's real `results` wrapper and field
    names -- not the permissive `{"positions": [...]}` shape the other fakes use."""

    def get_option_positions(self, account_number=None):
        self.position_calls.append({"account_number": account_number})
        return _RECORDED_OPTION_POSITIONS


def test_open_exposure_sourced_from_the_real_recorded_positions_shape():
    """MUTATION TEST (item 3): fed the REAL Robinhood response -- a `results` list
    of positions using the venue's actual field names (average_price / quantity /
    trade_value_multiplier / type) with string values and noise fields, plus a
    closed zero-quantity position -- the sourcing sums the two OPEN longs
    (2.50x100x4 + 2.00x100x2 = $1,400) under the lane's per-share premium
    convention and ignores the closed one. Grounds the sourcing on the shape it
    will really see: revert `_as_position_list` to only unwrap `positions` (not
    `results`), or the premium/quantity field lists to miss `average_price`/
    `quantity`, and this reads $0 -- silently voiding the total-at-risk cap on
    live positions."""
    connector = RecordedShapePositionsConnector()
    client = make_client(connector, armed=True)

    assert client._open_premium_at_risk_from_positions() == 1400.0


def test_recorded_shape_open_exposure_binds_the_total_at_risk_cap_end_to_end():
    """The real-shape standing exposure flows all the way into the submit-path cap:
    $1,400 already at risk (from the recorded positions) + a new $200 long
    (1 x $2.00 x 100, itself within the $500 per-trade debit cap) = $1,600, over
    the $1,500 total-at-risk cap. The client self-sources from the real shape and
    BLOCKS on the TOTAL cap -- connector never called -- even though the caller
    passes the 0.0 open-at-risk default."""
    connector = RecordedShapePositionsConnector()
    client = make_client(connector, armed=True)

    result = client.place_option_order(
        long_legs(), quantity="1", price="2.00", days_to_expiry=30,
        dry_run=False, confirm_live_order=True,  # open_premium_at_risk_usd defaults 0.0
    )

    assert result["submitted"] is False
    assert result["status"] == "options_risk_gate_blocked"
    assert "max_total_premium_at_risk" in result["risk_gate"]
    assert connector.place_calls == []


# (4) LONG AT-RISK FROM DEBIT -------------------------------------------------


def test_place_ignores_a_tiny_caller_max_loss_and_derives_at_risk_from_debit():
    """MUTATION TEST (item 4): a long (BUY leg) whose caller-supplied
    max_loss_per_contract_usd ($0.50) is far BELOW the real debit ($2.00 x 100 =
    $200). With $1,400 already at risk, the true total is $1,600 > the $1,500 cap,
    but the tiny caller figure would read only $1,400.50 and slip under. The client
    must derive at-risk from the debit and IGNORE the smaller caller figure --
    status 'options_risk_gate_blocked' naming the total-at-risk cap, connector
    never called. Revert _effective_max_loss_per_contract (trust the caller's
    max_loss) and the order under-counts and submits."""
    connector = FakeConnector()
    client = make_client(connector, armed=True)

    result = client.place_option_order(
        long_legs(), quantity="1", price="2.00", days_to_expiry=30,
        dry_run=False, confirm_live_order=True,
        open_premium_at_risk_usd=1400.0,
        max_loss_per_contract_usd=0.50,
    )

    assert result["submitted"] is False
    assert result["status"] == "options_risk_gate_blocked"
    assert "max_total_premium_at_risk" in result["risk_gate"]
    assert connector.place_calls == []


def test_effective_max_loss_pins_the_debit_floor_for_a_buy_leg():
    """Pin the helper directly: for any BUY leg the per-contract max loss is at
    least the debit paid (premium x multiplier); a lower or None caller figure is
    ignored, a HIGHER one (a real width-based max loss) wins, and an all-sell
    reducing order passes its caller figure through unchanged."""
    from src.robinhood_option_client import _effective_max_loss_per_contract

    assert _effective_max_loss_per_contract(long_legs(), 2.00, 100, 0.50) == 200.0
    assert _effective_max_loss_per_contract(long_legs(), 2.00, 100, None) == 200.0
    assert _effective_max_loss_per_contract(long_legs(), 2.00, 100, 500.0) == 500.0
    sell_to_close = [{"side": "sell", "position_effect": "close", "ratio_quantity": 1, "option": LONG_CALL}]
    assert _effective_max_loss_per_contract(sell_to_close, 2.00, 100, 0.50) == 0.50


# (5) RESPONSE INSPECTION -----------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [None, {}, {"foo": "bar"}, "accepted-looking-string", {"status": "rejected"}, {"status": "unconfirmed"}],
)
def test_place_reports_not_submitted_on_a_non_accepting_response(response):
    """MUTATION TEST (item 5): the connector call does NOT raise but returns a
    response with no acceptance signal (no order id, no accepting status -- or an
    explicit rejection). The client must inspect the response and report
    submitted=False, status 'order_not_accepted', carrying the raw response back --
    never claim a fill merely because the call returned. The connector WAS reached
    here (place_calls == 1), so the verdict is on the response, not on reaching the
    connector. Revert the response inspection (submitted=True whenever the call
    did not raise) and this phantom fill passes."""
    connector = NonAcceptingConnector(response=response)
    client = make_client(connector, armed=True)

    result = client.place_option_order(
        long_legs(), quantity="1", price="1.00", days_to_expiry=30,
        dry_run=False, confirm_live_order=True,
    )

    assert result["submitted"] is False
    assert result["status"] == "order_not_accepted"
    assert result["response"] == response
    assert len(connector.place_calls) == 1


def test_place_reports_submitted_on_an_id_only_acceptance():
    """Precision: an acceptance signal need not be a status word -- a venue-minted
    order id alone is acceptance. A response carrying only an id reports
    submitted=True, so the inspection does not over-block a real fill."""
    connector = NonAcceptingConnector(response={"id": "venue-minted-42"})
    client = make_client(connector, armed=True)

    result = client.place_option_order(
        long_legs(), quantity="1", price="1.00", days_to_expiry=30,
        dry_run=False, confirm_live_order=True,
    )

    assert result["submitted"] is True
    assert len(connector.place_calls) == 1
