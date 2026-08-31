"""The OPTIONS lane's live-readiness gate -- the analog of equity_readiness.py's
`equity_live_readiness`, for the DEFINED-RISK agentic options lane.

WHAT THIS IS FOR
    Before anyone arms the options lane for a real order, one command has to
    answer a single question honestly: is every safety property this lane claims
    to have actually PROVEN right now, on this tree, in this checkout? Not "was it
    proven when it was written" -- proven by a run that just happened.

HOW A GATE IS PROVEN (identical design to the equities gate, reused verbatim)
    A gate is proven by NAMED TESTS THAT MUST EXIST AND MUST PASS. Each gate
    below carries the pytest node ids that demonstrate it. The report runs the
    project's full suite once (the SAME suite runner the equities gate uses), and
    then asks of each gate: are all of my proving tests present in that run, and
    did all of them pass? A test that FAILED and a test that is ABSENT are
    indistinguishable -- both leave the gate unproven -- so deleting the test that
    proves a gate can never turn a red gate green.

    A few gates carry additional non-test EVIDENCE no unit test can speak to --
    what the options caps are actually set to right now, whether the paper lane
    really did run twice unattended and reconcile clean on a real basis, whether
    the account anchor is still the single agent-tradable account, and whether any
    credential-shaped literal has appeared in the source. A gate with evidence
    passes only if BOTH its tests and its evidence pass.

WHAT ready:true MEANS
    Every gate proven, the full suite green, and the suite non-vacuous.
    `readiness_verdict` is a pure function of the suite result and the gate rows,
    and it re-asserts the two conditions that must never be negotiable -- suite
    green, and the OPTION ORDER-SAFETY guard proven (no ungated submit, no naked
    short) -- after the generic per-gate loop, so no future gate-table edit can
    route around them.

WHAT THIS DELIBERATELY DOES NOT DO
    It does not touch the connector, does not place, review or cancel an order,
    and does not change any posture -- arming the options lane stays the
    operator's explicit call and is never a side effect of running this. It reuses
    the equities lane's suite runner, secret scan and git-hygiene reader unchanged
    (the tree is one tree), and adds only the options-specific evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

# Reuse the equities readiness machinery verbatim -- the suite runner, the gate
# dataclass and evaluator, the secret scan and the git-hygiene reader all read
# the SAME tree and must not be forked (a second copy is a second thing to keep
# in sync, and the whole point of the gate is that it cannot drift).
from .equity_readiness import (
    Evidence,
    Gate,
    SuiteResult,
    evaluate_gate,
    git_hygiene_evidence,
    hardcoded_account_literals,
    load_rules,
    no_secret_in_code_evidence,
    run_project_test_suite,
)
from .option_runtime import (
    BASIS_CONNECTOR_QUOTE,
    BASIS_EXPECTED_MOVE,
    REAL_OPTION_BASES,
    UNRECORDED_BASIS,
    option_paper_proving_runs,
)
from .robinhood_equity_client import _load_expected_account

VENUE = "robinhood_options"

# The gate whose proving tests are re-asserted by name in readiness_verdict:
# the OPTIONS order-safety guard (tests/test_option_order_safety_guard.py) --
# no ungated live submit, and no naked-short / undefined-risk payload.
OPTION_ORDER_SAFETY_GUARD = "option_order_safety_guard"

# A paper proving run counts only if the options lane completed a BOUNDED loop
# unattended, filled at least one defined-risk order, and the reconcile that
# followed came back clean -- on a REAL basis. Two of them. (The counting itself
# lives in option_runtime.option_paper_proving_runs; this is only the required
# count for the gate.)
REQUIRED_CLEAN_PAPER_RUNS = 2

# The options-lane risk caps this build may not exceed, read as EVIDENCE off
# config/trading_rules.yaml `options.risk` rather than re-implemented as a second
# risk path. Mirrors equity_readiness.CAP_BOUNDS. A cap missing entirely FAILS --
# an absent cap is not a permissive default here.
OPTION_CAP_BOUNDS: tuple[tuple[str, str, float], ...] = (
    ("max_debit_premium_per_trade_usd", "<=", 500.0),
    ("max_total_premium_at_risk_usd", "<=", 1500.0),
    ("min_days_to_expiry", ">=", 2),
    ("max_contracts_per_order", "<=", 5),
)

# The per-strategy option-level FLOORS the lane may not drop below -- a long must
# demand at least level 2, a defined-risk / long multi-leg spread at least level
# 3, so no config edit can quietly let the lane trade a strategy at a lower level
# than Robinhood's own approval tiers require.
OPTION_LEVEL_FLOORS: tuple[tuple[str, int], ...] = (
    ("single_leg_long", 2),
    ("long_multi_leg", 3),
    ("defined_risk_spread", 3),
)

# One equity option contract controls this many shares; a smaller multiplier in
# config would understate every dollar cap, so it is pinned.
REQUIRED_CONTRACT_MULTIPLIER = 100

# The account-identity anchor this lane pins -- the SAME one the equities lane
# uses (config equities.expected_account), reused verbatim, plus the shared
# binding doc that records it.
EXPECTED_NICKNAME = "Agentic"
EXPECTED_NUMBER_SUFFIX = "2092"
BINDING_DOC = Path("docs") / "rh-equities-binding.md"


# ---------------------------------------------------------------------------
# evidence beyond the tests
# ---------------------------------------------------------------------------


def _compare(value: float, operator: str, bound: float) -> bool:
    return value <= bound if operator == "<=" else value >= bound


def option_risk_caps_evidence(root: Path, rules: dict[str, Any]) -> Evidence:
    """The options caps in config/trading_rules.yaml right now, against the bounds
    this lane may not exceed. Reads the RAW `options.risk` section -- not
    OptionRiskConfig.from_rules, whose job is to substitute conservative defaults
    for a missing key, which is exactly the leniency this gate must NOT grant: a
    cap that is absent from config FAILS here, it does not silently default."""
    options = rules.get("options")
    section = options.get("risk") if isinstance(options, dict) else None
    if not isinstance(section, dict):
        return Evidence(False, "config options.risk is missing; the options caps are unconfigured")

    failures: list[str] = []
    observed: dict[str, Any] = {}
    for key, operator, bound in OPTION_CAP_BOUNDS:
        raw = section.get(key)
        observed[key] = raw
        if raw is None:
            failures.append(f"{key} is not configured")
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            failures.append(f"{key}={raw!r} is not a number")
            continue
        if not _compare(value, operator, bound):
            failures.append(f"{key}={value:g} violates {operator} {bound:g}")

    zero_dte = bool(section.get("allow_zero_dte", False))
    observed["allow_zero_dte"] = zero_dte
    if zero_dte:
        failures.append("allow_zero_dte is true; same-day expiry is blocked on this lane")

    multiplier = section.get("contract_multiplier")
    observed["contract_multiplier"] = multiplier
    if multiplier is None:
        failures.append("contract_multiplier is not configured")
    elif int(multiplier) != REQUIRED_CONTRACT_MULTIPLIER:
        failures.append(f"contract_multiplier={multiplier} must be {REQUIRED_CONTRACT_MULTIPLIER}")

    levels = section.get("strategy_min_option_level")
    observed["strategy_min_option_level"] = levels
    if not isinstance(levels, dict):
        failures.append("strategy_min_option_level is not configured; the level floors are unenforced")
    else:
        for strategy, floor in OPTION_LEVEL_FLOORS:
            configured = levels.get(strategy)
            if configured is None:
                failures.append(f"strategy_min_option_level.{strategy} is not configured")
            elif int(configured) < floor:
                failures.append(f"strategy_min_option_level.{strategy}={configured} is under the level-{floor} floor")

    if failures:
        return Evidence(False, "; ".join(failures), observed)
    return Evidence(
        True,
        f"all {len(OPTION_CAP_BOUNDS)} options caps within bounds, 0DTE blocked, "
        f"contract multiplier {REQUIRED_CONTRACT_MULTIPLIER}, level floors enforced "
        f"(long>=2, spread>=3)",
        observed,
    )


def option_account_confinement_evidence(root: Path, rules: dict[str, Any]) -> Evidence:
    """The options lane must pin the SAME single agent-tradable account the
    equities lane does, from the SAME anchor, and no source file may route an
    order to an account by literal.

    Three checks, all static (the account itself is resolved at RUNTIME from the
    connector's own account list -- RobinhoodOptionClient pins the one account
    flagged agentic_allowed AND cross-checks nickname + number suffix -- so there
    is deliberately no account number in the code):

      1. config equities.expected_account still sets the expected identity
         (nickname "Agentic", number ending 2092) -- the anchor the options client
         actually loads and cross-checks. A forked or weakened anchor fails.
      2. the binding doc still records that identity.
      3. nothing under src/ assigns an account number to an order-routing
         argument as a literal (reuses the equities scanner over the same tree).
    """
    failures: list[str] = []
    observed: dict[str, Any] = {}

    try:
        expected = _load_expected_account(root)
    except Exception as exc:  # a missing / malformed expected_account section
        return Evidence(False, f"config equities.expected_account is unusable: {exc}")
    observed["expected_account"] = expected
    if expected.get("nickname") != EXPECTED_NICKNAME or expected.get("number_suffix") != EXPECTED_NUMBER_SUFFIX:
        failures.append(
            f"the account anchor is {expected!r}, not the expected "
            f"(nickname {EXPECTED_NICKNAME!r}, number ending {EXPECTED_NUMBER_SUFFIX!r})"
        )

    doc = root / BINDING_DOC
    if not doc.exists():
        failures.append(f"{BINDING_DOC.as_posix()} is missing; the account binding is unrecorded")
    else:
        text = doc.read_text(encoding="utf-8", errors="replace")
        missing = [phrase for phrase in (f'"{EXPECTED_NICKNAME}"', EXPECTED_NUMBER_SUFFIX) if phrase not in text]
        if missing:
            failures.append(f"the binding doc no longer records: {', '.join(missing)}")

    literals = hardcoded_account_literals(root)
    observed["literals"] = literals
    if literals:
        failures.append(f"an account number is routed to by literal: {'; '.join(literals)}")

    if failures:
        return Evidence(False, "; ".join(failures), observed)
    return Evidence(
        True,
        f'the options lane pins the same single agent-tradable account (nickname "{EXPECTED_NICKNAME}", '
        f"the ..{EXPECTED_NUMBER_SUFFIX} account) as the equities lane, from equities.expected_account; "
        "nothing under src/ routes to an account by literal -- the client resolves and asserts it at runtime",
        observed,
    )


def _options_stop_file(rules: dict[str, Any]) -> str:
    options = rules.get("options") or {}
    return str((options.get("kill_switch") or {}).get("stop_file", "STOP_TRADING_OPTIONS"))


def lane_isolation_evidence(root: Path, rules: dict[str, Any]) -> Evidence:
    """The options lane must have its OWN kill switch, separate from the crypto
    and equities lanes, and this build must leave the crypto lane disarmed.

    STOP_TRADING (crypto) must still exist -- disarmed -- and the options lane's
    stop file must be a DIFFERENT file from both the crypto stop file and the
    equities one, so arming or disarming one lane can never silently touch
    another."""
    crypto_stop = str((rules.get("kill_switch") or {}).get("stop_file", "STOP_TRADING"))
    equities_stop = str(((rules.get("equities") or {}).get("kill_switch") or {}).get("stop_file", "STOP_TRADING_EQUITIES"))
    options_stop = _options_stop_file(rules)
    observed = {"crypto_stop_file": crypto_stop, "equities_stop_file": equities_stop, "options_stop_file": options_stop}

    failures: list[str] = []
    if not (root / crypto_stop).exists():
        failures.append(f"{crypto_stop} is missing; the crypto lane is no longer disarmed")
    if not options_stop:
        failures.append("the options lane has no kill switch stop file")
    else:
        if options_stop == crypto_stop:
            failures.append(f"the options lane shares the crypto stop file {crypto_stop!r}")
        if options_stop == equities_stop:
            failures.append(f"the options lane shares the equities stop file {equities_stop!r}")

    if failures:
        return Evidence(False, "; ".join(failures), observed)
    return Evidence(
        True,
        f"crypto lane still disarmed ({crypto_stop} present); options uses its own {options_stop}, "
        f"separate from crypto ({crypto_stop}) and equities ({equities_stop})",
        observed,
    )


def option_paper_runs_evidence(root: Path, rules: dict[str, Any]) -> Evidence:
    """At least REQUIRED_CLEAN_PAPER_RUNS clean unattended options paper runs, each
    priced on a REAL basis, in the audit log. `clean` is decided by
    option_runtime.option_paper_proving_runs -- a run counts only if it recorded a
    real basis, filled at least one defined-risk order, moved the ledger, and was
    followed by its own clean reconcile."""
    runs = option_paper_proving_runs(root)
    clean = [run for run in runs if run["clean"]]
    wrong_basis = [run for run in runs if not run["basis_is_real"]]
    data = {
        "runs": runs,
        "clean_run_count": len(clean),
        "required": REQUIRED_CLEAN_PAPER_RUNS,
        "real_bases": list(REAL_OPTION_BASES),
        "recorded_bases": recorded_option_bases(root),
    }
    if len(clean) < REQUIRED_CLEAN_PAPER_RUNS:
        detail = (
            f"{len(clean)} clean unattended options paper run(s) on a real basis in the audit log; "
            f"{REQUIRED_CLEAN_PAPER_RUNS} are required"
        )
        if wrong_basis:
            listed = ", ".join(sorted({run["quote_basis"] for run in wrong_basis}))
            detail += (
                f" ({len(wrong_basis)} recorded run(s) were not priced on a real basis -- "
                f"basis/bases: {listed} -- and cannot count)"
            )
        return Evidence(False, detail, data)
    iterations = ", ".join(str(run["iterations_completed"]) for run in clean[-REQUIRED_CLEAN_PAPER_RUNS:])
    return Evidence(
        True,
        f"{len(clean)} unattended bounded options paper run(s) on a real basis "
        f"({' | '.join(REAL_OPTION_BASES)}) filled defined-risk orders and reconciled with no errors "
        f"(latest iteration counts: {iterations})",
        data,
    )


def option_order_safety_guard_evidence(root: Path, rules: dict[str, Any]) -> Evidence:
    """The guard's test file must be on disk. Its tests passing is checked
    separately; this catches the file being deleted outright, which would
    otherwise show up only as absent node ids."""
    path = root / "tests" / "test_option_order_safety_guard.py"
    if not path.exists():
        return Evidence(False, "tests/test_option_order_safety_guard.py is missing; the options order-safety guard is absent")
    return Evidence(True, "tests/test_option_order_safety_guard.py present")


def recorded_option_bases(root: Path) -> dict[str, int]:
    """How many recorded proving runs ran on each price basis, by name -- read
    straight off the audit log so the posture states what the runs were ACTUALLY
    priced on."""
    counts: dict[str, int] = {}
    for run in option_paper_proving_runs(root):
        counts[run["quote_basis"]] = counts.get(run["quote_basis"], 0) + 1
    return dict(sorted(counts.items()))


def option_basis_summary(root: Path) -> str:
    """One honest line about the proving runs' price basis."""
    counts = recorded_option_bases(root)
    real = sum(count for name, count in counts.items() if name in REAL_OPTION_BASES)
    others = {name: count for name, count in counts.items() if name not in REAL_OPTION_BASES}
    if not counts:
        return (
            "no proving run recorded yet; a counted run must be priced on a real basis "
            f"({' or '.join(REAL_OPTION_BASES)})"
        )
    listed = ", ".join(f"{name} x{count}" for name, count in others.items())
    if real and not others:
        return f"real basis ({' | '.join(REAL_OPTION_BASES)}) across all {real} recorded proving run(s)"
    if real:
        return f"mixed: {real} run(s) on a real basis; {listed} NOT counted"
    return f"no run priced on a real basis; recorded bases are {listed}, none of which counts"


