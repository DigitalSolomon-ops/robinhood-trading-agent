"""The options broker runs on the SHARED risk machinery, adds the arm gate, and
cannot submit an ungated or undefined-risk order.

Every property is proved by connector call-count -- the only witness that says
nothing reached Robinhood. Each test is written to FAIL if the guard it exercises
is reverted:

  * disarmed lane blocks even with the full human confirm (revert the arm gate
    and the connector is hit);
  * STOP_TRADING_OPTIONS and TRADING_ENABLED=false each block at the irreversible
    moment (revert the kill-switch re-check and the connector is hit);
  * dry_run never submits, and neither confirm flag alone does;
  * defined-risk-only: a naked / uncovered short is refused before any gate, so
    it can never reach the connector.

The connector is MOCKED throughout; no live Robinhood call is ever made.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.kill_switch import KillSwitch
from src.robinhood_option_broker import RobinhoodOptionBroker
from src.robinhood_option_client import (
    DefinedRiskViolationError,
    RobinhoodOptionClient,
)
from src.strategy_engine import TradeSignal


class _FakeLogger:
    """Records log_decision calls; the caps path logs a refusal rationale."""

    def __init__(self) -> None:
        self.decisions: list[tuple] = []

    def log_decision(self, symbol, action, reason, details):
        self.decisions.append((symbol, action, reason, details))


class _FakeOrderManager:
    """A stand-in OrderManager -- submit_signal only reads its .logger now that
    the options lane sizes through its own builder, not the equities dollar path."""

    def __init__(self) -> None:
        self.logger = _FakeLogger()


def _long_call_signal() -> TradeSignal:
    return TradeSignal(symbol=LONG_CALL, side="buy", confidence=1.0, reason="unit-test long call")

# The agent-tradable account carries the SAME ground-truth identity the equities
# and options clients pin: nickname "Agentic", number ending 2092.
AGENT_ACCOUNT = {"account_number": "RH-OPT-AGENTIC-2092", "nickname": "Agentic", "agentic_allowed": True}
DEFAULT_ACCOUNT = {"account_number": "RH-OPT-DEFAULT-2833", "nickname": "Default", "agentic_allowed": False}
EXPECTED_ACCOUNT = {"nickname": "Agentic", "number_suffix": "2092"}

LONG_CALL = "OPT-XYZ-CALL-LONG"
SHORT_CALL = "OPT-XYZ-CALL-SHORT"

LONG_LEG = {"side": "buy", "position_effect": "open", "ratio_quantity": 1, "option": LONG_CALL}
SHORT_LEG = {"side": "sell", "position_effect": "open", "ratio_quantity": 1, "option": SHORT_CALL}

# A 10:1 ratio: a long leg plus nine uncovered extra shorts. The old
# presence-only check passed it because a buy leg is present.
RATIO_LEGS = [
    LONG_LEG,
    {"side": "sell", "position_effect": "open", "ratio_quantity": 10, "option": SHORT_CALL},
]
# A short call 'covered' by a long PUT -- a put does not cover a call, so the
# short call is naked. A buy leg is present, so the old check waved it through.
SELL_CALL_BUY_PUT_LEGS = [
    {"side": "buy", "position_effect": "open", "ratio_quantity": 1, "option_type": "put", "option": "OPT-XYZ-PUT"},
    {"side": "sell", "position_effect": "open", "ratio_quantity": 1, "option_type": "call", "option": SHORT_CALL},
]


def _assert_connector_leg_shape(legs):
    """The real Robinhood options MCP order schema requires each leg to carry
    `option_id` (the option instrument UUID), NOT the legacy `option` key. The
    broker hands raw `option`-keyed legs to the client, which re-keys them; assert
    the connector only ever sees the schema key so that re-keying stays
    load-bearing through the broker path too."""
    assert legs is not None, "connector received no legs"
    for leg in legs:
        assert "option_id" in leg, f"leg missing required 'option_id': {leg}"
        assert "option" not in leg, f"leg carries the wrong key 'option' (schema wants 'option_id'): {leg}"


class FakeArmStore:
    """Answers is_armed for the requested lane; records what it was asked."""

    def __init__(self, armed: bool = False) -> None:
        self.armed = armed
        self.queried: list[str] = []

    def is_armed(self, lane: str) -> bool:
        self.queried.append(lane)
        return self.armed


class FakeConnector:
    """Records calls instead of reaching the real Robinhood options connector."""

    def __init__(self, accounts: list[dict] | None = None, option_level: str = "level_3") -> None:
        self.accounts = accounts if accounts is not None else [AGENT_ACCOUNT, DEFAULT_ACCOUNT]
        self.option_level = option_level
        self.place_calls: list[dict] = []
        self.review_calls: list[dict] = []
        self.cancel_calls: list[dict] = []
        self.position_calls: list[dict] = []

    def get_accounts(self):
        return {"accounts": self.accounts}

    def get_option_chains(self, **kwargs):
        return {"chains": []}

    def get_option_quotes(self, **kwargs):
        return {"quotes": []}

    def get_option_positions(self, account_number=None):
        self.position_calls.append({"account_number": account_number})
        return {"positions": []}

    def get_option_level_upgrade_info(self, **kwargs):
        return {"option_level": self.option_level}

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


def make_broker(
    connector: FakeConnector,
    *,
    armed: bool = True,
    dry_run: bool = False,
    confirm_live_order: bool = True,
    stop_file: Path | None = None,
    trading_enabled: str = "true",
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> RobinhoodOptionBroker:
    """A broker + client sharing ONE arm store, with a kill switch whose stop file
    lives in tmp_path and whose env var is set explicitly. Defaults are the fully
    armed, fully confirmed live posture, so each test flips exactly one gate."""
    monkeypatch.setenv("TRADING_ENABLED", trading_enabled)
    store = FakeArmStore(armed=armed)
    client = RobinhoodOptionClient(connector, arm_store=store, expected_account=EXPECTED_ACCOUNT)
    stop = stop_file if stop_file is not None else (tmp_path / "STOP_TRADING_OPTIONS")
    kill = KillSwitch(stop_file=str(stop), env_var="TRADING_ENABLED")
    return RobinhoodOptionBroker(
        client,
        arm_store=store,
        dry_run=dry_run,
        confirm_live_order=confirm_live_order,
        kill_switch=kill,
    )


# --- the happy path: fully armed + confirmed live actually submits ------------


def test_fully_armed_and_confirmed_submits(monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)

    result = broker.submit_option_order(
        legs=[LONG_LEG], direction="debit", price="1.00", days_to_expiry=30, mode="live"
    )

    assert result["submitted"] is True
    assert result["status"] == "submitted"
    # The witness: exactly one order reached the mocked connector.
    assert len(connector.place_calls) == 1
    # And it carried the schema key option_id, re-keyed from the raw `option` leg.
    assert connector.place_calls[0]["legs"][0]["option_id"] == LONG_CALL
    assert "option" not in connector.place_calls[0]["legs"][0]


# --- property: no phantom fill -- the broker reports the CLIENT's real verdict --
# The broker hands the order to the client, which re-runs its OWN final gates
# (defined risk, the shared arm store, the caps: defense in depth). If the client
# refuses, nothing reached the connector -- and the broker must report that, not a
# phantom submitted=True.


def test_broker_reports_not_submitted_when_client_refuses_at_its_own_gate(monkeypatch, tmp_path):
    """PHANTOM-FILL MUTATION TEST: every broker gate is cleared, but the client
    refuses at ITS final arm gate (here the client's shared arm store reads
    DISARMED while the broker's reads ARMED). The connector is never reached, so
    the broker must report submitted=False and carry the client's status back.
    Revert the broker to hard-code submitted=True after the handoff and this
    fails -- the result would claim a fill that never left the building."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    connector = FakeConnector()
    # Broker sees ARMED; the client is wired to a DISARMED store, so the broker
    # clears its gates and hands off, and the client is the one that refuses.
    broker_store = FakeArmStore(armed=True)
    client_store = FakeArmStore(armed=False)
    client = RobinhoodOptionClient(connector, arm_store=client_store, expected_account=EXPECTED_ACCOUNT)
    kill = KillSwitch(stop_file=str(tmp_path / "STOP_TRADING_OPTIONS"), env_var="TRADING_ENABLED")
    broker = RobinhoodOptionBroker(
        client, arm_store=broker_store, dry_run=False, confirm_live_order=True, kill_switch=kill
    )

    result = broker.submit_option_order(
        legs=[LONG_LEG], direction="debit", quantity="1", price="1.00", days_to_expiry=30, mode="live"
    )

    assert result["submitted"] is False
    # The broker surfaces the client's own refusal status, not "submitted".
    assert result["status"] == "options_lane_disarmed"
    assert connector.place_calls == []


# --- property: disarmed lane blocks even with the full human confirm ----------


def test_disarmed_lane_blocks_even_with_confirm(monkeypatch, tmp_path):
    connector = FakeConnector()
    # dry_run False + confirm True (human gate fully cleared), but the lane is
    # DISARMED. Revert the arm gate and this order would submit.
    broker = make_broker(connector, armed=False, monkeypatch=monkeypatch, tmp_path=tmp_path)

    result = broker.submit_option_order(legs=[LONG_LEG], direction="debit", mode="live")

    assert result["submitted"] is False
    assert result["status"] == "options_lane_disarmed"
    assert connector.place_calls == []


def test_gate_reason_names_the_disarmed_lane(monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(connector, armed=False, monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert "options lane is not armed" in broker.gate_reason()


class _ExplodingArmStore:
    """An arm store whose is_armed RAISES -- an unreadable store at the
    irreversible moment (a real backend erroring under the connector call)."""

    def is_armed(self, lane: str) -> bool:
        raise RuntimeError("arm store unreadable")


def test_unreadable_arm_store_blocks_and_never_submits(monkeypatch, tmp_path):
    """MUTATION TEST: the broker's arm store RAISES. _is_options_lane_armed's
    `except Exception: return False` must read that as DISARMED (fail safe) -- a
    preview, no connector call, no propagating exception. Remove that guard and
    submit_option_order raises instead of returning a preview, so both the
    submitted==False assertion and the no-raise are lost."""
    monkeypatch.setenv("TRADING_ENABLED", "true")
    connector = FakeConnector()
    # Broker wired to an exploding store; the client gets a benign ARMED store so
    # the branch under test is the BROKER's own arm read (checked first).
    client = RobinhoodOptionClient(
        connector, arm_store=FakeArmStore(armed=True), expected_account=EXPECTED_ACCOUNT
    )
    kill = KillSwitch(stop_file=str(tmp_path / "STOP_TRADING_OPTIONS"), env_var="TRADING_ENABLED")
    broker = RobinhoodOptionBroker(
        client, arm_store=_ExplodingArmStore(), dry_run=False, confirm_live_order=True, kill_switch=kill
    )

    result = broker.submit_option_order(
        legs=[LONG_LEG], direction="debit", quantity="1", price="1.00", days_to_expiry=30, mode="live"
    )

    assert result["submitted"] is False
    assert result["status"] == "options_lane_disarmed"
    assert connector.place_calls == []
    assert broker._is_options_lane_armed() is False  # the guard swallowed the raise


def test_broker_rejects_a_non_options_arm_lane(monkeypatch, tmp_path):
    """MUTATION TEST: the leveraged options broker refuses any arm lane but
    'options'. crypto/equities are arm-tracked by ABSENCE of a stop file (ARMED by
    default), so consulting one here would silently fail OPEN. Drop the lane guard
    in __init__ and construction succeeds instead of raising."""
    connector = FakeConnector()
    client = RobinhoodOptionClient(
        connector, arm_store=FakeArmStore(armed=True), expected_account=EXPECTED_ACCOUNT
    )
    kill = KillSwitch(stop_file=str(tmp_path / "STOP_TRADING_OPTIONS"), env_var="TRADING_ENABLED")
    for bad_lane in ("equities", "crypto", "bogus", ""):
        with pytest.raises(ValueError, match="fail-closed"):
            RobinhoodOptionBroker(
                client,
                arm_store=FakeArmStore(armed=False),
                lane=bad_lane,
                dry_run=False,
                confirm_live_order=True,
                kill_switch=kill,
            )


# --- property: STOP_TRADING_OPTIONS blocks at the irreversible moment ---------


def test_stop_trading_options_file_blocks(monkeypatch, tmp_path):
    connector = FakeConnector()
    stop = tmp_path / "STOP_TRADING_OPTIONS"
    stop.write_text("halted", encoding="utf-8")
    # Fully armed + confirmed, but the kill switch's stop file exists.
    broker = make_broker(connector, stop_file=stop, monkeypatch=monkeypatch, tmp_path=tmp_path)

    with pytest.raises(RuntimeError, match="kill switch is engaged"):
        broker.submit_option_order(
            legs=[LONG_LEG], direction="debit", price="1.00", days_to_expiry=30, mode="live"
        )
    assert connector.place_calls == []


# --- property: TRADING_ENABLED=false blocks -----------------------------------


def test_trading_enabled_false_blocks(monkeypatch, tmp_path):
    connector = FakeConnector()
    # Fully armed + confirmed, no stop file, but TRADING_ENABLED is not true.
    broker = make_broker(connector, trading_enabled="false", monkeypatch=monkeypatch, tmp_path=tmp_path)

    with pytest.raises(RuntimeError, match="TRADING_ENABLED=false"):
        broker.submit_option_order(
            legs=[LONG_LEG], direction="debit", price="1.00", days_to_expiry=30, mode="live"
        )
    assert connector.place_calls == []


# --- property: dry_run never submits, and neither flag alone does -------------


def test_dry_run_default_never_submits(monkeypatch, tmp_path):
    connector = FakeConnector()
    # Default posture: dry_run True, no confirmation. Armed lane must not matter.
    broker = make_broker(
        connector, dry_run=True, confirm_live_order=False, monkeypatch=monkeypatch, tmp_path=tmp_path
    )

    result = broker.submit_option_order(legs=[LONG_LEG], direction="debit", mode="live")

    assert result["submitted"] is False
    assert result["status"] == "dry_run_order_preview"
    assert connector.place_calls == []


def test_confirm_without_dry_run_off_does_not_submit(monkeypatch, tmp_path):
    connector = FakeConnector()
    # confirm True but dry_run still True -- a single flag must not arm the lane.
    broker = make_broker(
        connector, dry_run=True, confirm_live_order=True, monkeypatch=monkeypatch, tmp_path=tmp_path
    )
    result = broker.submit_option_order(legs=[LONG_LEG], direction="debit", mode="live")
    assert result["submitted"] is False
    assert connector.place_calls == []


def test_truthy_nonboolean_confirm_does_not_submit(monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    # A truthy-but-not-True confirm must not arm the lane (identity, not truthiness).
    broker.confirm_live_order = "yes"  # type: ignore[assignment]
    result = broker.submit_option_order(legs=[LONG_LEG], direction="debit", mode="live")
    assert result["submitted"] is False
    assert connector.place_calls == []


def test_non_live_mode_forces_preview_even_when_armed(monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    # Fully armed + confirmed, but a preview run mode must never submit.
    result = broker.submit_option_order(legs=[LONG_LEG], direction="debit", mode="live-dry-run")
    assert result["submitted"] is False
    assert "forced preview" in result["human_gate"]
    assert connector.place_calls == []


# --- property: defined-risk only -- naked short refused before any gate -------


def test_naked_short_is_refused(monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    # A lone sell-to-open leg with no covering buy-to-open leg, fully armed and
    # confirmed. It must be refused at build time, before any gate.
    with pytest.raises(DefinedRiskViolationError, match="uncovered short"):
        broker.submit_option_order(legs=[SHORT_LEG], direction="credit", mode="live")
    assert connector.place_calls == []


def test_credit_opening_with_no_long_leg_is_refused(monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    with pytest.raises(DefinedRiskViolationError):
        broker.submit_option_order(legs=[dict(SHORT_LEG)], direction="credit", mode="live")
    assert connector.place_calls == []


def test_naked_short_refused_even_on_disarmed_preview_broker(monkeypatch, tmp_path):
    connector = FakeConnector()
    # Disarmed, dry_run default -- the order would preview anyway, but a naked
    # short must be refused OUTRIGHT, not previewed.
    broker = make_broker(
        connector, armed=False, dry_run=True, confirm_live_order=False, monkeypatch=monkeypatch, tmp_path=tmp_path
    )
    with pytest.raises(DefinedRiskViolationError):
        broker.submit_option_order(legs=[SHORT_LEG], direction="credit", mode="live")
    assert connector.place_calls == []


def test_long_plus_short_opening_pair_is_refused(monkeypatch, tmp_path):
    """MUTATION TEST: a long + short opening pair is refused, fully gated. The
    lane cannot prove the short is covered without strike-aware spread support,
    so any opening sell -- even with a buy present -- is refused before any gate
    and never reaches the connector. Revert to the presence-only check and this
    order submits."""
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    with pytest.raises(DefinedRiskViolationError):
        broker.submit_option_order(legs=[LONG_LEG, SHORT_LEG], direction="debit", mode="live")
    assert connector.place_calls == []


def test_a_ten_to_one_ratio_is_refused(monkeypatch, tmp_path):
    """The audit's 10:1 ratio (buy 1, sell 10). A long leg is present, but the
    extra shorts are uncovered. Fully armed + confirmed + live, it must never
    reach the connector."""
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    with pytest.raises(DefinedRiskViolationError):
        broker.submit_option_order(legs=RATIO_LEGS, direction="debit", mode="live")
    assert connector.place_calls == []


def test_a_short_call_covered_by_a_long_put_is_refused(monkeypatch, tmp_path):
    """The audit's call-'covered'-by-a-put. A buy leg is present, so the old
    presence-only check passed it to the connector. Fully gated, it must never
    place."""
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    with pytest.raises(DefinedRiskViolationError):
        broker.submit_option_order(legs=SELL_CALL_BUY_PUT_LEGS, direction="debit", mode="live")
    assert connector.place_calls == []


def test_empty_legs_refused(monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    with pytest.raises(DefinedRiskViolationError):
        broker.submit_option_order(legs=[], direction="debit", mode="live")
    assert connector.place_calls == []


# --- the broker's OWN defined-risk guard, unit-tested directly -----------------
# The end-to-end tests above prove naked shorts are rejected through the stack,
# but the client validates too (defense in depth), so those alone would still
# pass if the broker's own check were reverted. These pin the broker's guard
# method directly, so neutering _assert_defined_risk fails here regardless of the
# client.


def test_broker_defined_risk_guard_rejects_naked_short(monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    with pytest.raises(DefinedRiskViolationError, match="uncovered short"):
        broker._assert_defined_risk([SHORT_LEG], "credit")


def test_broker_defined_risk_guard_rejects_empty(monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    with pytest.raises(DefinedRiskViolationError, match="no legs"):
        broker._assert_defined_risk([], "debit")


def test_broker_defined_risk_guard_allows_long_but_refuses_any_opening_sell(monkeypatch, tmp_path):
    """A lone long leg (and a sell-to-CLOSE exit) pass the guard; ANY opening
    sell -- even the long+short pair the old check allowed -- raises. Pinning the
    broker's own guard directly, so neutering it fails here regardless of the
    client."""
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    broker._assert_defined_risk([LONG_LEG], "debit")
    broker._assert_defined_risk(
        [{"side": "sell", "position_effect": "close", "ratio_quantity": 1, "option": LONG_CALL}], "credit"
    )
    with pytest.raises(DefinedRiskViolationError):
        broker._assert_defined_risk([LONG_LEG, SHORT_LEG], "debit")


# --- account gate fires before anything else ----------------------------------


def test_wrong_account_is_refused(monkeypatch, tmp_path):
    from src.robinhood_option_client import AgentAccountMismatchError

    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    with pytest.raises(AgentAccountMismatchError):
        broker.submit_option_order(
            legs=[LONG_LEG], direction="debit", account_number="RH-OPT-DEFAULT-2833", mode="live"
        )
    assert connector.place_calls == []


# --- place_long_call convenience routes through the same gated path -----------


def test_place_long_call_submits_when_armed(monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    result = broker.place_long_call(LONG_CALL, price="1.00", days_to_expiry=30, mode="live")
    assert result["submitted"] is True
    assert len(connector.place_calls) == 1


def test_place_long_call_blocked_when_disarmed(monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(connector, armed=False, monkeypatch=monkeypatch, tmp_path=tmp_path)
    result = broker.place_long_call(LONG_CALL, mode="live")
    assert result["submitted"] is False
    assert connector.place_calls == []


# --- OrderManager-facing place_limit_order ------------------------------------


def test_place_limit_order_buy_submits_when_armed(monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    order = {"symbol": LONG_CALL, "side": "buy", "quantity": "1", "limit_price": 2.50}
    result = broker.place_limit_order(order, mode="live")
    assert result["submitted"] is True
    assert len(connector.place_calls) == 1


def test_place_limit_order_disarmed_previews(monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(connector, armed=False, monkeypatch=monkeypatch, tmp_path=tmp_path)
    order = {"symbol": LONG_CALL, "side": "buy", "quantity": "1", "limit_price": 2.50}
    result = broker.place_limit_order(order, mode="live")
    assert result["submitted"] is False
    assert connector.place_calls == []


def test_place_limit_order_nonlive_mode_forces_preview(monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    order = {"symbol": LONG_CALL, "side": "buy", "quantity": "1", "limit_price": 2.50}
    result = broker.place_limit_order(order, mode="live-dry-run")
    assert result["submitted"] is False
    assert connector.place_calls == []


# --- cancel is not held to the arm fact ---------------------------------------


def test_cancel_not_gated_on_arm(monkeypatch, tmp_path):
    connector = FakeConnector()
    # Disarmed, but fully confirmed -- cancelling reduces exposure and must not be
    # blocked by the lane being disarmed.
    broker = make_broker(connector, armed=False, monkeypatch=monkeypatch, tmp_path=tmp_path)
    result = broker.cancel_order("opt-order-1")
    assert connector.cancel_calls == [{"order_id": "opt-order-1", "account_number": AGENT_ACCOUNT["account_number"]}]
    assert result["status"] == "cancel_requested"


def test_cancel_dry_run_prepared_when_unconfirmed(monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(
        connector, dry_run=True, confirm_live_order=False, monkeypatch=monkeypatch, tmp_path=tmp_path
    )
    result = broker.cancel_order("opt-order-1")
    assert result["status"] == "dry_run_cancel_prepared"
    assert connector.cancel_calls == []


# --- default kill switch and arm store are ROOT-anchored (fail closed) --------


def test_default_kill_switch_is_options_lane(monkeypatch, tmp_path):
    connector = FakeConnector()
    store = FakeArmStore(armed=True)
    client = RobinhoodOptionClient(connector, arm_store=store, expected_account=EXPECTED_ACCOUNT)
    # No kill switch passed: the broker must substitute the options lane's own.
    broker = RobinhoodOptionBroker(client, arm_store=store, dry_run=False, confirm_live_order=True)
    assert broker.kill_switch.stop_file.name == "STOP_TRADING_OPTIONS"
    assert broker.kill_switch.env_var == "TRADING_ENABLED"


# --- the REAL default arm store is POSITIVE + fail-closed ---------------------
# A broker built with NO explicit arm store consults the shared default store
# (build_arm_store, ROOT-anchored). That default must read DISARMED unless a
# positive ARM_STATE_OPTIONS marker exists, and must NEVER fall back to a local
# fail-open store when a cloud backend was requested but could not be built. Both
# tests build the broker WITHOUT an arm store and fully confirmed live; each FAILS
# if the fail-closed default is reverted, because the order would then submit and
# connector.place_calls would be non-empty. The connector stays mocked throughout.


def _default_store_broker(connector, monkeypatch, tmp_path):
    """A broker with its REAL default arm store, ROOT pointed at an empty tmp tree
    (no ARM_STATE_OPTIONS marker, no config), fully confirmed live, kill switch
    open."""
    monkeypatch.setattr("src.robinhood_option_broker.ROOT", tmp_path)
    monkeypatch.setenv("TRADING_ENABLED", "true")
    client = RobinhoodOptionClient(connector, expected_account=EXPECTED_ACCOUNT)
    kill = KillSwitch(stop_file=str(tmp_path / "STOP_TRADING_OPTIONS"), env_var="TRADING_ENABLED")
    # arm_store omitted on purpose: the broker must build its own fail-closed default.
    return RobinhoodOptionBroker(client, dry_run=False, confirm_live_order=True, kill_switch=kill)


def test_default_arm_store_disarmed_without_a_positive_marker(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADER_ARM_FIRESTORE_PROJECT", raising=False)
    connector = FakeConnector()
    broker = _default_store_broker(connector, monkeypatch, tmp_path)
    assert broker._is_options_lane_armed() is False  # no marker == DISARMED
    result = broker.submit_option_order(
        legs=[LONG_LEG], direction="debit", quantity="1", price="1.00", days_to_expiry=30, mode="live"
    )
    assert result["submitted"] is False
    assert result["status"] == "options_lane_disarmed"
    assert connector.place_calls == []


def test_default_arm_store_disarmed_when_firestore_requested_but_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADER_ARM_FIRESTORE_PROJECT", "some-project")

    def _boom(*args, **kwargs):
        raise RuntimeError("firestore unavailable")

    monkeypatch.setattr("src.shared_state.FirestoreArmStore", _boom)
    connector = FakeConnector()
    broker = _default_store_broker(connector, monkeypatch, tmp_path)
    # A requested-but-unbuildable cloud store must fail CLOSED, never fall back to
    # a local fail-open file store.
    assert broker._is_options_lane_armed() is False
    result = broker.submit_option_order(
        legs=[LONG_LEG], direction="debit", quantity="1", price="1.00", days_to_expiry=30, mode="live"
    )
    assert result["submitted"] is False
    assert result["status"] == "options_lane_disarmed"
    assert connector.place_calls == []


# --- caps AT SUBMIT: 0DTE / over-contract / over-debit / over-level refused ----
# Fix A. The option risk caps (option_risk_gates) run INSIDE submit_option_order,
# before the client/connector call -- and are mirrored in the client (defense in
# depth). A FULLY armed + confirmed + live order that violates a cap returns an
# unsubmitted preview (status "options_risk_gate_blocked") and never reaches the
# connector. Each test FAILS if the caps call is reverted: the order would then
# submit and connector.place_calls would be non-empty. The caps are read from the
# repo's config/trading_rules.yaml (max_debit $500, max_contracts 5, min_dte 2,
# 0DTE blocked, single-leg long needs level 2).


def test_within_caps_order_submits(monkeypatch, tmp_path):
    """A compliant long (2 contracts x $2.50 = $500 debit == cap, 30d, level 3)
    still submits -- the caps are active but do not block a compliant order."""
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    result = broker.submit_option_order(
        legs=[LONG_LEG], direction="debit", quantity="2", price="2.50", days_to_expiry=30, mode="live"
    )
    assert result["submitted"] is True
    assert len(connector.place_calls) == 1


def test_zero_dte_order_refused_even_fully_armed(monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    result = broker.submit_option_order(
        legs=[LONG_LEG], direction="debit", quantity="1", price="1.00", days_to_expiry=0, mode="live"
    )
    assert result["submitted"] is False
    assert result["status"] == "options_risk_gate_blocked"
    assert "zero_dte" in result["human_gate"]
    assert connector.place_calls == []


def test_over_contract_order_refused_even_fully_armed(monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    # 6 contracts > the 5-contract per-order cap; premium kept small so ONLY the
    # contract cap blocks (0.50 x 100 x 6 = $300 debit, under the $500 cap).
    result = broker.submit_option_order(
        legs=[LONG_LEG], direction="debit", quantity="6", price="0.50", days_to_expiry=30, mode="live"
    )
    assert result["submitted"] is False
    assert result["status"] == "options_risk_gate_blocked"
    assert "max_contracts_per_order" in result["human_gate"]
    assert connector.place_calls == []


def test_over_debit_order_refused_even_fully_armed(monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    # 1 contract x $6.00 x 100 = $600 debit, over the $500 per-trade debit cap.
    result = broker.submit_option_order(
        legs=[LONG_LEG], direction="debit", quantity="1", price="6.00", days_to_expiry=30, mode="live"
    )
    assert result["submitted"] is False
    assert result["status"] == "options_risk_gate_blocked"
    assert "max_debit_premium_per_trade" in result["human_gate"]
    assert connector.place_calls == []


def test_over_approval_level_order_refused_even_fully_armed(monkeypatch, tmp_path):
    # The account is granted only level 1; a single-leg long needs level 2.
    connector = FakeConnector(option_level="level_1")
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    result = broker.submit_option_order(
        legs=[LONG_LEG], direction="debit", quantity="1", price="1.00", days_to_expiry=30, mode="live"
    )
    assert result["submitted"] is False
    assert result["status"] == "options_risk_gate_blocked"
    assert "option_approval_level" in result["human_gate"]
    assert connector.place_calls == []


def test_place_long_call_refuses_a_known_zero_dte(monkeypatch, tmp_path):
    """The convenience long-call wrapper threads days_to_expiry into the caps: a
    known 0DTE long is refused even fully armed."""
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    result = broker.place_long_call(LONG_CALL, price="1.00", days_to_expiry=0, mode="live")
    assert result["submitted"] is False
    assert result["status"] == "options_risk_gate_blocked"
    assert connector.place_calls == []


# --- submit-hardening (1): a None / <= 0 / non-numeric price is refused --------
# A live limit order priced at None / non-numeric / <= 0 makes BOTH dollar caps
# read $0 of new risk (max(premium, 0) = 0), voiding them at the irreversible
# submit. The broker refuses it (status 'invalid_limit_price') and never reaches
# the connector, even fully armed + confirmed + live. Each FAILS if the price
# guard is reverted.


@pytest.mark.parametrize("bad_price", [None, "0", "0.00", "-2.00", "abc", "nan", "inf"])
def test_none_or_nonpositive_price_refused_even_fully_armed(bad_price, monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    result = broker.submit_option_order(
        legs=[LONG_LEG], direction="debit", quantity="1", price=bad_price, days_to_expiry=30, mode="live"
    )
    assert result["submitted"] is False
    assert result["status"] == "invalid_limit_price"
    assert connector.place_calls == []


def test_none_price_refused_by_the_brokers_own_guard(monkeypatch, tmp_path):
    """MUTATION TEST pinning the BROKER's own price guard (independent of the
    client's mirror): the broker logs its own invalid-price refusal reason before
    the client is ever reached. Revert the broker's price guard and the client
    still catches it, but the broker would log 'client did not submit' instead of
    the price reason -- so this assertion fails."""
    connector = FakeConnector()
    monkeypatch.setenv("TRADING_ENABLED", "true")
    store = FakeArmStore(armed=True)
    client = RobinhoodOptionClient(connector, arm_store=store, expected_account=EXPECTED_ACCOUNT)
    kill = KillSwitch(stop_file=str(tmp_path / "STOP_TRADING_OPTIONS"), env_var="TRADING_ENABLED")
    logger = _FakeLogger()
    broker = RobinhoodOptionBroker(
        client, arm_store=store, dry_run=False, confirm_live_order=True, kill_switch=kill, logger=logger
    )

    result = broker.submit_option_order(legs=[LONG_LEG], direction="debit", price=None, mode="live")

    assert result["submitted"] is False
    assert result["status"] == "invalid_limit_price"
    assert connector.place_calls == []
    # The broker's OWN guard logged the price reason (not a client-handoff status).
    assert any("not a positive number" in str(reason) for _, _, reason, _ in logger.decisions)


# --- submit-hardening (2): the connector leg carries ONLY the schema keys ------
# Through the broker path too, the leg the connector receives must be exactly
# {option_id, side, position_effect, ratio_quantity} (the real MCP schema is
# additionalProperties:false) -- the broker hands rich legs to the client, which
# strips them at the wire.


def test_broker_submit_strips_schema_forbidden_leg_keys_at_the_connector(monkeypatch, tmp_path):
    """MUTATION TEST: a rich leg (with option_type / underlying / strike /
    expiration) submitted through the broker reaches the connector stripped to
    exactly the four allowed keys. Revert the client's wire projection and the
    connector sees forbidden keys."""
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    rich_leg = {
        "side": "buy",
        "position_effect": "open",
        "ratio_quantity": 1,
        "option": LONG_CALL,
        "option_type": "call",
        "underlying": "XYZ",
        "strike_price": "100",
        "expiration_date": "2026-09-18",
    }
    result = broker.submit_option_order(
        legs=[rich_leg], direction="debit", quantity="1", price="1.00", days_to_expiry=30, mode="live"
    )
    assert result["submitted"] is True
    placed_leg = connector.place_calls[0]["legs"][0]
    assert set(placed_leg) == {"option_id", "side", "position_effect", "ratio_quantity"}
    assert placed_leg["option_id"] == LONG_CALL


# --- submit-hardening (3): standing exposure is sourced from open positions ----
# The live path must not trust open_premium_at_risk_usd=0.0 -- it sources the
# premium already at risk in open option positions from the connector, so the
# portfolio total-at-risk cap ($1,500) cannot be under-counted. A second order
# is refused once the standing exposure fills the cap.


class PositionsConnector(FakeConnector):
    """A connector that reports OPEN option positions, so the broker's standing-
    exposure sourcing has something to sum."""

    def __init__(self, positions, **kwargs):
        super().__init__(**kwargs)
        self._positions = positions

    def get_option_positions(self, account_number=None):
        self.position_calls.append({"account_number": account_number})
        return {"positions": self._positions}


def test_second_order_refused_once_open_exposure_fills_the_cap(monkeypatch, tmp_path):
    """MUTATION TEST: $1,400 is already at risk in open positions (7 contracts x
    $2.00 x 100). A new $200 order (1 x $2.00 x 100) is within the debit cap but
    pushes total at-risk to $1,600, over the $1,500 cap -- so it is REFUSED with
    status 'options_risk_gate_blocked'. Revert the positions sourcing and the
    open exposure reads as $0, the new order clears the cap, and it SUBMITS."""
    connector = PositionsConnector(
        positions=[{"quantity": "7", "average_open_price": "2.00", "option_id": LONG_CALL}]
    )
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)

    result = broker.submit_option_order(
        legs=[LONG_LEG], direction="debit", quantity="1", price="2.00", days_to_expiry=30, mode="live"
    )

    assert result["submitted"] is False
    assert result["status"] == "options_risk_gate_blocked"
    assert "max_total_premium_at_risk" in result["human_gate"]
    assert connector.place_calls == []


def test_order_within_cap_still_submits_with_open_exposure(monkeypatch, tmp_path):
    """Precision: standing exposure UNDER the cap does not block a compliant new
    order. $600 open (3 x $2.00 x 100) + $200 new = $800 <= $1,500 -> submits.
    Ensures the sourcing tightens the cap without falsely blocking."""
    connector = PositionsConnector(
        positions=[{"quantity": "3", "average_open_price": "2.00", "option_id": LONG_CALL}]
    )
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)

    result = broker.submit_option_order(
        legs=[LONG_LEG], direction="debit", quantity="1", price="2.00", days_to_expiry=30, mode="live"
    )

    assert result["submitted"] is True
    assert len(connector.place_calls) == 1


def test_open_exposure_from_positions_sums_premium_and_max_loss(monkeypatch, tmp_path):
    """Pin the broker's sourcing method directly: a position's standing risk is
    its recorded max_loss when present, else premium x 100 x contracts. Revert it
    and this fails independently of the end-to-end cap test."""
    connector = PositionsConnector(
        positions=[
            {"quantity": "2", "average_open_price": "1.50"},  # 2 x 1.50 x 100 = 300
            {"quantity": "1", "max_loss_usd": "250"},          # recorded max loss = 250
            {"quantity": "0", "average_open_price": "9.99"},   # closed -> ignored
        ]
    )
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert broker._open_premium_at_risk_from_positions() == 550.0


def test_open_exposure_is_fail_soft_on_an_unreadable_positions_read(monkeypatch, tmp_path):
    """A positions read that raises contributes 0.0 rather than crashing the
    submit -- the under-count it guards against is re-checked next order."""
    connector = FakeConnector()

    def _boom(account_number=None):
        raise RuntimeError("positions unavailable")

    connector.get_option_positions = _boom  # type: ignore[assignment]
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert broker._open_premium_at_risk_from_positions() == 0.0


def test_caller_supplied_open_exposure_is_not_undercounted(monkeypatch, tmp_path):
    """When a caller passes an explicit open-exposure figure HIGHER than what the
    positions sum to, the higher figure wins (max), so the cap is never reset
    downward. Here the caller's $1,450 + a new $200 order exceeds the $1,500 cap
    even though the connector reports no open positions."""
    connector = FakeConnector()  # reports no open positions
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    order = {
        "symbol": LONG_CALL,
        "side": "buy",
        "quantity": "1",
        "limit_price": 2.00,
        "days_to_expiry": 30,
        "open_premium_at_risk_usd": 1450.0,
    }
    result = broker.place_limit_order(order, mode="live")
    assert result["submitted"] is False
    assert result["status"] == "options_risk_gate_blocked"
    assert "max_total_premium_at_risk" in result["human_gate"]
    assert connector.place_calls == []


# --- Fix B: the options lane's OWN sizing (contract count + true notional) ------
# submit_signal must NOT size options through the equities dollar path
# (order_manager.build_limit_order, which counts one unit as one share and
# under-counts option exposure ~100x). Its own builder sets quantity to a whole
# contract count and notional/at-risk to premium x 100 x contracts.


def test_broker_caps_block_method_flags_each_cap(monkeypatch, tmp_path):
    """Pin the broker's OWN caps method directly, so reverting it fails here
    regardless of the client's mirror (the end-to-end tests alone would still
    pass on the client's caps if the broker's were neutered)."""
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    zero_dte = broker._option_caps_block_reason([LONG_LEG], "debit", "1", "1.00", 0, 0.0, None)
    over_ct = broker._option_caps_block_reason([LONG_LEG], "debit", "6", "0.50", 30, 0.0, None)
    over_debit = broker._option_caps_block_reason([LONG_LEG], "debit", "1", "6.00", 30, 0.0, None)
    within = broker._option_caps_block_reason([LONG_LEG], "debit", "2", "2.50", 30, 0.0, None)
    assert "zero_dte" in (zero_dte or "")
    assert "max_contracts_per_order" in (over_ct or "")
    assert "max_debit_premium_per_trade" in (over_debit or "")
    assert within is None


def test_broker_caps_block_method_flags_over_level(monkeypatch, tmp_path):
    connector = FakeConnector(option_level="level_1")
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    reason = broker._option_caps_block_reason([LONG_LEG], "debit", "1", "1.00", 30, 0.0, None)
    assert "option_approval_level" in (reason or "")


def test_broker_caps_relax_dte_only_when_the_expiry_is_unknown(monkeypatch, tmp_path):
    """A bare order with no expiry (and no price) clears the caps -- the DTE floor
    is relaxed only when the expiry is genuinely unknown, while the dollar/size/
    level caps still apply."""
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert broker._option_caps_block_reason([LONG_LEG], "debit", "1", None, None, 0.0, None) is None


def test_build_option_limit_order_sizes_on_the_contract_multiplier(monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    order = broker.build_option_limit_order(_long_call_signal(), limit_price=2.50, contracts=3)
    assert order["quantity"] == 3  # a CONTRACT count, never a share count
    assert order["contracts"] == 3
    assert order["notional"] == 750.0  # 2.50 x 100 x 3, not 7.50
    assert order["at_risk_usd"] == 750.0


def test_submit_signal_sizes_on_true_cost_and_refuses_over_contract(monkeypatch, tmp_path):
    """A $2,000 budget on a $2.50 option = $250 TRUE cost/contract = 8 contracts,
    over the 5-contract cap -> REFUSED. The old share-based path would have read
    the budget as 800 'shares' at $2,000 notional and cleared the dollar cap, so
    this pins that options size on the 100x multiplier, not the equities path."""
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    result = broker.submit_signal(
        _FakeOrderManager(), _long_call_signal(), limit_price=2.50, mode="live",
        portfolio=None, daily_summary={}, amount_usd=2000.0, days_to_expiry=30,
    )
    assert result["submitted"] is False
    assert result["status"] == "options_risk_gate_blocked"
    assert "max_contracts_per_order" in result["human_gate"]
    assert connector.place_calls == []


def test_submit_signal_within_budget_submits_true_contract_count(monkeypatch, tmp_path):
    """A $500 budget on a $2.50 option = 2 contracts ($500 debit == cap): submits,
    and the payload carries 2 contracts (the true count), not 200 'shares'."""
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    result = broker.submit_signal(
        _FakeOrderManager(), _long_call_signal(), limit_price=2.50, mode="live",
        portfolio=None, daily_summary={}, amount_usd=500.0, days_to_expiry=30,
    )
    assert result["submitted"] is True
    assert len(connector.place_calls) == 1
    assert connector.place_calls[0]["quantity"] == "2"


# --- caps-at-submit: the DEBIT cap keys off leg polarity, not the caller's label
# A long (buy-to-open) mislabeled direction='credit' must not escape the per-trade
# debit cap: debit_premium_usd returns $0 for a credit order, and defined risk has
# already refused every opening short at the submit path, so any buy leg is a real
# debit and the cap must fire on it.


def test_broker_refuses_a_long_mislabeled_credit_over_the_debit_cap(monkeypatch, tmp_path):
    """MUTATION TEST: fully armed + confirmed live, a single buy-to-open leg at
    $6.00 (= $600 debit, over the $500 cap) labeled direction='credit'. The debit
    cap must still fire (status 'options_risk_gate_blocked', human_gate naming the
    debit cap) and the connector must never be reached. Revert caps_direction and
    the 'credit' label voids the debit cap and the $600 long submits."""
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)

    result = broker.submit_option_order(
        legs=[LONG_LEG], direction="credit", quantity="1", price="6.00", days_to_expiry=30, mode="live"
    )

    assert result["submitted"] is False
    assert result["status"] == "options_risk_gate_blocked"
    assert "max_debit_premium_per_trade" in result["human_gate"]
    assert connector.place_calls == []
