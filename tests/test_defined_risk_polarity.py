"""Fail-CLOSED polarity of the defined-risk floor.

The re-audit found the floor checked only `side=='sell' and effect=='open'`, so a
sell leg with a MISSING/blank/synonym position_effect (stringifies to 'none') was
NOT refused and a naked short could submit. Both the runtime validator and the
static classifier must refuse ANY sell that is not provably sell-to-close. These
tests inject exactly the effect-less / blank / opening sell the shipped suite
never did -- they fail if the polarity is reverted.
"""
from __future__ import annotations

import pytest

from src.option_risk_gates import STRATEGY_UNSUPPORTED, classify_strategy
from src.robinhood_option_client import DefinedRiskViolationError, assert_defined_risk


def _sell_leg(effect):
    leg = {"side": "sell", "option": "AAPL240920C00200000", "ratio_quantity": 1}
    if effect is not None:
        leg["position_effect"] = effect
    return leg


@pytest.mark.parametrize("effect", [None, "", "none", "open", "opening", "OPEN", "o"])
def test_any_sell_not_provably_close_is_refused(effect):
    with pytest.raises(DefinedRiskViolationError):
        assert_defined_risk([_sell_leg(effect)], "credit")


@pytest.mark.parametrize("effect", [None, "", "none", "open", "opening"])
def test_classify_marks_an_effectless_sell_unsupported(effect):
    assert classify_strategy([_sell_leg(effect)]) == STRATEGY_UNSUPPORTED


def test_a_naked_ratio_is_refused_by_the_floor():
    legs = [
        {"side": "buy", "position_effect": "open", "option": "AAPL240920C00200000", "ratio_quantity": 1},
        {"side": "sell", "option": "AAPL240920C00205000", "ratio_quantity": 3},  # no effect
    ]
    with pytest.raises(DefinedRiskViolationError):
        assert_defined_risk(legs, "credit")


def test_a_genuine_sell_to_close_is_still_allowed():
    # Exiting a held long must NOT be trapped.
    assert_defined_risk(
        [{"side": "sell", "position_effect": "close", "option": "AAPL240920C00200000", "ratio_quantity": 1}],
        "credit",
    )


def test_a_single_opening_long_still_passes():
    assert_defined_risk(
        [{"side": "buy", "position_effect": "open", "option": "AAPL240920C00200000", "ratio_quantity": 1}],
        "debit",
    )