# ---------------------------------------------------------------------------
# the gate table
# ---------------------------------------------------------------------------

G = "tests/test_option_order_safety_guard.py::"
Bo = "tests/test_robinhood_option_broker.py::"
Co = "tests/test_robinhood_option_client.py::"
Rg = "tests/test_option_risk_gates.py::"
Ro = "tests/test_option_runtime.py::"
O = "tests/test_option_readiness.py::"


GATES: tuple[Gate, ...] = (
    Gate(
        key=OPTION_ORDER_SAFETY_GUARD,
        name="Options order-safety guard",
        why=(
            "No options execution module may reach the connector's place-order tool without a gate establishing BOTH an "
            "explicit confirm flag AND the options lane armed, and no payload may open a naked / uncovered "
            "short (a sell-to-open leg with no covering buy-to-open leg, or a credit opening order with no "
            "long leg)."
        ),
        proving_tests=(
            G + "test_repo_src_has_no_ungated_option_submit_and_no_naked_short_path",
            G + "test_the_real_scan_is_not_empty",
            G + "test_soundness_check_rejects_an_empty_scan_when_order_tools_exist",
            G + "test_soundness_check_rejects_a_scan_that_missed_an_order_tool_module",
            G + "test_soundness_check_rejects_an_unparsable_module",
            G + "test_an_ungated_submit_is_flagged",
            G + "test_a_submit_gated_only_by_confirm_is_flagged",
            G + "test_a_submit_gated_only_by_the_arm_toggle_is_flagged",
            G + "test_a_single_leg_sell_to_open_is_flagged",
            G + "test_a_sell_to_open_leg_with_no_long_leg_is_flagged",
            G + "test_the_equities_and_crypto_lanes_are_out_of_scope",
        ),
        evidence=option_order_safety_guard_evidence,
    ),
    Gate(
        key="options_lane_armed",
        name="Options lane armed",
        why=(
            "A real option order needs the shared ArmStore to report the OPTIONS lane armed, on top of the "
            "human gate. A disarmed -- or absent / unreadable -- store returns an unsubmitted preview and "
            "never reaches the connector; the arm gate is re-read at the irreversible moment, and again in "
            "the client (defense in depth)."
        ),
        proving_tests=(
            Bo + "test_fully_armed_and_confirmed_submits",
            Bo + "test_disarmed_lane_blocks_even_with_confirm",
            Bo + "test_gate_reason_names_the_disarmed_lane",
            Co + "test_place_submits_only_when_dry_run_false_and_confirm_true_and_armed",
            Co + "test_place_refuses_to_submit_when_the_options_lane_is_disarmed",
            Co + "test_place_refuses_to_submit_when_no_arm_store_is_wired",
        ),
    ),
    Gate(
        key="human_gate",
        name="Double human gate on every real order",
        why=(
            "A real order needs both flags together -- dry_run=False and confirm_live_order=True, compared by "
            "identity not truthiness. Either flag alone, a truthy non-boolean, a non-live run mode, or neither "
            "returns an unsubmitted preview."
        ),
        proving_tests=(
            Co + "test_place_defaults_to_dry_run_and_returns_a_payload",
            Co + "test_every_combination_but_both_flags_stays_unsubmitted",
            Co + "test_truthy_nonboolean_flags_do_not_arm_the_lane",
            Co + "test_inverted_confirm_caller_still_cannot_submit",
            Co + "test_cancel_defaults_to_dry_run_and_does_not_submit",
            Bo + "test_dry_run_default_never_submits",
            Bo + "test_confirm_without_dry_run_off_does_not_submit",
            Bo + "test_truthy_nonboolean_confirm_does_not_submit",
            Bo + "test_non_live_mode_forces_preview_even_when_armed",
        ),
    ),
    Gate(
        key="kill_switch",
        name="Kill switch (own file)",
        why=(
            "STOP_TRADING_OPTIONS or TRADING_ENABLED=false halts the lane, the switch is re-read at the "
            "irreversible moment, and it is a DIFFERENT file from the crypto and equities stop files so "
            "disarming one lane never touches another. The crypto lane stays disarmed."
        ),
        proving_tests=(
            Bo + "test_stop_trading_options_file_blocks",
            Bo + "test_trading_enabled_false_blocks",
            Bo + "test_default_kill_switch_is_options_lane",
            Ro + "test_options_kill_switch_is_its_own_file_not_the_crypto_or_equities_stop",
            Ro + "test_options_stop_file_halts_the_loop_immediately",
            Ro + "test_trading_enabled_false_halts_the_loop",
        ),
        evidence=lane_isolation_evidence,
    ),
    Gate(
        key="defined_risk_only",
        name="Defined-risk only (no naked short)",
        why=(
            "Every order payload is validated at build time: a sell-to-open leg with no covering buy-to-open "
            "leg is refused, as is a credit opening order with no long leg. Single-leg long calls/puts and "
            "defined-risk vertical spreads are allowed; a naked / uncovered short is refused before any gate "
            "and can never reach the connector, the paper ledger, or the static guard as a literal."
        ),
        proving_tests=(
            Co + "test_build_single_leg_long_is_a_debit_buy_to_open",
            Co + "test_build_refuses_a_long_plus_short_opening_pair",
            Co + "test_build_refuses_a_ten_to_one_ratio",
            Co + "test_build_refuses_a_short_call_covered_by_a_long_put",
            Co + "test_build_refuses_a_single_leg_sell_to_open_naked_short",
            Co + "test_build_refuses_a_credit_opening_order_with_no_long_leg",
            Co + "test_build_refuses_an_empty_leg_set",
            Co + "test_place_still_refuses_a_naked_short_even_fully_gated",
            Bo + "test_naked_short_is_refused",
            Bo + "test_credit_opening_with_no_long_leg_is_refused",
            Bo + "test_naked_short_refused_even_on_disarmed_preview_broker",
            Bo + "test_long_plus_short_opening_pair_is_refused",
            Bo + "test_a_ten_to_one_ratio_is_refused",
            Bo + "test_a_short_call_covered_by_a_long_put_is_refused",
            Bo + "test_empty_legs_refused",
            Ro + "test_an_undefined_risk_leg_never_books_a_paper_fill",
        ),
    ),
    Gate(
        key="account_confinement",
        name="Agentic-account-only (shared anchor)",
        why=(
            'Every order targets the single Robinhood-designated agent-tradable account (nickname "Agentic", '
            "..2092) -- the SAME anchor the equities lane pins, resolved by the agentic_allowed flag AND a "
            "nickname/suffix cross-check. The default account is a hard failure, refused ahead of the risk "
            "layer, and no source routes to an account by literal."
        ),
        proving_tests=(
            Co + "test_resolves_and_pins_the_agent_tradable_account",
            Co + "test_raises_when_no_agent_tradable_account_exists",
            Co + "test_raises_when_more_than_one_agent_tradable_account_exists",
            Co + "test_agentic_flag_on_wrong_number_is_rejected_not_pinned",
            Co + "test_agentic_flag_on_wrong_nickname_is_rejected_not_pinned",
            Co + "test_identity_anchor_is_loaded_from_equities_config_when_not_injected",
            Co + "test_place_refuses_the_default_account",
            Co + "test_place_targets_the_pinned_account_by_default",
            Co + "test_cancel_refuses_the_default_account",
            Bo + "test_wrong_account_is_refused",
        ),
        evidence=option_account_confinement_evidence,
    ),
    Gate(
        key="options_risk_caps",
        name="Options risk caps (premium / DTE / contracts / level)",
        why=(
            "The options RiskManager enforces the per-trade debit cap, the total-premium-at-risk cap, the "
            "min-DTE floor (0DTE blocked), the max-contracts-per-order cap and the granted option-approval "
            "level (fail closed on an unreadable level), and the configured caps are within bounds."
        ),
        proving_tests=(
            Rg + "test_debit_premium_over_cap_is_blocked_by_name",
            Rg + "test_total_at_risk_over_cap_counting_open_positions_is_blocked",
            Rg + "test_zero_dte_is_blocked_by_default",
            Rg + "test_under_the_dte_floor_is_blocked_by_name",
            Rg + "test_an_expired_contract_is_blocked",
            Rg + "test_over_the_contract_cap_is_blocked_by_name",
            Rg + "test_single_long_needs_level_2_and_is_blocked_at_level_1",
            Rg + "test_a_long_plus_short_is_unsupported_and_blocked_by_the_level_gate",
            Rg + "test_an_unknown_granted_level_fails_closed",
            Rg + "test_an_unsupported_strategy_demands_a_level_the_lane_never_grants",
            Rg + "test_from_rules_defaults_when_section_missing",
        ),
        evidence=option_risk_caps_evidence,
    ),
    Gate(
        key="paper_proven_twice",
        name="Paper ran unattended twice, reconciled clean, on a real basis",
        why=(
            f"The audit log records at least {REQUIRED_CLEAN_PAPER_RUNS} bounded options paper loops that "
            f"finished on their own bound without being halted, each filling at least one DEFINED-RISK order "
            f"and followed by a reconcile reporting no errors -- and each priced on a REAL basis "
            f"(`{BASIS_EXPECTED_MOVE}` real Massive underlying closes, or `{BASIS_CONNECTOR_QUOTE}` the live "
            f"option quote). A made-up or unrecorded basis, or a zero-fill run, does not count."
        ),
        proving_tests=(
            Ro + "test_bounded_paper_loop_fills_defined_risk_and_reconciles_clean",
            Ro + "test_loop_requires_an_explicit_bound",
            Ro + "test_a_proving_run_prices_on_real_underlying_bars_and_records_the_basis",
            Ro + "test_a_proving_run_refuses_to_fall_back_when_no_real_bars_exist",
            Ro + "test_a_fresh_ledger_each_run_makes_runs_independent",
            Ro + "test_a_run_priced_on_a_made_up_basis_is_not_counted",
            Ro + "test_a_zero_fill_run_is_not_counted",
            O + "test_two_clean_option_runs_satisfy_the_paper_gate",
            O + "test_a_run_priced_on_a_made_up_basis_is_not_clean_at_the_gate",
        ),
        evidence=option_paper_runs_evidence,
    ),
    Gate(
        key="no_secret_in_code",
        name="No secret in code",
        why=(
            "Options auth is the OAuth connector, which leaves no key to hold; no credential-shaped literal "
            "may appear in src/, tests/, scripts/, config/ or deploy/."
        ),
        evidence=no_secret_in_code_evidence,
    ),
    Gate(
        key="git_hygiene",
        name="Nothing leaks on push",
        why=(
            "No .env, database, SQLite sidecar or key file is tracked, and every runtime artifact the lane "
            "writes is git-ignored -- the secret scan reads the source, this reads what a push would publish."
        ),
        proving_tests=(
            O + "test_option_git_hygiene_passes_on_this_repo",
            O + "test_option_git_hygiene_fails_when_a_database_is_tracked",
        ),
        evidence=git_hygiene_evidence,
    ),
)


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------


