"""Tests for the equities lane's live-readiness gate (src/equity_readiness.py).

The point of the readiness report is that it cannot lie in the operator's
favour, so most of what is tested here is the NEGATIVE direction: a failing
test, a deleted test, a loosened cap, a missing paper run and a leaked
credential must each drive ready to false on their own.

No test in this file runs the real pytest suite -- `equity_live_readiness`
takes its suite runner as an argument precisely so a test can hand it a
synthetic result. `parse_pytest_output` is tested directly against real
verbose-output shapes instead.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
import yaml

from src.equity_readiness import (
    ALLOWED_LITERALS,
    GATES,
    ORDER_SYMBOL_GUARD,
    REQUIRED_CLEAN_PAPER_RUNS,
    REQUIRED_QUOTE_SOURCE,
    UNRECORDED_QUOTE_SOURCE,
    Evidence,
    SuiteResult,
    agentic_account_evidence,
    crypto_lane_untouched_evidence,
    equity_live_readiness,
    evaluate_gate,
    git_hygiene_evidence,
    hardcoded_account_literals,
    no_secret_in_code_evidence,
    paper_proving_runs,
    paper_runs_evidence,
    parse_pytest_output,
    quote_source_summary,
    recorded_quote_sources,
    readiness_markdown,
    readiness_verdict,
    risk_caps_evidence,
    scan_for_secrets,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every node id any gate names. A synthetic all-green suite is built from this
# so the fixtures never drift from the gate table.
ALL_PROVING_TESTS = tuple(sorted({nodeid for gate in GATES for nodeid in gate.proving_tests}))


def green_suite(**overrides: str) -> SuiteResult:
    """A synthetic suite where every proving test passed, plus whatever
    outcome overrides a test wants to inject."""
    outcomes = dict.fromkeys(ALL_PROVING_TESTS, "PASSED")
    outcomes.update(overrides)
    return SuiteResult(returncode=0, outcomes=outcomes)


# --- a tree the report can be pointed at ------------------------------------

RULES = {
    "trading": {"enabled": True, "mode": "live"},
    "risk": {
        "max_trade_amount_usd": 100.0,
        "max_daily_loss_usd": 100.0,
        "max_trades_per_day": 5,
        "max_open_positions": 10,
        "max_symbol_allocation_percent": 25.0,
        "min_order_cooldown_seconds": 300,
        "allow_margin": False,
        "allow_shorts": False,
        "allow_shorting": False,
    },
    "kill_switch": {"stop_file": "STOP_TRADING", "env_var": "TRADING_ENABLED"},
    "equities": {
        "allow_extended_hours": False,
        "kill_switch": {"stop_file": "STOP_TRADING_EQUITIES", "env_var": "TRADING_ENABLED"},
        "universe": ["ZZTOP"],
    },
}

BINDING_DOC = """# binding
The designated account is nickname "Agentic", ending 2092.
"""


def log_decision(db: Path, action: str, details: dict, symbol: str | None = None, reason: str = "fixture") -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL, symbol TEXT, action TEXT NOT NULL,
                reason TEXT NOT NULL, details TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO decisions(timestamp, symbol, action, reason, details) VALUES(?, ?, ?, ?, ?)",
            ("2026-08-29T00:00:00+00:00", symbol, action, reason, json.dumps(details)),
        )


# The rationale a real fill carries -- the strategy conditions that fired, the
# same shape run_equity_cycle logs. A counted run must show a real one.
FILL_RATIONALE = "ema20_above_ema50+rsi_between_35_and_70+momentum_5_positive"


def paper_fill(db: Path, symbol: str = "AAPL", notional: float = 100.0) -> None:
    """A single simulated paper fill, with the non-empty rationale a genuine
    decision carries and a notional that moves the ledger."""
    log_decision(
        db,
        "paper_order_filled",
        {"symbol": symbol, "side": "buy", "quantity": notional / 100.0, "price": 100.0, "notional": notional},
        symbol=symbol,
        reason=FILL_RATIONALE,
    )


def clean_paper_run(
    db: Path,
    iterations: int = 12,
    symbol: str = "AAPL",
    fills: int = 3,
    quote_source: str | None = REQUIRED_QUOTE_SOURCE,
) -> None:
    """One GENUINE unattended bounded loop: real fills, priced on the REAL
    Massive historical feed, the loop that finished, then a clean reconcile.
    A run with no fills is not a clean run; nor is one priced on anything but
    the real feed, which is what `quote_source=None` models."""
    for _ in range(fills):
        paper_fill(db, symbol=symbol)
    details: dict = {"iterations_completed": iterations}
    if quote_source is not None:
        details["quote_source"] = quote_source
    log_decision(db, "equity_paper_loop_completed", details)
    log_decision(db, "equity_paper_reconcile", {"adjusted": [], "errors": []})


# The ignore rules the real repo carries. Kept here as the shape a clean tree
# must have, so a test can widen or narrow one and watch the gate react.
FIXTURE_GITIGNORE = """.env
.env.*
!.env.example
logs/
data/*.db
data/*.db-journal
data/*.db-wal
data/*.db-shm
data/*.sqlite
data/*.sqlite3
data/private/
data/proving_runs/
__pycache__/
*.pyc
.venv/
.pytest_cache/
"""


def git(root: Path, *args: str) -> None:
    """Run a git command in the fixture repo, failing loudly if it errors."""
    result = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"


def init_repo(root: Path) -> None:
    """A real git repo, because git hygiene cannot be faked with a fake one --
    the gate shells out to `git ls-files` and `git check-ignore`."""
    git(root, "init", "-q")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "fixture")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "fixture")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A minimal tree shaped like the real one: config, binding doc, guard
    test, stop files, an audit database with two clean paper runs, and a git
    repo whose ignore rules match the real ones."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "trading_rules.yaml").write_text(yaml.safe_dump(RULES), encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "rh-equities-binding.md").write_text(BINDING_DOC, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_order_symbol_guard.py").write_text("# guard\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lane.py").write_text("SYMBOLS = []\n", encoding="utf-8")
    (tmp_path / "STOP_TRADING").write_text("disarmed\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(FIXTURE_GITIGNORE, encoding="utf-8")
    (tmp_path / ".env.example").write_text("ROBINHOOD_API_KEY=\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    database = tmp_path / "data" / "trading_agent.db"
    for _ in range(REQUIRED_CLEAN_PAPER_RUNS):
        clean_paper_run(database)
    init_repo(tmp_path)
    return tmp_path


def report_for(tree: Path, suite: SuiteResult | None = None) -> dict:
    return equity_live_readiness(tree, suite_runner=lambda _root: suite or green_suite())


# --- parsing a real pytest run ----------------------------------------------


def test_parses_a_verbose_result_line() -> None:
    outcomes = parse_pytest_output("tests/test_a.py::test_one PASSED                        [ 50%]")

    assert outcomes == {"tests/test_a.py::test_one": "PASSED"}


def test_a_parametrized_id_containing_spaces_still_parses() -> None:
    """Real ids in this repo look like `[True-False-dry_run off, no confirm]`,
    so the parser cannot split on the first space."""
    line = "tests/test_b.py::test_flags[True-False-dry_run off, confirmation absent] PASSED [ 32%]"

    assert parse_pytest_output(line) == {"tests/test_b.py::test_flags": "PASSED"}


def test_one_failing_parameter_case_fails_the_whole_proving_test() -> None:
    output = "\n".join(
        [
            "tests/test_b.py::test_flags[a] PASSED  [ 10%]",
            "tests/test_b.py::test_flags[b] FAILED  [ 20%]",
            "tests/test_b.py::test_flags[c] PASSED  [ 30%]",
        ]
    )

    assert parse_pytest_output(output) == {"tests/test_b.py::test_flags": "FAILED"}


def test_a_skip_is_not_a_pass() -> None:
    outcomes = parse_pytest_output("tests/test_c.py::test_skipped SKIPPED [ 10%]")
    suite = SuiteResult(returncode=0, outcomes=outcomes)
    gate = evaluate_gate(
        type(GATES[0])(key="k", name="n", why="w", proving_tests=("tests/test_c.py::test_skipped",)),
        suite,
        REPO_ROOT,
        {},
    )

    assert gate["passed"] is False
    assert "SKIPPED" in gate["reasons"][0]


def test_summary_and_traceback_lines_are_not_mistaken_for_results() -> None:
    output = "\n".join(
        [
            "FAILED tests/test_a.py::test_one - AssertionError: nope",
            "tests/test_a.py:42: AssertionError",
            "=========================== short test summary info ===========================",
        ]
    )

    assert parse_pytest_output(output) == {}


def test_a_suite_that_collected_nothing_is_not_green() -> None:
    assert SuiteResult(returncode=0, outcomes={}).passed is False


# --- the verdict ------------------------------------------------------------


def test_ready_is_true_when_the_suite_is_green_and_every_gate_passes(tree: Path) -> None:
    report = report_for(tree)

    assert report["ready"] is True, report["blocking_reasons"]
    assert report["blocking_reasons"] == []
    assert report["gates_passed"] == report["gates_total"]


def test_every_gate_is_enumerated_with_a_pass_or_fail(tree: Path) -> None:
    report = report_for(tree)
    keys = {gate["key"] for gate in report["gates"]}

    assert keys >= {
        ORDER_SYMBOL_GUARD,
        "kill_switch",
        "regular_trading_hours",
        "pattern_day_trader",
        "long_only_no_shorts",
        "no_margin",
        "risk_caps",
        "agentic_account_only",
        "no_secret_in_code",
        "paper_proven_twice",
    }
    for gate in report["gates"]:
        assert isinstance(gate["passed"], bool)
        assert gate["name"] and gate["why"]


def test_a_single_failing_test_makes_ready_false(tree: Path) -> None:
    """Acceptance criterion: ready:true is impossible if any test fails."""
    failing = ALL_PROVING_TESTS[0]
    report = report_for(tree, green_suite(**{failing: "FAILED"}))

    assert report["ready"] is False
    assert any(failing in reason for reason in report["blocking_reasons"])


def test_a_failing_test_outside_every_gate_still_makes_ready_false(tree: Path) -> None:
    """A red test nothing happens to name as a proving test is still a red
    suite, and a red suite is never ready."""
    suite = green_suite()
    suite = SuiteResult(returncode=1, outcomes={**suite.outcomes, "tests/test_unrelated.py::test_x": "FAILED"})
    report = report_for(tree, suite)

    assert report["ready"] is False
    assert any("failed" in reason for reason in report["blocking_reasons"])


def test_a_nonzero_exit_with_no_named_failure_still_makes_ready_false(tree: Path) -> None:
    """A collection error can take pytest red without any test line going red."""
    suite = green_suite()
    report = report_for(tree, SuiteResult(returncode=2, outcomes=suite.outcomes))

    assert report["ready"] is False
    assert any("exited 2" in reason for reason in report["blocking_reasons"])


def test_an_empty_suite_makes_ready_false(tree: Path) -> None:
    report = report_for(tree, SuiteResult(returncode=0, outcomes={}))

    assert report["ready"] is False
    assert any("collected no tests" in reason for reason in report["blocking_reasons"])


def test_an_absent_order_symbol_guard_test_makes_ready_false(tree: Path) -> None:
    """Acceptance criterion: ready:true is impossible if the order-symbol
    guard is absent. Deleting the proving test reads exactly like failing it."""
    guard = next(gate for gate in GATES if gate.key == ORDER_SYMBOL_GUARD)
    outcomes = {node: "PASSED" for node in ALL_PROVING_TESTS if node not in guard.proving_tests}
    report = report_for(tree, SuiteResult(returncode=0, outcomes=outcomes))

    assert report["ready"] is False
    guard_row = next(row for row in report["gates"] if row["key"] == ORDER_SYMBOL_GUARD)
    assert guard_row["passed"] is False
    assert all("ABSENT" in reason for reason in guard_row["reasons"])


def test_deleting_the_guard_file_makes_ready_false_even_with_a_green_suite(tree: Path) -> None:
    (tree / "tests" / "test_order_symbol_guard.py").unlink()
    report = report_for(tree)

    assert report["ready"] is False
    guard_row = next(row for row in report["gates"] if row["key"] == ORDER_SYMBOL_GUARD)
    assert guard_row["evidence"]["passed"] is False


def test_the_verdict_refuses_a_report_with_no_order_symbol_guard_gate_at_all() -> None:
    """The guard is re-asserted by name, so removing its row from the gate
    table cannot produce a ready:true report."""
    ready, blocking = readiness_verdict(green_suite(), [{"key": "kill_switch", "name": "Kill switch", "passed": True, "reasons": []}])

    assert ready is False
    assert any("order-symbol guard gate is absent" in reason for reason in blocking)


def test_the_verdict_is_a_pure_function_of_the_suite_and_the_gates() -> None:
    gates = [{"key": ORDER_SYMBOL_GUARD, "name": "Order-symbol guard", "passed": True, "reasons": []}]

    assert readiness_verdict(green_suite(), gates) == (True, [])
    assert readiness_verdict(SuiteResult(returncode=1, outcomes={"a.py::b": "FAILED"}), gates)[0] is False


# --- risk caps --------------------------------------------------------------


def test_caps_within_bounds_pass(tree: Path) -> None:
    assert risk_caps_evidence(tree, RULES).passed is True


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_trade_amount_usd", 250.0),
        ("max_daily_loss_usd", 500.0),
        ("max_trades_per_day", 40),
        ("max_open_positions", 99),
        ("max_symbol_allocation_percent", 80.0),
        ("min_order_cooldown_seconds", 5),
    ],
)
def test_a_loosened_cap_fails_the_risk_gate(tree: Path, key: str, value: float) -> None:
    rules = {**RULES, "risk": {**RULES["risk"], key: value}}

    evidence = risk_caps_evidence(tree, rules)

    assert evidence.passed is False
    assert key in evidence.detail


def test_a_missing_cap_is_not_a_permissive_default(tree: Path) -> None:
    risk = {key: value for key, value in RULES["risk"].items() if key != "max_trade_amount_usd"}

    evidence = risk_caps_evidence(tree, {**RULES, "risk": risk})

    assert evidence.passed is False
    assert "not configured" in evidence.detail


@pytest.mark.parametrize("flag", ["allow_margin", "allow_shorts", "allow_shorting"])
def test_turning_on_margin_or_shorts_fails_the_risk_gate(tree: Path, flag: str) -> None:
    rules = {**RULES, "risk": {**RULES["risk"], flag: True}}

    evidence = risk_caps_evidence(tree, rules)

    assert evidence.passed is False
    assert flag in evidence.detail


def test_an_empty_equities_universe_fails_the_risk_gate(tree: Path) -> None:
    rules = {**RULES, "equities": {**RULES["equities"], "universe": []}}

    assert risk_caps_evidence(tree, rules).passed is False


def test_the_repos_own_caps_are_within_bounds() -> None:
    """Pinned against the real config, not a fixture."""
    rules = yaml.safe_load((REPO_ROOT / "config" / "trading_rules.yaml").read_text(encoding="utf-8"))

    evidence = risk_caps_evidence(REPO_ROOT, rules)

    assert evidence.passed is True, evidence.detail


# --- the paper proving runs -------------------------------------------------


def test_two_clean_runs_satisfy_the_paper_gate(tree: Path) -> None:
    evidence = paper_runs_evidence(tree, RULES)

    assert evidence.passed is True
    assert evidence.data["clean_run_count"] == REQUIRED_CLEAN_PAPER_RUNS


def test_one_clean_run_is_not_enough(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    clean_paper_run(tmp_path / "data" / "trading_agent.db")

    evidence = paper_runs_evidence(tmp_path, RULES)

    assert evidence.passed is False
    assert "1 clean unattended paper run" in evidence.detail


def test_a_run_whose_reconcile_reported_errors_is_not_clean(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    database = tmp_path / "data" / "trading_agent.db"
    clean_paper_run(database)
    # A second run that DID trade, so the only thing keeping it from clean is
    # that its reconcile reported errors -- isolates the errors path.
    paper_fill(database, symbol="MSFT")
    log_decision(database, "equity_paper_loop_completed", {"iterations_completed": 12, "quote_source": REQUIRED_QUOTE_SOURCE})
    log_decision(database, "equity_paper_reconcile", {"adjusted": ["AAA"], "errors": ["negative position"]})

    evidence = paper_runs_evidence(tmp_path, RULES)

    assert evidence.passed is False
    assert [run["clean"] for run in paper_proving_runs(tmp_path)] == [True, False]


def test_a_completed_run_with_no_reconcile_after_it_is_not_clean(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    database = tmp_path / "data" / "trading_agent.db"
    clean_paper_run(database)
    # A second run that traded but has no reconcile -- isolates the missing
    # reconcile as the sole reason it is not clean.
    paper_fill(database, symbol="MSFT")
    log_decision(database, "equity_paper_loop_completed", {"iterations_completed": 12, "quote_source": REQUIRED_QUOTE_SOURCE})

    runs = paper_proving_runs(tmp_path)

    assert runs[-1]["reconciled"] is False
    assert runs[-1]["clean"] is False


def test_a_zero_trade_run_is_not_clean(tmp_path: Path) -> None:
    """A loop that filled nothing and reconciled clean must NOT count -- it
    proves the loop can idle, not that the lane can trade and reconcile. This
    is the tautology the old evidence let through."""
    (tmp_path / "data").mkdir()
    database = tmp_path / "data" / "trading_agent.db"
    log_decision(database, "equity_paper_loop_completed", {"iterations_completed": 80, "quote_source": REQUIRED_QUOTE_SOURCE})
    log_decision(database, "equity_paper_reconcile", {"adjusted": [], "errors": []})

    runs = paper_proving_runs(tmp_path)

    assert runs[-1]["reconciled"] is True
    assert runs[-1]["reconcile_errors"] == []
    assert runs[-1]["fills"] == 0
    assert runs[-1]["clean"] is False
    assert paper_runs_evidence(tmp_path, RULES).passed is False


def test_a_run_preceded_by_a_manual_counter_reset_is_not_independent(tmp_path: Path) -> None:
    """A manual state mutation inside a run's window -- the daily-counter reset
    this repo's own run 3 needed to be able to trade again -- means the run was
    not left unattended, so it cannot count however clean the reconcile looks."""
    (tmp_path / "data").mkdir()
    database = tmp_path / "data" / "trading_agent.db"
    clean_paper_run(database)  # a genuine first run
    # Second run: someone resets the daily counter, THEN it trades and reconciles.
    log_decision(database, "equity_paper_run2_daily_counter_reset", {"before": {"trade_count": 5}, "after": {"trade_count": 0}})
    paper_fill(database, symbol="MSFT")
    log_decision(database, "equity_paper_loop_completed", {"iterations_completed": 80, "quote_source": REQUIRED_QUOTE_SOURCE})
    log_decision(database, "equity_paper_reconcile", {"adjusted": [], "errors": []})

    runs = paper_proving_runs(tmp_path)

    assert runs[1]["fills"] >= 1
    assert runs[1]["reconcile_errors"] == []
    assert runs[1]["manual_mutation_in_window"] is True
    assert runs[1]["clean"] is False
    # Only the first, genuinely unattended run stands.
    assert [run["clean"] for run in runs] == [True, False]


def test_a_single_reconcile_cannot_vouch_for_two_runs(tmp_path: Path) -> None:
    """The reconcile is matched to the run in whose window it falls, and is
    consumed at most once. A first run that traded and reconciled is clean; a
    second run with a real fill but NO reconcile of its own is not."""
    (tmp_path / "data").mkdir()
    database = tmp_path / "data" / "trading_agent.db"
    # Run 1: fill, loop, reconcile.
    paper_fill(database, symbol="AAPL")
    log_decision(database, "equity_paper_loop_completed", {"iterations_completed": 5, "quote_source": REQUIRED_QUOTE_SOURCE})
    log_decision(database, "equity_paper_reconcile", {"adjusted": [], "errors": []})
    # Run 2: fill, loop -- but no reconcile follows it.
    paper_fill(database, symbol="MSFT")
    log_decision(database, "equity_paper_loop_completed", {"iterations_completed": 5, "quote_source": REQUIRED_QUOTE_SOURCE})

    runs = paper_proving_runs(tmp_path)

    assert [run["clean"] for run in runs] == [True, False]
    assert runs[0]["reconciled"] is True
    assert runs[1]["reconciled"] is False
    # The one reconcile belongs to run 1 only -- run 2 never borrows it.
    assert runs[0]["reconcile_at"] is not None
    assert runs[1]["reconcile_at"] is None


# --- the price source a run was proven on -----------------------------------


def test_a_run_priced_on_a_synthetic_source_is_not_clean(tmp_path: Path) -> None:
    """The audit finding, closed at the gate. A run that is perfect in every
    other respect -- real fills, its own reconcile, no errors, no manual
    mutation -- still does not count if it was priced on a generated series.
    Only the real Massive feed counts."""
    (tmp_path / "data").mkdir()
    database = tmp_path / "data" / "trading_agent.db"
    clean_paper_run(database, quote_source="synthetic")

    runs = paper_proving_runs(tmp_path)

    assert runs[0]["quote_source"] == "synthetic"
    assert runs[0]["quote_source_is_real"] is False
    # Everything else about it is clean; the source alone disqualifies it.
    assert runs[0]["fills"] >= 1
    assert runs[0]["reconcile_errors"] == []
    assert runs[0]["manual_mutation_in_window"] is False
    assert runs[0]["clean"] is False


def test_a_run_that_recorded_no_quote_source_is_not_clean(tmp_path: Path) -> None:
    """A run from before the source was recorded cannot vouch for itself. An
    absent source reads exactly like a wrong one -- silence is not the real
    feed."""
    (tmp_path / "data").mkdir()
    database = tmp_path / "data" / "trading_agent.db"
    clean_paper_run(database, quote_source=None)

    runs = paper_proving_runs(tmp_path)

    assert runs[0]["quote_source"] == UNRECORDED_QUOTE_SOURCE
    assert runs[0]["clean"] is False


def test_the_paper_gate_requires_the_real_quote_source(tmp_path: Path) -> None:
    """Two otherwise-clean runs on a synthetic feed do not satisfy the gate;
    the same two on the real feed do. The source is the only thing that differs."""
    (tmp_path / "data").mkdir()
    synthetic_db = tmp_path / "data" / "trading_agent.db"
    for _ in range(REQUIRED_CLEAN_PAPER_RUNS):
        clean_paper_run(synthetic_db, quote_source="synthetic")

    synthetic = paper_runs_evidence(tmp_path, RULES)

    assert synthetic.passed is False
    assert synthetic.data["clean_run_count"] == 0
    assert synthetic.data["required_quote_source"] == REQUIRED_QUOTE_SOURCE == "massive"
    assert "not priced on the real feed" in synthetic.detail

    real_root = tmp_path / "real"
    (real_root / "data").mkdir(parents=True)
    for _ in range(REQUIRED_CLEAN_PAPER_RUNS):
        clean_paper_run(real_root / "data" / "trading_agent.db")

    real = paper_runs_evidence(real_root, RULES)

    assert real.passed is True, real.detail
    assert REQUIRED_QUOTE_SOURCE in real.detail
    assert real.data["recorded_quote_sources"] == {REQUIRED_QUOTE_SOURCE: REQUIRED_CLEAN_PAPER_RUNS}


def test_the_recorded_sources_are_reported_by_name(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    database = tmp_path / "data" / "trading_agent.db"
    clean_paper_run(database, quote_source="synthetic")
    clean_paper_run(database)

    assert recorded_quote_sources(tmp_path) == {REQUIRED_QUOTE_SOURCE: 1, "synthetic": 1}
    summary = quote_source_summary(tmp_path)
    assert "mixed" in summary and "synthetic x1" in summary and "NOT counted" in summary


def test_the_summary_says_so_when_nothing_ran_on_the_real_feed(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    clean_paper_run(tmp_path / "data" / "trading_agent.db", quote_source="synthetic")

    assert "no run priced on massive" in quote_source_summary(tmp_path)


def test_no_audit_database_means_no_proven_runs(tmp_path: Path) -> None:
    assert paper_proving_runs(tmp_path) == []
    assert paper_runs_evidence(tmp_path, RULES).passed is False


@pytest.mark.skipif(
    not (REPO_ROOT / "data" / "trading_agent.db").exists(),
    reason="the machine-local runtime audit db is absent (a fresh checkout); nothing to pin against",
)
def test_the_repos_own_audit_log_is_counted_honestly() -> None:
    """Pinned against the real audit log, counted the GENUINE way.

    Two rounds of making this evidence real have each cost the lane a run it
    thought it had. First: the tautological count hid that run 2 filled nothing
    and run 3 traded only after a manual daily-counter reset, leaving exactly
    one genuine run. Now: requiring a REAL price source costs it that one too.
    Every recorded run here predates the Massive history feed -- they were
    priced on a locally generated series and recorded no source at all -- so
    ZERO runs count, and the gate is honestly not met.

    This asserts the honest count rather than a number that was never true.
    Two genuine unattended runs priced on real Massive bars
    (src.equity_runtime.run_equity_proving_run) are owed before this gate can
    pass. The audit trail of the old runs is untouched; only what it is allowed
    to prove has changed.
    """
    runs = paper_proving_runs(REPO_ROOT)
    clean = [run for run in runs if run["clean"]]
    evidence = paper_runs_evidence(REPO_ROOT, {})

    assert evidence.passed is False, evidence.detail
    assert evidence.data["clean_run_count"] == 0
    assert clean == []

    # Not counted, and for the stated reason: none of them named a real feed.
    assert runs, "the recorded runs are still in the audit log; they simply no longer count"
    assert all(not run["quote_source_is_real"] for run in runs)
    assert REQUIRED_QUOTE_SOURCE not in recorded_quote_sources(REPO_ROOT)
    assert "not priced on the real feed" in evidence.detail

    # The earlier, independent reasons still stand on the runs they applied to,
    # so relaxing the source rule alone could never turn this gate green.
    assert any(run["fills"] == 0 for run in runs), "the zero-trade run must still be visible as such"
    assert any(run["manual_mutation_in_window"] for run in runs), "the reset-tainted run must still be visible as such"

    # The fills that did happen carried a real, non-empty rationale -- the audit
    # trail is genuine, it is the price series behind it that was not.
    with sqlite3.connect(REPO_ROOT / "data" / "trading_agent.db") as conn:
        reasons = [
            row[0]
            for row in conn.execute("SELECT reason FROM decisions WHERE action = 'paper_order_filled'").fetchall()
        ]
    assert reasons and all(reason and reason.strip() for reason in reasons)


# --- the agentic-account and crypto-lane checks -----------------------------


def test_the_account_gate_needs_the_binding_doc(tree: Path) -> None:
    (tree / "docs" / "rh-equities-binding.md").unlink()

    evidence = agentic_account_evidence(tree, RULES)

    assert evidence.passed is False
    assert "binding" in evidence.detail


def test_a_binding_doc_that_stopped_naming_the_account_fails(tree: Path) -> None:
    (tree / "docs" / "rh-equities-binding.md").write_text("# binding\nnothing pinned here\n", encoding="utf-8")

    assert agentic_account_evidence(tree, RULES).passed is False


def test_an_account_number_routed_to_by_literal_fails(tree: Path) -> None:
    (tree / "src" / "router.py").write_text(
        'def send(client):\n    return client.place(account_number="99887766")\n', encoding="utf-8"
    )

    evidence = agentic_account_evidence(tree, RULES)

    assert evidence.passed is False
    assert "router.py" in evidence.detail


def test_a_prose_label_mentioning_the_account_is_not_a_routing_literal(tree: Path) -> None:
    """The dashboard displays a masked account number as a human label; that
    is a caption, not a route, and must not fail the gate."""
    (tree / "src" / "panel.py").write_text(
        'ROWS = {"Designated account number": "..2092"}\n', encoding="utf-8"
    )

    assert agentic_account_evidence(tree, RULES).passed is True


def test_the_repo_routes_no_order_to_an_account_by_literal() -> None:
    assert hardcoded_account_literals(REPO_ROOT) == []


def test_deleting_the_crypto_stop_file_fails_the_crypto_lane_check(tree: Path) -> None:
    (tree / "STOP_TRADING").unlink()

    evidence = crypto_lane_untouched_evidence(tree, RULES)

    assert evidence.passed is False
    assert "no longer disarmed" in evidence.detail


def test_sharing_one_stop_file_between_the_lanes_fails(tree: Path) -> None:
    rules = {**RULES, "equities": {**RULES["equities"], "kill_switch": {"stop_file": "STOP_TRADING"}}}

    evidence = crypto_lane_untouched_evidence(tree, rules)

    assert evidence.passed is False
    assert "shares the crypto stop file" in evidence.detail


def test_the_repos_crypto_lane_is_still_disarmed() -> None:
    rules = yaml.safe_load((REPO_ROOT / "config" / "trading_rules.yaml").read_text(encoding="utf-8"))

    assert crypto_lane_untouched_evidence(REPO_ROOT, rules).passed is True


# --- the secret scan --------------------------------------------------------


def test_a_credential_shaped_literal_is_found(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "leaky.py").write_text('API_KEY = "sk-live-9f2b7c1d4e8a6b3f0d5c"\n', encoding="utf-8")

    findings = scan_for_secrets(tmp_path)

    assert [finding.name for finding in findings] == ["API_KEY"]
    assert no_secret_in_code_evidence(tmp_path, RULES).passed is False


def test_a_keyword_argument_carrying_a_credential_is_found(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "call.py").write_text('connect(api_key="9f2b7c1d4e8a6b3f0d5c1a2b")\n', encoding="utf-8")

    assert [finding.name for finding in scan_for_secrets(tmp_path)] == ["api_key"]


def test_a_private_key_block_is_found(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "key.yaml").write_text(
        "material: |\n  -----BEGIN EC PRIVATE KEY-----\n  aGVsbG8=\n", encoding="utf-8"
    )

    assert [finding.name for finding in scan_for_secrets(tmp_path)] == ["<pem block>"]


def test_an_env_var_reference_is_not_a_secret(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "config.py").write_text(
        'API_KEY_ENV = "ROBINHOOD_API_KEY"\napi_key = os.getenv("ROBINHOOD_API_KEY")\n', encoding="utf-8"
    )

    assert scan_for_secrets(tmp_path) == []


def test_a_documented_placeholder_is_not_a_secret(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(
        "api_key: your-key-goes-here\nclient_secret: <paste-from-the-console>\n", encoding="utf-8"
    )

    assert scan_for_secrets(tmp_path) == []


def test_a_short_value_is_not_a_secret(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "small.py").write_text('token = "abc"\n', encoding="utf-8")

    assert scan_for_secrets(tmp_path) == []


def test_the_allowlist_excuses_one_value_not_a_whole_file(tmp_path: Path) -> None:
    """Every allowlist entry pins a file AND a value together, so a new
    credential-shaped literal in an allowlisted file is still a finding."""
    path, value, _reason = ALLOWED_LITERALS[0]
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f'api_key = "{value}"\nother_token = "8f2b7c1d4e8a6b3f0d5c1a2b"\n', encoding="utf-8")

    findings = scan_for_secrets(tmp_path)

    assert [finding.name for finding in findings] == ["other_token"]


def test_the_same_value_elsewhere_is_still_a_finding(tmp_path: Path) -> None:
    _path, value, _reason = ALLOWED_LITERALS[0]
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "elsewhere.py").write_text(f'api_key = "{value}"\n', encoding="utf-8")

    assert [finding.name for finding in scan_for_secrets(tmp_path)] == ["api_key"]


def test_every_allowlist_entry_states_a_reason() -> None:
    for path, value, reason in ALLOWED_LITERALS:
        assert path and value and len(reason) > 30, path


def test_the_repo_itself_carries_no_secret_in_code() -> None:
    """The pinned assertion. Equities auth is the OAuth connector, so this
    lane has no key at all -- and nothing else may leak one either."""
    evidence = no_secret_in_code_evidence(REPO_ROOT, {})

    assert evidence.passed is True, evidence.detail


# --- rendering and audit ----------------------------------------------------


def test_the_markdown_report_enumerates_every_gate_with_a_verdict(tree: Path) -> None:
    report = report_for(tree)

    rendered = readiness_markdown(report)

    assert "READY: true" in rendered
    for gate in report["gates"]:
        assert gate["name"] in rendered
    assert rendered.count("| **PASS** |") == report["gates_total"]


def test_the_markdown_report_lists_what_is_blocking(tree: Path) -> None:
    report = report_for(tree, SuiteResult(returncode=0, outcomes={}))

    rendered = readiness_markdown(report)

    assert "READY: false" in rendered
    assert "collected no tests" in rendered


def test_the_verdict_is_written_to_the_audit_log(tree: Path) -> None:
    recorded: list[tuple] = []

    class RecordingLogger:
        def log_decision(self, symbol, action, reason, details=None):
            recorded.append((symbol, action, reason, details))

    equity_live_readiness(tree, suite_runner=lambda _root: green_suite(), logger=RecordingLogger())

    assert len(recorded) == 1
    _symbol, action, reason, details = recorded[0]
    assert action == "equity_live_readiness"
    assert "ready=True" in reason
    assert details["venue"] == "robinhood_equities"
    assert set(details["gates"]) == {gate.key for gate in GATES}


def test_the_posture_states_the_paper_quote_source_honestly(tree: Path) -> None:
    """The posture reports the source the recorded runs were ACTUALLY priced
    on, read off the audit log -- not the source the lane intends to use. The
    fixture tree's runs are on the real Massive feed, so it says so, names the
    required source, and keeps execution price on the connector."""
    report = report_for(tree)
    posture = report["posture"]
    paper = posture["paper_broker"].lower()

    assert posture["quote_source_required"] == REQUIRED_QUOTE_SOURCE == "massive"
    assert posture["quote_source_recorded"] == {REQUIRED_QUOTE_SOURCE: REQUIRED_CLEAN_PAPER_RUNS}
    assert posture["quote_source"].startswith("massive -- real Massive historical daily bars")
    assert "massive historical daily bars" in paper
    assert "synthetic" not in paper
    # The real source is the PROVING price only; a fill still prices off the
    # connector, and the posture has to keep the two apart.
    assert "connector" in posture["execution_price_source"].lower()
    assert "never a fill" in posture["execution_price_source"]


def test_the_posture_does_not_claim_a_real_source_the_runs_never_used(tmp_path: Path) -> None:
    """The honesty property, from the other side: when the recorded runs were
    priced on a generated series, the posture says that -- it never launders a
    synthetic run into a claim about real data."""
    (tmp_path / "data").mkdir()
    clean_paper_run(tmp_path / "data" / "trading_agent.db", quote_source="synthetic")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "trading_rules.yaml").write_text(yaml.safe_dump(RULES), encoding="utf-8")

    report = equity_live_readiness(tmp_path, suite_runner=lambda _root: green_suite())

    assert "no run priced on massive" in report["posture"]["quote_source"]
    assert report["posture"]["quote_source_recorded"] == {"synthetic": 1}
    assert report["ready"] is False


def test_the_report_does_not_change_any_posture(tree: Path) -> None:
    """The gate is a read. Nothing it does may arm or disarm a lane."""
    before = sorted(path.name for path in tree.iterdir())

    report_for(tree)

    assert sorted(path.name for path in tree.iterdir()) == before
    assert (tree / "STOP_TRADING").exists()
    assert yaml.safe_load((tree / "config" / "trading_rules.yaml").read_text(encoding="utf-8")) == RULES


# --- the gate table itself --------------------------------------------------


def test_every_gate_proving_test_exists_in_the_real_suite() -> None:
    """The gate table names real tests. A renamed test must be renamed here
    too, or its gate silently reads as ABSENT forever."""
    missing = []
    for gate in GATES:
        for nodeid in gate.proving_tests:
            path, _, name = nodeid.partition("::")
            source = REPO_ROOT / path
            if not source.exists() or f"def {name}(" not in source.read_text(encoding="utf-8"):
                missing.append(nodeid)

    assert missing == [], "\n".join(missing)


def test_every_gate_is_proven_by_tests_or_by_evidence() -> None:
    for gate in GATES:
        assert gate.proving_tests or gate.evidence is not None, gate.key


def test_gate_keys_are_unique() -> None:
    keys = [gate.key for gate in GATES]

    assert len(keys) == len(set(keys))


def test_evidence_returns_the_declared_shape(tree: Path) -> None:
    for gate in GATES:
        if gate.evidence is None:
            continue
        assert isinstance(gate.evidence(tree, RULES), Evidence), gate.key


# --- git hygiene: what a push would actually publish -------------------------
#
# The secret scan reads the SOURCE; these read what git would PUBLISH. A
# database full of audit rows carries no credential-shaped literal, and
# `git check-ignore` says nothing about a file committed before the rule
# existed -- so each direction needs its own negative test.


def test_git_hygiene_passes_on_this_repo() -> None:
    """The real checkout, not a fixture: nothing that leaks is tracked."""
    evidence = git_hygiene_evidence(REPO_ROOT, RULES)

    assert evidence.passed, evidence.detail
    assert evidence.data["leaked_tracked_paths"] == []
    assert evidence.data["unignored_runtime_artifacts"] == []
    assert evidence.data["over_ignored_committed_paths"] == []


def test_git_hygiene_fails_when_a_dotenv_is_tracked(tree: Path) -> None:
    """A committed .env is the leak this whole posture exists to prevent, and
    -f is exactly how one gets past the ignore rule by accident."""
    (tree / ".env").write_text("ROBINHOOD_API_KEY=sk-not-a-real-value\n", encoding="utf-8")
    git(tree, "add", "-f", ".env")
    git(tree, "commit", "-qm", "oops")

    evidence = git_hygiene_evidence(tree, RULES)

    assert not evidence.passed
    assert ".env" in evidence.data["leaked_tracked_paths"]
    assert "already tracked" in evidence.detail


def test_git_hygiene_fails_when_a_database_is_tracked(tree: Path) -> None:
    """The audit database holds every decision and position the lane took. It
    carries no credential literal, so only this check catches it."""
    git(tree, "add", "-f", "data/trading_agent.db")
    git(tree, "commit", "-qm", "oops")

    evidence = git_hygiene_evidence(tree, RULES)

    assert not evidence.passed
    assert "data/trading_agent.db" in evidence.data["leaked_tracked_paths"]


def test_git_hygiene_fails_when_a_runtime_artifact_is_not_ignored(tree: Path) -> None:
    """Drop the logs/ rule: the audit exports become stageable by `git add -A`.
    The weakened .gitignore is COMMITTED, because the gate reads HEAD -- an
    uncommitted weakening trips the separate porcelain guard first."""
    remaining = [line for line in FIXTURE_GITIGNORE.splitlines() if line != "logs/"]
    (tree / ".gitignore").write_text("\n".join(remaining) + "\n", encoding="utf-8")
    git(tree, "add", ".gitignore")
    git(tree, "commit", "-qm", "weaken ignore rules")

    evidence = git_hygiene_evidence(tree, RULES)

    assert not evidence.passed
    assert "logs/live_audit_example.json" in evidence.data["unignored_runtime_artifacts"]
    assert "not git-ignored" in evidence.detail


def test_git_hygiene_fails_when_an_ignore_rule_is_too_wide(tree: Path) -> None:
    """The opposite failure: an ignore rule so broad it swallows the documented
    placeholder file. Silently un-committing .env.example is a defect too."""
    (tree / ".gitignore").write_text(FIXTURE_GITIGNORE + "\n.env*\n", encoding="utf-8")
    git(tree, "add", ".gitignore")
    git(tree, "commit", "-qm", "over-broad ignore rule")

    evidence = git_hygiene_evidence(tree, RULES)

    assert not evidence.passed
    assert ".env.example" in evidence.data["over_ignored_committed_paths"]
    assert "must stay committed" in evidence.detail


# A weak, HEAD-like .gitignore from before the hardening: it ignores .env, logs
# and the database itself, but NOT the SQLite sidecars or the extra sqlite
# extensions. Committing this is exactly the regression the hardening prevents.
WEAK_GITIGNORE = """.env
.env.*
!.env.example
logs/
data/*.db
__pycache__/
*.pyc
.venv/
.pytest_cache/
"""


def test_git_hygiene_fails_when_the_gitignore_edit_is_not_committed(tree: Path) -> None:
    """The porcelain guard itself: a working-copy edit that is not committed is
    not protection, because a clone and CI read HEAD, not the operator's disk.
    Reverting the HEAD/porcelain check lets an uncommitted .gitignore pass."""
    # A perfectly correct edit -- but left uncommitted.
    (tree / ".gitignore").write_text(FIXTURE_GITIGNORE + "\ndata/extra/\n", encoding="utf-8")

    evidence = git_hygiene_evidence(tree, RULES)

    assert not evidence.passed
    assert "uncommitted changes" in evidence.detail
    assert evidence.data["porcelain"]


def test_git_hygiene_fails_when_gitignore_is_reverted_to_a_weak_head_version(tree: Path) -> None:
    """Revert the committed .gitignore to the weak, pre-hardening HEAD shape and
    the gate must FAIL: the SQLite sidecars become stageable by `git add -A`
    again. This is the mutation test for the hardened ignore rules -- undo them
    at HEAD and this test goes red."""
    (tree / ".gitignore").write_text(WEAK_GITIGNORE, encoding="utf-8")
    git(tree, "add", ".gitignore")
    git(tree, "commit", "-qm", "revert to weak ignore rules")

    evidence = git_hygiene_evidence(tree, RULES)

    assert not evidence.passed
    unignored = evidence.data["unignored_runtime_artifacts"]
    assert "data/trading_agent.db-journal" in unignored
    assert "data/trading_agent.db-wal" in unignored
    assert "data/trading_agent.db-shm" in unignored


def test_committed_gitignore_is_a_superset_of_the_required_rules() -> None:
    """The real repo's COMMITTED .gitignore (HEAD, not the working copy) must
    contain every rule the fixture pins as required. A hardening that only lands
    in the working copy, or that drops one of the required rules, fails here."""
    head = subprocess.run(
        ["git", "show", "HEAD:.gitignore"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert head.returncode == 0, ".gitignore must be committed at HEAD"
    committed = {line.strip() for line in head.stdout.splitlines() if line.strip()}
    required = {line.strip() for line in FIXTURE_GITIGNORE.splitlines() if line.strip()}
    missing = required - committed
    assert not missing, f"committed .gitignore is missing required rules: {sorted(missing)}"


@pytest.mark.parametrize("sidecar", ["-journal", "-wal", "-shm"])
def test_sqlite_sidecars_are_ignored_not_just_the_database(tree: Path, sidecar: str) -> None:
    """`data/*.db` does not match `data/x.db-wal`. A run interrupted mid-write
    leaves one behind holding the same audit rows as the database itself, so
    each sidecar needs its own rule -- this is the gap that check found."""
    artifact = tree / "data" / f"trading_agent.db{sidecar}"
    artifact.write_text("fragment\n", encoding="utf-8")

    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(tree), capture_output=True, text=True, check=False
    )

    assert artifact.name not in result.stdout, f"{artifact.name} is stageable by `git add -A`"
    assert git_hygiene_evidence(tree, RULES).passed
