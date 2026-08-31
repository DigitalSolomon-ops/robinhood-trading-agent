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

# The agent-tradable account carries the SAME ground-truth identity the equities
# and options clients pin: nickname "Agentic", number ending 2092.
AGENT_ACCOUNT = {"account_number": "RH-OPT-AGENTIC-2092", "nickname": "Agentic", "agentic_allowed": True}
DEFAULT_ACCOUNT = {"account_number": "RH-OPT-DEFAULT-2833", "nickname": "Default", "agentic_allowed": False}
EXPECTED_ACCOUNT = {"nickname": "Agentic", "number_suffix": "2092"}

LONG_CALL = "OPT-XYZ-CALL-LONG"
SHORT_CALL = "OPT-XYZ-CALL-SHORT"

LONG_LEG = {"side": "buy", "position_effect": "open", "ratio_quantity": 1, "option": LONG_CALL}
SHORT_LEG = {"side": "sell", "position_effect": "open", "ratio_quantity": 1, "option": SHORT_CALL}


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

    def __init__(self, accounts: list[dict] | None = None) -> None:
        self.accounts = accounts if accounts is not None else [AGENT_ACCOUNT, DEFAULT_ACCOUNT]
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
        return {"option_level": "level_3"}

    def review_option_order(self, **kwargs):
        self.review_calls.append(kwargs)
        return {"reviewed": True, **kwargs}

    def place_option_order(self, **kwargs):
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

    result = broker.submit_option_order(legs=[LONG_LEG], direction="debit", mode="live")

    assert result["submitted"] is True
    assert result["status"] == "submitted"
    # The witness: exactly one order reached the mocked connector.
    assert len(connector.place_calls) == 1


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


# --- property: STOP_TRADING_OPTIONS blocks at the irreversible moment ---------


def test_stop_trading_options_file_blocks(monkeypatch, tmp_path):
    connector = FakeConnector()
    stop = tmp_path / "STOP_TRADING_OPTIONS"
    stop.write_text("halted", encoding="utf-8")
    # Fully armed + confirmed, but the kill switch's stop file exists.
    broker = make_broker(connector, stop_file=stop, monkeypatch=monkeypatch, tmp_path=tmp_path)

    with pytest.raises(RuntimeError, match="kill switch is engaged"):
        broker.submit_option_order(legs=[LONG_LEG], direction="debit", mode="live")
    assert connector.place_calls == []


# --- property: TRADING_ENABLED=false blocks -----------------------------------


def test_trading_enabled_false_blocks(monkeypatch, tmp_path):
    connector = FakeConnector()
    # Fully armed + confirmed, no stop file, but TRADING_ENABLED is not true.
    broker = make_broker(connector, trading_enabled="false", monkeypatch=monkeypatch, tmp_path=tmp_path)

    with pytest.raises(RuntimeError, match="TRADING_ENABLED=false"):
        broker.submit_option_order(legs=[LONG_LEG], direction="debit", mode="live")
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


def test_defined_risk_vertical_spread_submits(monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    # A long + short vertical: the short is covered by the long, so it is
    # defined-risk and allowed.
    result = broker.submit_option_order(legs=[LONG_LEG, SHORT_LEG], direction="debit", mode="live")
    assert result["submitted"] is True
    assert len(connector.place_calls) == 1


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


def test_broker_defined_risk_guard_allows_long_and_spread(monkeypatch, tmp_path):
    connector = FakeConnector()
    broker = make_broker(connector, monkeypatch=monkeypatch, tmp_path=tmp_path)
    # A lone long leg and a covered vertical must both pass the guard (no raise).
    broker._assert_defined_risk([LONG_LEG], "debit")
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
    result = broker.place_long_call(LONG_CALL, mode="live")
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