def readiness_verdict(suite: SuiteResult, gates: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """The overall verdict -- a pure function of the suite result and the gate
    rows, so it can be tested without running anything.

    ready:true requires a green non-vacuous suite AND every gate passing. The two
    non-negotiable conditions are re-asserted by name after the generic loop: a
    red suite and an unproven options order-safety guard each force ready:false on
    their own, whatever the gate table says or how it is later edited.
    """
    blocking: list[str] = []

    if suite.tests_run == 0:
        blocking.append("the test suite collected no tests; there is nothing to prove readiness with")
    if suite.failed:
        listed = ", ".join(suite.failed[:5])
        more = f" (+{len(suite.failed) - 5} more)" if len(suite.failed) > 5 else ""
        blocking.append(f"{len(suite.failed)} test(s) failed: {listed}{more}")
    elif suite.returncode != 0:
        blocking.append(f"pytest exited {suite.returncode}")

    for gate in gates:
        if not gate["passed"]:
            blocking.append(f"gate not proven -- {gate['name']}: {'; '.join(gate['reasons'])}")

    # Non-negotiable, re-asserted independently of the loop above.
    if not suite.passed and not any("test" in reason for reason in blocking):
        blocking.append("the full test suite is not green")
    guard = next((gate for gate in gates if gate["key"] == OPTION_ORDER_SAFETY_GUARD), None)
    if guard is None:
        blocking.append("the options order-safety guard gate is absent from the readiness report")
    elif not guard["passed"] and not any(
        OPTION_ORDER_SAFETY_GUARD in reason or guard["name"] in reason for reason in blocking
    ):
        blocking.append("the options order-safety guard is not proven")

    return (not blocking), blocking


def option_live_readiness(
    root: Path,
    suite_runner: Callable[[Path], SuiteResult] = run_project_test_suite,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Produce the options lane's live-readiness report.

    Read-only: runs the test suite, reads config, reads the audit log, scans the
    source. Never touches the connector and never changes a posture.
    """
    rules = load_rules(root)
    suite = suite_runner(root)
    gates = [evaluate_gate(gate, suite, root, rules) for gate in GATES]
    ready, blocking = readiness_verdict(suite, gates)

    report: dict[str, Any] = {
        "lane": VENUE,
        "generated_at": datetime.now(UTC).isoformat(),
        "ready": ready,
        "blocking_reasons": blocking,
        "posture": {
            "execution_surface": "authorized Robinhood OPTIONS OAuth connector toolset (agent-hosted; session-bound, no key to mint)",
            "auth": "the OAuth connector is itself the credential -- no vault entry, no .env key",
            "defined_risk": "DEFINED RISK ONLY -- single-leg long calls/puts and defined-risk spreads; a naked / uncovered short is refused at build time",
            "paper_broker": (
                "local paper ledger (data/option_paper_trades.db) simulating defined-risk fills against a REAL "
                "basis (real Massive underlying closes x an expected-move fraction, or the live option quote); "
                "no order is placed and no premium here is an execution price"
            ),
            "quote_basis": option_basis_summary(root),
            "quote_basis_recorded": recorded_option_bases(root),
            "real_bases": list(REAL_OPTION_BASES),
            "options_stop_file": _options_stop_file(rules),
            "options_universe": list((rules.get("options") or {}).get("universe", []) or []),
            "note": "arming the options lane is the operator's explicit call and is never a side effect of this report",
        },
        "test_suite": {
            "passed": suite.passed,
            "returncode": suite.returncode,
            "tests_run": suite.tests_run,
            "failed": suite.failed,
            "output_tail": suite.output_tail,
        },
        "gates": gates,
        "gates_passed": sum(1 for gate in gates if gate["passed"]),
        "gates_total": len(gates),
    }

    if logger is not None:
        summary = f"options live-readiness: ready={ready}, {report['gates_passed']}/{report['gates_total']} gates proven"
        logger.log_decision(
            None,
            "option_live_readiness",
            summary if ready else f"{summary}; blocked by: {'; '.join(blocking[:3])}",
            {
                "venue": VENUE,
                "ready": ready,
                "blocking_reasons": blocking,
                "gates": {gate["key"]: gate["passed"] for gate in gates},
                "test_suite": {"passed": suite.passed, "tests_run": suite.tests_run, "failed": suite.failed},
            },
        )
    return report


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def readiness_markdown(report: dict[str, Any]) -> str:
    """The same report as a human-readable page."""
    verdict = "READY: true" if report["ready"] else "READY: false"
    suite = report["test_suite"]
    lines = [
        "# Options lane -- live-readiness report",
        "",
        f"**{verdict}**  -- {report['gates_passed']}/{report['gates_total']} gates proven, "
        f"{suite['tests_run']} distinct tests recorded (parametrized cases rolled up), "
        f"{len(suite['failed'])} failed.",
        "",
        f"Generated {report['generated_at']} for lane `{report['lane']}`.",
        "",
        "This report is read-only. Arming the options lane is the operator's explicit call",
        "and is never a side effect of generating it.",
        "",
        "## Verdict",
        "",
    ]
    if report["ready"]:
        lines += [
            "Every gate below is proven by tests that exist and pass, the full suite is green,",
            "and every non-test evidence check holds.",
            "",
        ]
    else:
        lines.append("**Not ready.** Blocking:")
        lines.append("")
        lines += [f"- {reason}" for reason in report["blocking_reasons"]]
        lines.append("")

    lines += [
        "## Gates",
        "",
        "| Gate | Result | Proving tests | Evidence |",
        "| --- | --- | --- | --- |",
    ]
    for gate in report["gates"]:
        result = "PASS" if gate["passed"] else "FAIL"
        if gate["proving_tests_total"]:
            tests = f"{gate['proving_tests_passed']}/{gate['proving_tests_total']} passing"
        else:
            tests = "evidence only"
        evidence = gate["evidence"]["detail"] if gate["evidence"] else "-"
        lines.append(f"| {gate['name']} | **{result}** | {tests} | {evidence} |")

    lines += ["", "## What each gate means, and what proves it", ""]
    for gate in report["gates"]:
        result = "PASS" if gate["passed"] else "FAIL"
        lines += [f"### {gate['name']} -- {result}", "", gate["why"], ""]
        for row in gate["proving_tests"]:
            mark = "ok" if row["passed"] else row["outcome"]
            lines.append(f"- `{row['nodeid']}` -- {mark}")
        if gate["evidence"]:
            state = "ok" if gate["evidence"]["passed"] else "FAILED"
            lines.append(f"- evidence ({state}): {gate['evidence']['detail']}")
        if gate["reasons"]:
            lines.append("")
            lines += [f"- **blocking:** {reason}" for reason in gate["reasons"]]
        lines.append("")

    posture = report["posture"]
    lines += [
        "## Posture",
        "",
        f"- Execution surface: {posture['execution_surface']}",
        f"- Auth: {posture['auth']}",
        f"- Defined risk: {posture['defined_risk']}",
        f"- Paper: {posture['paper_broker']}",
        f"- Proving basis: {posture['quote_basis']}",
        f"- Options kill switch: {posture['options_stop_file']}",
        f"- Universe: {', '.join(posture['options_universe']) or '(falls back to the equities universe)'}",
        "",
    ]
    return "\n".join(lines)
