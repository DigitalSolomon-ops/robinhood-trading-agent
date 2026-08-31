"""The options-lane risk gates (src/option_risk_gates.py) block each of their
violations with a NAMED reason, are driven entirely by config, and read the
account's granted approval level from a MOCKED connector -- never a live call.

Every test is written to FAIL if the gate it exercises is reverted:

  * max debit premium: an over-cap debit is blocked by name; drop the compare
    and it submits;
  * max total premium at risk: this order's max loss plus already-open premium
    is capped; drop the addition and an over-cap order slips through;
  * min DTE floor + 0DTE: a same-day expiry is refused by default and a sub-floor
    expiry is refused by name; flip either check and they pass;
  * max contracts per order: an over-cap count is blocked by name;
  * option approval level: a strategy needing a higher level than granted is
    refused, an UNKNOWN level fails closed, and the level is parsed from the
    connector's get_option_level_upgrade_info, which is mocked.

The named reasons are asserted explicitly, so weakening a gate to "pass but warn"
also fails these. No connector is ever the real Robinhood one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.option_risk_gates import (
    DEFAULT_CONTRACT_MULTIPLIER,
    GateName,
    OptionOrderProposal,
    OptionRiskConfig,
    classify_strategy,
    evaluate_option_order,
    parse_option_level,
    resolve_granted_level,
    STRATEGY_DEFINED_RISK_SPREAD,
    STRATEGY_LONG_MULTI_LEG,
    STRATEGY_REDUCING,
    STRATEGY_SINGLE_LEG_LONG,
    STRATEGY_UNSUPPORTED,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

LONG_LEG = {"side": "buy", "position_effect": "open", "ratio_quantity": 1, "option": "LONG"}
SHORT_LEG = {"side": "sell", "position_effect": "open", "ratio_quantity": 1, "option": "SHORT"}
CLOSE_LONG_LEG = {"side": "sell", "position_effect": "close", "ratio_quantity": 1, "option": "LONG"}

# A generous config so a test that means to isolate ONE gate does not trip
# another by accident. Each gate test then tightens the one cap it cares about.
LOOSE = OptionRiskConfig(
    max_debit_premium_per_trade_usd=10_000.0,
    max_total_premium_at_risk_usd=100_000.0,
    contract_multiplier=100,
    min_days_to_expiry=1,
    allow_zero_dte=False,
    max_contracts_per_order=100,
    strategy_min_option_level={
        STRATEGY_REDUCING: 0,
        STRATEGY_SINGLE_LEG_LONG: 2,
        STRATEGY_LONG_MULTI_LEG: 3,
        STRATEGY_DEFINED_RISK_SPREAD: 3,
    },
)

# Granted a high level so the level gate never blocks unless a test means it to.
GRANTED_L3 = 3


def long_call(
    *,
    price: float = 1.00,
    quantity: int = 1,
    dte: int = 10,
    direction: str = "debit",
    max_loss_per_contract_usd: float | None = None,
) -> OptionOrderProposal:
    return OptionOrderProposal(
        legs=[LONG_LEG],
        net_premium_per_contract=price,
        quantity=quantity,
        days_to_expiry=dte,
        direction=direction,
        max_loss_per_contract_usd=max_loss_per_contract_usd,
    )


# --- a mocked connector for the level read -----------------------------------


class FakeLevelConnector:
    """Stands in for the authorized connector's get_option_level_upgrade_info.
    Records that it was asked; never reaches Robinhood."""

    def __init__(self, info) -> None:
        self.info = info
        self.calls = 0

    def get_option_level_upgrade_info(self):
        self.calls += 1
        return self.info


class RaisingLevelConnector:
    def get_option_level_upgrade_info(self):
        raise RuntimeError("connector unavailable")


class BrokerLikeLevelSource:
    """A broker exposes get_level_upgrade_info, not the raw tool name."""

    def __init__(self, info) -> None:
        self.info = info

    def get_level_upgrade_info(self):
        return self.info


# --- config: driven entirely by trading_rules.yaml ---------------------------


def test_from_rules_reads_the_options_section() -> None:
    with (REPO_ROOT / "config" / "trading_rules.yaml").open("r", encoding="utf-8") as handle:
        rules = yaml.safe_load(handle)
    config = OptionRiskConfig.from_rules(rules)

    section = rules["options"]["risk"]
    assert config.max_debit_premium_per_trade_usd == float(section["max_debit_premium_per_trade_usd"])
    assert config.max_total_premium_at_risk_usd == float(section["max_total_premium_at_risk_usd"])
    assert config.min_days_to_expiry == int(section["min_days_to_expiry"])
    assert config.allow_zero_dte == bool(section["allow_zero_dte"])
    assert config.max_contracts_per_order == int(section["max_contracts_per_order"])
    assert config.strategy_min_option_level[STRATEGY_SINGLE_LEG_LONG] == 2
    assert config.strategy_min_option_level[STRATEGY_DEFINED_RISK_SPREAD] == 3


def test_from_rules_defaults_when_section_missing() -> None:
    config = OptionRiskConfig.from_rules({})
    assert config.max_debit_premium_per_trade_usd == 500.0
    assert config.min_days_to_expiry == 2
    assert config.allow_zero_dte is False
    assert config.max_contracts_per_order == 5
    assert config.contract_multiplier == DEFAULT_CONTRACT_MULTIPLIER
    # A missing section is the conservative default, never a permissive blank.
    assert config.min_level_for(STRATEGY_SINGLE_LEG_LONG) == 2


def test_from_rules_ignores_bad_types_and_keeps_the_default() -> None:
    config = OptionRiskConfig.from_rules({"options": {"risk": {"max_contracts_per_order": "lots"}}})
    assert config.max_contracts_per_order == 5


# --- max debit premium per trade ---------------------------------------------


def test_debit_premium_over_cap_is_blocked_by_name() -> None:
    config = OptionRiskConfig(**{**LOOSE.__dict__, "max_debit_premium_per_trade_usd": 300.0})
    # $2.00 * 100 * 2 contracts = $400 paid, over the $300 cap.
    decision = evaluate_option_order(config, long_call(price=2.00, quantity=2), GRANTED_L3)
    assert decision.allowed is False
    assert GateName.MAX_DEBIT_PREMIUM in decision.blocking_names


def test_debit_premium_within_cap_allows() -> None:
    config = OptionRiskConfig(**{**LOOSE.__dict__, "max_debit_premium_per_trade_usd": 300.0})
    # $1.00 * 100 * 2 = $200, under the cap.
    decision = evaluate_option_order(config, long_call(price=1.00, quantity=2), GRANTED_L3)
    assert decision.allowed is True
    assert decision.blocking_names == []


def test_credit_order_pays_no_debit_premium() -> None:
    # A defined-risk credit spread collects premium; the debit cap must not fire
    # on it however high the per-contract price is.
    config = OptionRiskConfig(**{**LOOSE.__dict__, "max_debit_premium_per_trade_usd": 50.0})
    proposal = OptionOrderProposal(
        legs=[LONG_LEG, SHORT_LEG],
        net_premium_per_contract=5.00,
        quantity=1,
        days_to_expiry=10,
        direction="credit",
        max_loss_per_contract_usd=100.0,
    )
    decision = evaluate_option_order(config, proposal, GRANTED_L3)
    assert GateName.MAX_DEBIT_PREMIUM not in decision.blocking_names


# --- max total premium at risk -----------------------------------------------


def test_total_at_risk_over_cap_counting_open_positions_is_blocked() -> None:
    config = OptionRiskConfig(**{**LOOSE.__dict__, "max_total_premium_at_risk_usd": 1000.0})
    # New order risks $300 ($3.00*100*1); $800 already open -> $1100 total > $1000.
    decision = evaluate_option_order(
        config, long_call(price=3.00, quantity=1), GRANTED_L3, open_premium_at_risk_usd=800.0
    )
    assert decision.allowed is False
    assert GateName.MAX_TOTAL_PREMIUM_AT_RISK in decision.blocking_names


def test_total_at_risk_counts_the_open_book_not_just_this_order() -> None:
    # Same order in isolation is fine; only the open book pushes it over. Drop the
    # addition of open_premium_at_risk_usd and this order would be allowed.
    config = OptionRiskConfig(**{**LOOSE.__dict__, "max_total_premium_at_risk_usd": 400.0})
    alone = evaluate_option_order(config, long_call(price=3.00, quantity=1), GRANTED_L3)
    assert GateName.MAX_TOTAL_PREMIUM_AT_RISK not in alone.blocking_names
    with_book = evaluate_option_order(
        config, long_call(price=3.00, quantity=1), GRANTED_L3, open_premium_at_risk_usd=200.0
    )
    assert GateName.MAX_TOTAL_PREMIUM_AT_RISK in with_book.blocking_names


def test_explicit_max_loss_drives_at_risk_for_a_credit_spread() -> None:
    # A credit spread's max loss is width - credit, supplied explicitly; the gate
    # must use it, not the (zero) debit.
    config = OptionRiskConfig(**{**LOOSE.__dict__, "max_total_premium_at_risk_usd": 150.0})
    proposal = OptionOrderProposal(
        legs=[LONG_LEG, SHORT_LEG],
        net_premium_per_contract=0.50,
        quantity=1,
        days_to_expiry=10,
        direction="credit",
        max_loss_per_contract_usd=200.0,  # $200 at risk > $150 cap
    )
    decision = evaluate_option_order(config, proposal, GRANTED_L3)
    assert GateName.MAX_TOTAL_PREMIUM_AT_RISK in decision.blocking_names


# --- min DTE floor and 0DTE --------------------------------------------------


def test_zero_dte_is_blocked_by_default() -> None:
    config = OptionRiskConfig(**{**LOOSE.__dict__, "min_days_to_expiry": 0, "allow_zero_dte": False})
    decision = evaluate_option_order(config, long_call(dte=0), GRANTED_L3)
    assert decision.allowed is False
    assert GateName.ZERO_DTE in decision.blocking_names


def test_zero_dte_allowed_only_when_opted_in() -> None:
    config = OptionRiskConfig(**{**LOOSE.__dict__, "min_days_to_expiry": 0, "allow_zero_dte": True})
    decision = evaluate_option_order(config, long_call(dte=0), GRANTED_L3)
    assert GateName.ZERO_DTE not in decision.blocking_names
    assert decision.allowed is True


def test_under_the_dte_floor_is_blocked_by_name() -> None:
    config = OptionRiskConfig(**{**LOOSE.__dict__, "min_days_to_expiry": 3})
    decision = evaluate_option_order(config, long_call(dte=2), GRANTED_L3)
    assert decision.allowed is False
    assert GateName.MIN_DTE in decision.blocking_names


def test_at_the_dte_floor_is_allowed() -> None:
    config = OptionRiskConfig(**{**LOOSE.__dict__, "min_days_to_expiry": 3})
    decision = evaluate_option_order(config, long_call(dte=3), GRANTED_L3)
    assert GateName.MIN_DTE not in decision.blocking_names


def test_an_expired_contract_is_blocked() -> None:
    decision = evaluate_option_order(LOOSE, long_call(dte=-1), GRANTED_L3)
    assert decision.allowed is False
    assert GateName.EXPIRED in decision.blocking_names


# --- max contracts per order -------------------------------------------------


def test_over_the_contract_cap_is_blocked_by_name() -> None:
    config = OptionRiskConfig(**{**LOOSE.__dict__, "max_contracts_per_order": 5})
    decision = evaluate_option_order(config, long_call(quantity=6), GRANTED_L3)
    assert decision.allowed is False
    assert GateName.MAX_CONTRACTS in decision.blocking_names


def test_at_the_contract_cap_is_allowed() -> None:
    config = OptionRiskConfig(**{**LOOSE.__dict__, "max_contracts_per_order": 5})
    decision = evaluate_option_order(config, long_call(quantity=5), GRANTED_L3)
    assert GateName.MAX_CONTRACTS not in decision.blocking_names


def test_a_nonpositive_quantity_is_refused_as_invalid() -> None:
    decision = evaluate_option_order(LOOSE, long_call(quantity=0), GRANTED_L3)
    assert decision.allowed is False
    assert GateName.INVALID_PROPOSAL in decision.blocking_names


# --- option approval level ---------------------------------------------------


def test_single_long_needs_level_2_and_is_blocked_at_level_1() -> None:
    decision = evaluate_option_order(LOOSE, long_call(), granted_level=1)
    assert decision.strategy == STRATEGY_SINGLE_LEG_LONG
    assert decision.allowed is False
    assert GateName.OPTION_APPROVAL_LEVEL in decision.blocking_names


def test_single_long_is_allowed_at_level_2() -> None:
    decision = evaluate_option_order(LOOSE, long_call(), granted_level=2)
    assert GateName.OPTION_APPROVAL_LEVEL not in decision.blocking_names
    assert decision.allowed is True


def test_a_long_plus_short_is_unsupported_and_blocked_by_the_level_gate() -> None:
    """MUTATION TEST: a long + short opening pair is NO LONGER a defined-risk
    spread. The short is an opening sell the lane cannot prove covered, so it
    classifies as UNSUPPORTED and the level gate refuses it even at level 3 --
    the highest the lane trades. Revert classify_strategy to return
    defined_risk_spread (min level 3, granted) and the order would be allowed,
    failing this."""
    proposal = OptionOrderProposal(
        legs=[LONG_LEG, SHORT_LEG], net_premium_per_contract=1.00, quantity=1, days_to_expiry=10
    )
    decision = evaluate_option_order(LOOSE, proposal, granted_level=3)
    assert decision.strategy == STRATEGY_UNSUPPORTED
    assert decision.allowed is False
    assert GateName.OPTION_APPROVAL_LEVEL in decision.blocking_names


def test_an_unknown_granted_level_fails_closed() -> None:
    decision = evaluate_option_order(LOOSE, long_call(), granted_level=None)
    assert decision.allowed is False
    assert GateName.OPTION_APPROVAL_LEVEL in decision.blocking_names


def test_an_unsupported_strategy_demands_a_level_the_lane_never_grants() -> None:
    # A lone opening sell (the defined-risk guard refuses this earlier). Even at
    # the highest level this lane trades, the level gate refuses it -- fail closed.
    proposal = OptionOrderProposal(
        legs=[SHORT_LEG], net_premium_per_contract=1.00, quantity=1, days_to_expiry=10, direction="credit"
    )
    decision = evaluate_option_order(LOOSE, proposal, granted_level=3)
    assert decision.strategy == STRATEGY_UNSUPPORTED
    assert GateName.OPTION_APPROVAL_LEVEL in decision.blocking_names


# --- classification ----------------------------------------------------------


def test_classify_single_long() -> None:
    assert classify_strategy([LONG_LEG]) == STRATEGY_SINGLE_LEG_LONG


def test_classify_two_longs_is_multi_leg() -> None:
    assert classify_strategy([LONG_LEG, dict(LONG_LEG)]) == STRATEGY_LONG_MULTI_LEG


def test_classify_long_plus_short_is_unsupported() -> None:
    """MUTATION TEST: a long + short is no longer a defined-risk spread. Without
    strike-aware coverage the opening sell cannot be proven covered, so the pair
    classifies as UNSUPPORTED. Revert classify_strategy and this fails."""
    assert classify_strategy([LONG_LEG, SHORT_LEG]) == STRATEGY_UNSUPPORTED


def test_classify_ratio_one_long_many_shorts_is_unsupported() -> None:
    """A 10:1 ratio (buy 1, sell 10) has a long leg but the extra shorts are
    uncovered. Presence of a buy no longer buys a defined-risk verdict."""
    ratio = [LONG_LEG, {"side": "sell", "position_effect": "open", "ratio_quantity": 10, "option": "SHORT"}]
    assert classify_strategy(ratio) == STRATEGY_UNSUPPORTED


def test_classify_short_call_covered_by_long_put_is_unsupported() -> None:
    """A short call 'covered' by a long put: the put does not cover the call, so
    the opening sell is uncovered and the pair is unsupported."""
    long_put = {"side": "buy", "position_effect": "open", "ratio_quantity": 1, "option_type": "put", "option": "P"}
    short_call = {"side": "sell", "position_effect": "open", "ratio_quantity": 1, "option_type": "call", "option": "C"}
    assert classify_strategy([long_put, short_call]) == STRATEGY_UNSUPPORTED


def test_classify_closing_only_is_reducing() -> None:
    assert classify_strategy([CLOSE_LONG_LEG]) == STRATEGY_REDUCING


def test_classify_lone_opening_sell_is_unsupported() -> None:
    assert classify_strategy([SHORT_LEG]) == STRATEGY_UNSUPPORTED


def test_classify_empty_is_unsupported() -> None:
    assert classify_strategy([]) == STRATEGY_UNSUPPORTED


# --- level parsing (connector MOCKED) ----------------------------------------


@pytest.mark.parametrize(
    "info, expected",
    [
        ("level_3", 3),
        (3, 3),
        ("3", 3),
        ({"option_level": "level_2"}, 2),
        ({"current_option_level": "3"}, 3),
        ({"option_level": 4}, 4),
        ({"account": {"option_level": "level_2"}}, 2),
        ({"unrelated": "x"}, None),
        (None, None),
        (True, None),
    ],
)
def test_parse_option_level_covers_the_known_shapes(info, expected) -> None:
    assert parse_option_level(info) == expected


def test_resolve_granted_level_reads_from_a_mocked_connector() -> None:
    connector = FakeLevelConnector({"option_level": "level_3"})
    assert resolve_granted_level(connector) == 3
    assert connector.calls == 1


def test_resolve_granted_level_reads_a_broker_like_source() -> None:
    assert resolve_granted_level(BrokerLikeLevelSource({"option_level": 2})) == 2


def test_resolve_granted_level_from_a_raw_mapping() -> None:
    assert resolve_granted_level({"current_option_level": "1"}) == 1


def test_resolve_granted_level_fails_closed_when_the_connector_raises() -> None:
    assert resolve_granted_level(RaisingLevelConnector()) is None


# --- the combined decision ---------------------------------------------------


def test_a_clean_order_passes_every_gate() -> None:
    config = OptionRiskConfig.from_rules(
        {
            "options": {
                "risk": {
                    "max_debit_premium_per_trade_usd": 500.0,
                    "max_total_premium_at_risk_usd": 1500.0,
                    "min_days_to_expiry": 2,
                    "allow_zero_dte": False,
                    "max_contracts_per_order": 5,
                }
            }
        }
    )
    decision = evaluate_option_order(config, long_call(price=1.00, quantity=1, dte=10), granted_level=3)
    assert decision.allowed is True
    assert decision.blocking == []
    assert decision.reason == "all options risk gates passed"


def test_multiple_violations_each_carry_their_own_named_reason() -> None:
    config = OptionRiskConfig(
        max_debit_premium_per_trade_usd=100.0,
        max_total_premium_at_risk_usd=100.0,
        contract_multiplier=100,
        min_days_to_expiry=5,
        allow_zero_dte=False,
        max_contracts_per_order=2,
        strategy_min_option_level={STRATEGY_SINGLE_LEG_LONG: 2},
    )
    # $5.00*100*10 = $5000 debit, 10 contracts, 0 DTE, granted level 1.
    decision = evaluate_option_order(config, long_call(price=5.00, quantity=10, dte=0), granted_level=1)
    assert decision.allowed is False
    names = set(decision.blocking_names)
    assert {
        GateName.MAX_DEBIT_PREMIUM,
        GateName.MAX_TOTAL_PREMIUM_AT_RISK,
        GateName.ZERO_DTE,
        GateName.MAX_CONTRACTS,
        GateName.OPTION_APPROVAL_LEVEL,
    } <= names
    # The reason line names each blocking gate.
    for name in names:
        assert name in decision.reason
