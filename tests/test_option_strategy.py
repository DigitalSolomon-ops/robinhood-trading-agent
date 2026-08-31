"""The option-strategy mapper (src/option_strategy.py) turns the Options Scout's
ranked plays into DEFINED-RISK order candidates the options RiskManager evaluates,
skips low-conviction / gate-failing / unpriceable plays with a NAMED logged reason,
and writes a readable rationale for EVERY decision -- while never submitting.

Every test is written to FAIL if the behaviour it exercises is reverted:

  * a clean high-conviction play that clears every gate becomes an actionable
    candidate (drop the act path and it disappears);
  * a play under the conviction floor is SKIPPED and never mapped (drop the
    conviction check and it acts -- test fails);
  * a play the risk gates block (over-cap debit / DTE floor / fail-closed level)
    is SKIPPED carrying the gate's own named reason (ignore the risk decision and
    it acts -- test fails);
  * an unpriceable play (no contract / no premium / no expiry) is SKIPPED, never
    crashes and never acts;
  * EVERY decision is written to the audit log exactly once -- act as
    'option_play_selected', skip as 'option_play_skipped' (drop the logging and
    the recorded count drops);
  * the running premium-at-risk is threaded forward so a batch cannot exceed the
    total-at-risk cap (revert the threading and a second over-cap order slips in);
  * the mapper is analysis/decision-only: no connector, no broker, no submit.

No live Robinhood call is ever made; there is no connector here at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from src.option_risk_gates import GateName, OptionRiskConfig
from src.option_strategy import (
    ACTION_SELECTED,
    ACTION_SKIPPED,
    OptionStrategyConfig,
    SkipReason,
    actionable_candidates,
    map_play,
    plan_from_rules,
    plan_option_orders,
)

TODAY = date(2026, 8, 31)


@dataclass
class StubPlay:
    """A lightweight stand-in for an options_scout Play -- only the attributes the
    mapper reads, so a test can dial one field without building 20."""

    symbol: str = "AAPL"
    direction: str = "call"
    reference_close: float = 100.0
    entry: float = 100.0
    ceiling: float = 110.0
    floor: float = 95.0
    conviction: float = 80.0
    rank_score: float = 0.40
    strike: float | None = 105.0
    expiry_date: str | None = "2026-10-16"  # ~46 days out from TODAY
    contract_ticker: str | None = "O:AAPL261016C00105000"
    premium: float | None = 2.50


class FakeLogger:
    """Records every log_decision call; the only witness that a decision was
    audited. Shaped like SQLiteLogger.log_decision."""

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str, str, dict]] = []

    def log_decision(self, symbol, action, reason, details=None):
        self.calls.append((symbol, action, reason, details or {}))


# A generous risk config so a test isolating ONE cause does not trip another gate.
LOOSE_RISK = OptionRiskConfig(
    max_debit_premium_per_trade_usd=10_000.0,
    max_total_premium_at_risk_usd=100_000.0,
    contract_multiplier=100,
    min_days_to_expiry=2,
    allow_zero_dte=False,
    max_contracts_per_order=10,
)
LOOSE_STRATEGY = OptionStrategyConfig(min_conviction=50.0, min_rank_score=0.0, contracts_per_order=1)
GRANTED_LEVEL = 3  # enough for single_leg_long (needs 2)


def _map(play, *, risk=LOOSE_RISK, strat=LOOSE_STRATEGY, level=GRANTED_LEVEL, open_at_risk=0.0):
    return map_play(play, risk, strat, level, open_premium_at_risk_usd=open_at_risk, today=TODAY)


# --- act path -----------------------------------------------------------------


def test_clean_play_becomes_defined_risk_candidate():
    decision = _map(StubPlay())
    assert decision.acted
    candidate = decision.candidate
    assert candidate is not None
    # Single-leg long, debit, defined risk by construction.
    assert candidate.order_direction == "debit"
    assert candidate.leg["side"] == "buy" and candidate.leg["position_effect"] == "open"
    assert candidate.contract_ticker == "O:AAPL261016C00105000"
    assert candidate.quantity == 1
    assert candidate.limit_price == pytest.approx(2.50)
    # max loss = premium x 100 x qty (the defined risk of a long option).
    assert candidate.max_loss_usd == pytest.approx(250.0)
    assert "ACT" in decision.rationale and "max loss" in decision.rationale


def test_act_rationale_carries_option_levels_and_conviction():
    decision = _map(StubPlay(conviction=77.0))
    r = decision.rationale
    assert "AAPL" in r and "target 110.00" in r and "stop 95.00" in r
    assert "Conviction 77.0" in r


# --- conviction / rank floors -------------------------------------------------


def test_low_conviction_is_skipped_not_mapped():
    decision = _map(StubPlay(conviction=40.0))  # below the 50 floor
    assert not decision.acted
    assert decision.candidate is None
    assert decision.reason_code == SkipReason.LOW_CONVICTION
    assert "below the 50.0 floor" in decision.rationale


def test_conviction_exactly_at_floor_acts():
    # A strict '<' floor: exactly-at-floor must pass, not be skipped.
    decision = _map(StubPlay(conviction=50.0))
    assert decision.acted


def test_low_rank_is_skipped():
    strat = OptionStrategyConfig(min_conviction=50.0, min_rank_score=0.30, contracts_per_order=1)
    decision = _map(StubPlay(rank_score=0.10), strat=strat)
    assert not decision.acted
    assert decision.reason_code == SkipReason.LOW_RANK


# --- risk-gate skips (the RiskManager for this lane) --------------------------


def test_over_cap_debit_is_skipped_with_gate_name():
    tight = OptionRiskConfig(
        max_debit_premium_per_trade_usd=100.0,  # 2.50 x 100 = 250 > 100
        max_total_premium_at_risk_usd=100_000.0,
        min_days_to_expiry=2,
        max_contracts_per_order=10,
    )
    decision = _map(StubPlay(), risk=tight)
    assert not decision.acted
    assert decision.reason_code == SkipReason.RISK_GATE
    assert GateName.MAX_DEBIT_PREMIUM in decision.blocking_gates
    assert GateName.MAX_DEBIT_PREMIUM in decision.rationale


def test_sub_floor_dte_is_skipped():
    decision = _map(StubPlay(expiry_date="2026-09-01"))  # 1 day out, floor is 2
    assert not decision.acted
    assert decision.reason_code == SkipReason.RISK_GATE
    assert GateName.MIN_DTE in decision.blocking_gates


def test_unknown_level_fails_closed():
    # granted_level None must fail the approval-level gate CLOSED -> skip.
    decision = _map(StubPlay(), level=None)
    assert not decision.acted
    assert GateName.OPTION_APPROVAL_LEVEL in decision.blocking_gates


# --- unpriceable / structural skips (never crash, never act) ------------------


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("contract_ticker", None, SkipReason.NO_CONTRACT),
        ("premium", None, SkipReason.NO_PREMIUM),
        ("premium", 0.0, SkipReason.NO_PREMIUM),
        ("expiry_date", None, SkipReason.NO_EXPIRY),
        ("expiry_date", "not-a-date", SkipReason.NO_EXPIRY),
        ("direction", "spread", SkipReason.BAD_DIRECTION),
    ],
)
def test_unpriceable_play_is_skipped(field, value, code):
    decision = _map(StubPlay(**{field: value}))
    assert not decision.acted
    assert decision.candidate is None
    assert decision.reason_code == code


# --- audit: every decision is logged exactly once -----------------------------


def test_every_decision_is_logged_once_with_the_right_action():
    plays = [
        StubPlay(symbol="GOOD", conviction=80.0),          # acts
        StubPlay(symbol="WEAK", conviction=10.0),          # skip: conviction
        StubPlay(symbol="NOCON", contract_ticker=None),    # skip: no contract
    ]
    logger = FakeLogger()
    decisions = plan_option_orders(plays, LOOSE_RISK, LOOSE_STRATEGY, GRANTED_LEVEL, logger=logger, today=TODAY)
    assert len(decisions) == 3
    assert len(logger.calls) == 3  # every decision audited, none silent

    by_symbol = {c[0]: c for c in logger.calls}
    assert by_symbol["GOOD"][1] == ACTION_SELECTED
    assert by_symbol["WEAK"][1] == ACTION_SKIPPED
    assert by_symbol["NOCON"][1] == ACTION_SKIPPED
    # The acted decision's log carries the mapped contract + sizing in details.
    assert by_symbol["GOOD"][3]["contract"] == "O:AAPL261016C00105000"
    assert by_symbol["GOOD"][3]["max_loss_usd"] == pytest.approx(250.0)
    # A skip records its machine-readable cause.
    assert by_symbol["WEAK"][3]["reason_code"] == SkipReason.LOW_CONVICTION


def test_none_logger_is_a_silent_noop():
    # A caller may want decisions in-memory only; that must not crash.
    decisions = plan_option_orders([StubPlay()], LOOSE_RISK, LOOSE_STRATEGY, GRANTED_LEVEL, today=TODAY)
    assert decisions[0].acted


# --- batch: running premium-at-risk is threaded forward -----------------------


def test_batch_threads_premium_at_risk_and_caps_the_second_order():
    # Each order risks 250 (2.50 x 100). Cap at 400: first fits (250 <= 400),
    # second would push the batch to 500 -> must be blocked by the total cap.
    risk = OptionRiskConfig(
        max_debit_premium_per_trade_usd=10_000.0,
        max_total_premium_at_risk_usd=400.0,
        min_days_to_expiry=2,
        max_contracts_per_order=10,
    )
    plays = [StubPlay(symbol="ONE"), StubPlay(symbol="TWO")]
    decisions = plan_option_orders(plays, risk, LOOSE_STRATEGY, GRANTED_LEVEL, today=TODAY)
    assert decisions[0].acted
    assert not decisions[1].acted
    assert GateName.MAX_TOTAL_PREMIUM_AT_RISK in decisions[1].blocking_gates


def test_batch_without_threading_would_pass_both_control():
    # Same two orders under a cap that fits BOTH (600 > 500): both act. Proves the
    # previous test's second block is the threading, not a blanket refusal.
    risk = OptionRiskConfig(
        max_debit_premium_per_trade_usd=10_000.0,
        max_total_premium_at_risk_usd=600.0,
        min_days_to_expiry=2,
        max_contracts_per_order=10,
    )
    plays = [StubPlay(symbol="ONE"), StubPlay(symbol="TWO")]
    decisions = plan_option_orders(plays, risk, LOOSE_STRATEGY, GRANTED_LEVEL, today=TODAY)
    assert decisions[0].acted and decisions[1].acted
    assert len(actionable_candidates(decisions)) == 2


# --- config-driven from rules -------------------------------------------------


def test_plan_from_rules_reads_both_config_sections():
    rules = {
        "options": {
            "risk": {"max_debit_premium_per_trade_usd": 100.0, "min_days_to_expiry": 2},
            "strategy": {"min_conviction": 90.0},
        }
    }
    # conviction 80 < the from-rules floor of 90 -> skip on conviction.
    decisions = plan_from_rules([StubPlay(conviction=80.0)], rules, GRANTED_LEVEL, today=TODAY)
    assert decisions[0].reason_code == SkipReason.LOW_CONVICTION


def test_strategy_config_defaults_when_section_absent():
    cfg = OptionStrategyConfig.from_rules({"options": {}})
    assert cfg.min_conviction == 50.0
    assert cfg.contracts_per_order == 1
