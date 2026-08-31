"""Tests for the options lane's live-readiness gate (src/option_readiness.py).

The point of the readiness report is that it cannot lie in the operator's
favour, so most of what is tested here is the NEGATIVE direction: a failing
test, a deleted test, a loosened options cap, a missing paper run, a forked
account anchor and a shared kill-switch file must each drive ready to false on
their own.

No test in this file runs the real pytest suite -- `option_live_readiness` takes
its suite runner as an argument precisely so a test can hand it a synthetic
result.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
import yaml

from src.option_runtime import (
    ACTION_COMPLETED,
    ACTION_FILLED,
    ACTION_RECONCILE,
    BASIS_EXPECTED_MOVE,
    UNRECORDED_BASIS,
)
from src.option_readiness import (
    GATES,
    OPTION_ORDER_SAFETY_GUARD,
    REQUIRED_CLEAN_PAPER_RUNS,
    Evidence,
    SuiteResult,
    evaluate_gate,
    git_hygiene_evidence,
    lane_isolation_evidence,
    option_account_confinement_evidence,
    option_basis_summary,
    option_live_readiness,
    option_paper_runs_evidence,
    option_risk_caps_evidence,
    readiness_markdown,
    readiness_verdict,
    recorded_option_bases,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every node id any gate names. A synthetic all-green suite is built from this so
# the fixtures never drift from the gate table.
ALL_PROVING_TESTS = tuple(sorted({nodeid for gate in GATES for nodeid in gate.proving_tests}))


def green_suite(**overrides: str) -> SuiteResult:
    outcomes = dict.fromkeys(ALL_PROVING_TESTS, "PASSED")
    outcomes.update(overrides)
    return SuiteResult(returncode=0, outcomes=outcomes)


# --- a tree the report can be pointed at ------------------------------------

RULES = {
    "kill_switch": {"stop_file": "STOP_TRADING", "env_var": "TRADING_ENABLED"},
    "equities": {
        "kill_switch": {"stop_file": "STOP_TRADING_EQUITIES", "env_var": "TRADING_ENABLED"},
        "expected_account": {"nickname": "Agentic", "number_suffix": "2092"},
        "universe": ["AAPL", "MSFT"],
    },
    "options": {
        "kill_switch": {"stop_file": "STOP_TRADING_OPTIONS", "env_var": "TRADING_ENABLED"},
        "universe": ["AAPL", "MSFT"],
        "risk": {
            "max_debit_premium_per_trade_usd": 500.0,
            "max_total_premium_at_risk_usd": 1500.0,
            "contract_multiplier": 100,
            "min_days_to_expiry": 2,
            "allow_zero_dte": False,
            "max_contracts_per_order": 5,
            "strategy_min_option_level": {
                "reducing": 0,
                "single_leg_long": 2,
                "long_multi_leg": 3,
                "defined_risk_spread": 3,
            },
        },
    },
}

BINDING_DOC = """# binding
The designated account is nickname "Agentic", ending 2092.
"""

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

FILL_RATIONALE = "single_leg_long+conviction_70+rank_1.0"


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
            ("2026-08-30T00:00:00+00:00", symbol, action, reason, json.dumps(details)),
        )


def option_fill(db: Path, symbol: str = "AAPL", notional: float = 300.0) -> None:
    log_decision(
        db,
        ACTION_FILLED,
        {"contract": f"{symbol}_C", "underlying": symbol, "notional": notional, "premium": 3.0},
        symbol=symbol,
        reason=FILL_RATIONALE,
    )


def clean_option_run(
    db: Path,
    iterations: int = 12,
    symbol: str = "AAPL",
    fills: int = 3,
    quote_basis: str | None = BASIS_EXPECTED_MOVE,
) -> None:
    """One GENUINE unattended bounded options loop: real defined-risk fills,
    priced on a REAL basis, the loop that finished recording provenance that
    SUBSTANTIATES that basis, then a clean reconcile. A run with no fills is not
    clean; nor is one priced on anything but a real basis (`quote_basis=None` / a
    made-up string models that); nor is one whose provenance does not back its
    declared basis (which a real basis with no `basis_provenance` would model)."""
    for _ in range(fills):
        option_fill(db, symbol=symbol)
    details: dict = {"iterations_completed": iterations, "fills": fills}
    if quote_basis is not None:
        details["quote_basis"] = quote_basis
        if quote_basis == BASIS_EXPECTED_MOVE:
            # What a genuine expected-move run writes: the real Massive underlying
            # window behind the premiums, which the counting logic validates.
            details["basis_provenance"] = {
                "basis": quote_basis,
                "underlying_source": {
                    "quote_source": "massive",
                    "total_bars": iterations,
                    "from_date": "2024-01-01",
                    "to_date": "2024-06-30",
                },
            }
    log_decision(db, ACTION_COMPLETED, details)
    log_decision(db, ACTION_RECONCILE, {"errors": []})


def git(root: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"


def init_repo(root: Path) -> None:
    git(root, "init", "-q")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "fixture")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "fixture")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A minimal tree shaped like the real one: config, binding doc, options
    guard test, stop files, an audit database with two clean options paper runs,
    and a git repo whose ignore rules match the real ones."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "trading_rules.yaml").write_text(yaml.safe_dump(RULES), encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "rh-equities-binding.md").write_text(BINDING_DOC, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_option_order_safety_guard.py").write_text("# guard\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lane.py").write_text("LEGS = []\n", encoding="utf-8")
    (tmp_path / "STOP_TRADING").write_text("disarmed\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(FIXTURE_GITIGNORE, encoding="utf-8")
    (tmp_path / ".env.example").write_text("ROBINHOOD_API_KEY=\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    database = tmp_path / "data" / "trading_agent.db"
    for _ in range(REQUIRED_CLEAN_PAPER_RUNS):
        clean_option_run(database)
    init_repo(tmp_path)
    return tmp_path


def report_for(tree: Path, suite: SuiteResult | None = None) -> dict:
    return option_live_readiness(tree, suite_runner=lambda _root: suite or green_suite())


# --- the verdict ------------------------------------------------------------


def test_ready_is_true_when_the_suite_is_green_and_every_gate_passes(tree: Path) -> None:
    report = report_for(tree)

    assert report["ready"] is True, report["blocking_reasons"]
    assert report["blocking_reasons"] == []
    assert report["gates_passed"] == report["gates_total"]


def test_every_expected_gate_is_enumerated(tree: Path) -> None:
    report = report_for(tree)
    keys = {gate["key"] for gate in report["gates"]}

    assert keys >= {
        OPTION_ORDER_SAFETY_GUARD,
        "options_lane_armed",
        "human_gate",
        "kill_switch",
        "defined_risk_only",
        "account_confinement",
        "options_risk_caps",
        "paper_proven_twice",
        "no_secret_in_code",
        "git_hygiene",
    }
    for gate in report["gates"]:
        assert isinstance(gate["passed"], bool)
        assert gate["name"] and gate["why"]


def test_a_single_failing_test_makes_ready_false(tree: Path) -> None:
    failing = ALL_PROVING_TESTS[0]
    report = report_for(tree, green_suite(**{failing: "FAILED"}))

    assert report["ready"] is False
    assert any(failing in reason for reason in report["blocking_reasons"])


def test_a_failing_test_outside_every_gate_still_makes_ready_false(tree: Path) -> None:
    suite = green_suite()
    suite = SuiteResult(returncode=1, outcomes={**suite.outcomes, "tests/test_unrelated.py::test_x": "FAILED"})
    report = report_for(tree, suite)

    assert report["ready"] is False
    assert any("failed" in reason for reason in report["blocking_reasons"])


def test_an_empty_suite_makes_ready_false(tree: Path) -> None:
    report = report_for(tree, SuiteResult(returncode=0, outcomes={}))

    assert report["ready"] is False
    assert any("collected no tests" in reason for reason in report["blocking_reasons"])


def test_an_absent_options_guard_test_makes_ready_false(tree: Path) -> None:
    """Deleting a proving test reads exactly like failing it."""
    guard = next(gate for gate in GATES if gate.key == OPTION_ORDER_SAFETY_GUARD)
    outcomes = {node: "PASSED" for node in ALL_PROVING_TESTS if node not in guard.proving_tests}
    report = report_for(tree, SuiteResult(returncode=0, outcomes=outcomes))

    assert report["ready"] is False
    guard_row = next(row for row in report["gates"] if row["key"] == OPTION_ORDER_SAFETY_GUARD)
    assert guard_row["passed"] is False
    assert all("ABSENT" in reason for reason in guard_row["reasons"])


def test_deleting_the_guard_file_makes_ready_false_even_with_a_green_suite(tree: Path) -> None:
    (tree / "tests" / "test_option_order_safety_guard.py").unlink()
    report = report_for(tree)

    assert report["ready"] is False
    guard_row = next(row for row in report["gates"] if row["key"] == OPTION_ORDER_SAFETY_GUARD)
    assert guard_row["evidence"]["passed"] is False


def test_the_verdict_refuses_a_report_with_no_guard_gate_at_all() -> None:
    ready, blocking = readiness_verdict(
        green_suite(), [{"key": "kill_switch", "name": "Kill switch", "passed": True, "reasons": []}]
    )

    assert ready is False
    assert any("order-safety guard gate is absent" in reason for reason in blocking)


def test_the_verdict_is_a_pure_function_of_the_suite_and_the_gates() -> None:
    gates = [{"key": OPTION_ORDER_SAFETY_GUARD, "name": "Options order-safety guard", "passed": True, "reasons": []}]

    assert readiness_verdict(green_suite(), gates) == (True, [])
    assert readiness_verdict(SuiteResult(returncode=1, outcomes={"a.py::b": "FAILED"}), gates)[0] is False


# --- options risk caps ------------------------------------------------------


def test_caps_within_bounds_pass(tree: Path) -> None:
    assert option_risk_caps_evidence(tree, RULES).passed is True


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_debit_premium_per_trade_usd", 2000.0),
        ("max_total_premium_at_risk_usd", 9000.0),
        ("min_days_to_expiry", 0),
        ("max_contracts_per_order", 50),
    ],
)
def test_a_loosened_cap_fails_the_options_risk_gate(tree: Path, key: str, value: float) -> None:
    rules = {**RULES, "options": {**RULES["options"], "risk": {**RULES["options"]["risk"], key: value}}}

    evidence = option_risk_caps_evidence(tree, rules)

    assert evidence.passed is False
    assert key in evidence.detail


def test_a_missing_cap_is_not_a_permissive_default(tree: Path) -> None:
    risk = {k: v for k, v in RULES["options"]["risk"].items() if k != "max_debit_premium_per_trade_usd"}
    rules = {**RULES, "options": {**RULES["options"], "risk": risk}}

    evidence = option_risk_caps_evidence(tree, rules)

    assert evidence.passed is False
    assert "not configured" in evidence.detail


def test_turning_on_zero_dte_fails_the_gate(tree: Path) -> None:
    rules = {**RULES, "options": {**RULES["options"], "risk": {**RULES["options"]["risk"], "allow_zero_dte": True}}}

    evidence = option_risk_caps_evidence(tree, rules)

    assert evidence.passed is False
    assert "allow_zero_dte" in evidence.detail


def test_dropping_a_level_floor_fails_the_gate(tree: Path) -> None:
    """A defined-risk spread demanding only level 2 is below the level-3 floor."""
    levels = {**RULES["options"]["risk"]["strategy_min_option_level"], "defined_risk_spread": 2}
    rules = {
        **RULES,
        "options": {**RULES["options"], "risk": {**RULES["options"]["risk"], "strategy_min_option_level": levels}},
    }

    evidence = option_risk_caps_evidence(tree, rules)

    assert evidence.passed is False
    assert "defined_risk_spread" in evidence.detail


def test_a_missing_options_section_fails(tree: Path) -> None:
    assert option_risk_caps_evidence(tree, {"kill_switch": {}}).passed is False


def test_the_repos_own_options_caps_are_within_bounds() -> None:
    rules = yaml.safe_load((REPO_ROOT / "config" / "trading_rules.yaml").read_text(encoding="utf-8"))

    evidence = option_risk_caps_evidence(REPO_ROOT, rules)

    assert evidence.passed is True, evidence.detail


# --- the paper proving runs -------------------------------------------------


def test_two_clean_option_runs_satisfy_the_paper_gate(tree: Path) -> None:
    evidence = option_paper_runs_evidence(tree, RULES)

    assert evidence.passed is True
    assert evidence.data["clean_run_count"] == REQUIRED_CLEAN_PAPER_RUNS


def test_one_clean_run_is_not_enough(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    clean_option_run(tmp_path / "data" / "trading_agent.db")

    evidence = option_paper_runs_evidence(tmp_path, RULES)

    assert evidence.passed is False
    assert "1 clean unattended options paper run" in evidence.detail


def test_a_run_priced_on_a_made_up_basis_is_not_clean_at_the_gate(tmp_path: Path) -> None:
    """A run perfect in every other respect -- real fills, its own clean
    reconcile -- still does not count if it was priced on a made-up basis. Only a
    real basis counts."""
    (tmp_path / "data").mkdir()
    database = tmp_path / "data" / "trading_agent.db"
    for _ in range(REQUIRED_CLEAN_PAPER_RUNS):
        clean_option_run(database, quote_basis="made_up")

    evidence = option_paper_runs_evidence(tmp_path, RULES)

    assert evidence.passed is False
    assert evidence.data["clean_run_count"] == 0
    assert "not priced on a real basis" in evidence.detail


def test_a_run_that_recorded_no_basis_is_not_clean(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    clean_option_run(tmp_path / "data" / "trading_agent.db", quote_basis=None)

    assert recorded_option_bases(tmp_path) == {UNRECORDED_BASIS: 1}
    assert option_paper_runs_evidence(tmp_path, RULES).passed is False


def test_a_zero_fill_run_is_not_clean(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    database = tmp_path / "data" / "trading_agent.db"
    log_decision(database, ACTION_COMPLETED, {"iterations_completed": 40, "quote_basis": BASIS_EXPECTED_MOVE})
    log_decision(database, ACTION_RECONCILE, {"errors": []})

    assert option_paper_runs_evidence(tmp_path, RULES).passed is False


def test_no_audit_database_means_no_proven_runs(tmp_path: Path) -> None:
    assert option_paper_runs_evidence(tmp_path, RULES).passed is False
    assert "no proving run recorded yet" in option_basis_summary(tmp_path)


# --- the account anchor and lane isolation ----------------------------------


def test_account_confinement_passes_on_the_fixture(tree: Path) -> None:
    assert option_account_confinement_evidence(tree, RULES).passed is True


def test_a_forked_account_anchor_fails(tree: Path) -> None:
    """The options lane must pin the SAME anchor as the equities lane; a config
    that changes nickname or suffix is a forked anchor and fails."""
    rules = {
        **RULES,
        "equities": {**RULES["equities"], "expected_account": {"nickname": "Agentic", "number_suffix": "9999"}},
    }
    (tree / "config" / "trading_rules.yaml").write_text(yaml.safe_dump(rules), encoding="utf-8")

    evidence = option_account_confinement_evidence(tree, rules)

    assert evidence.passed is False
    assert "9999" in evidence.detail or "anchor" in evidence.detail


def test_the_account_gate_needs_the_binding_doc(tree: Path) -> None:
    (tree / "docs" / "rh-equities-binding.md").unlink()

    evidence = option_account_confinement_evidence(tree, RULES)

    assert evidence.passed is False
    assert "binding" in evidence.detail


def test_an_account_number_routed_to_by_literal_fails(tree: Path) -> None:
    (tree / "src" / "router.py").write_text(
        'def send(client):\n    return client.place(account_number="99887766")\n', encoding="utf-8"
    )

    evidence = option_account_confinement_evidence(tree, RULES)

    assert evidence.passed is False
    assert "router.py" in evidence.detail


def test_the_repo_pins_the_shared_anchor_and_routes_no_literal() -> None:
    rules = yaml.safe_load((REPO_ROOT / "config" / "trading_rules.yaml").read_text(encoding="utf-8"))

    assert option_account_confinement_evidence(REPO_ROOT, rules).passed is True


def test_lane_isolation_passes_on_the_fixture(tree: Path) -> None:
    assert lane_isolation_evidence(tree, RULES).passed is True


def test_deleting_the_crypto_stop_file_fails_lane_isolation(tree: Path) -> None:
    (tree / "STOP_TRADING").unlink()

    evidence = lane_isolation_evidence(tree, RULES)

    assert evidence.passed is False
    assert "no longer disarmed" in evidence.detail


def test_sharing_the_crypto_stop_file_fails_lane_isolation(tree: Path) -> None:
    rules = {**RULES, "options": {**RULES["options"], "kill_switch": {"stop_file": "STOP_TRADING"}}}

    evidence = lane_isolation_evidence(tree, rules)

    assert evidence.passed is False
    assert "shares the crypto stop file" in evidence.detail


def test_sharing_the_equities_stop_file_fails_lane_isolation(tree: Path) -> None:
    rules = {**RULES, "options": {**RULES["options"], "kill_switch": {"stop_file": "STOP_TRADING_EQUITIES"}}}

    evidence = lane_isolation_evidence(tree, rules)

    assert evidence.passed is False
    assert "shares the equities stop file" in evidence.detail


def test_the_repos_lanes_are_isolated_and_crypto_is_disarmed() -> None:
    rules = yaml.safe_load((REPO_ROOT / "config" / "trading_rules.yaml").read_text(encoding="utf-8"))

    assert lane_isolation_evidence(REPO_ROOT, rules).passed is True


# --- git hygiene (referenced by the gate table) -----------------------------


def test_option_git_hygiene_passes_on_this_repo() -> None:
    evidence = git_hygiene_evidence(REPO_ROOT, RULES)

    assert evidence.passed, evidence.detail


def test_option_git_hygiene_fails_when_a_database_is_tracked(tree: Path) -> None:
    git(tree, "add", "-f", "data/trading_agent.db")
    git(tree, "commit", "-qm", "oops")

    evidence = git_hygiene_evidence(tree, RULES)

    assert not evidence.passed
    assert "data/trading_agent.db" in evidence.data["leaked_tracked_paths"]


# --- the secret scan (repo-wide) --------------------------------------------


def test_the_repo_itself_carries_no_secret_in_code() -> None:
    from src.option_readiness import no_secret_in_code_evidence

    evidence = no_secret_in_code_evidence(REPO_ROOT, {})

    assert evidence.passed is True, evidence.detail


# --- rendering and audit ----------------------------------------------------


def test_the_markdown_report_enumerates_every_gate(tree: Path) -> None:
    report = report_for(tree)

    rendered = readiness_markdown(report)

    assert "READY: true" in rendered
    assert "Options lane -- live-readiness report" in rendered
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

    option_live_readiness(tree, suite_runner=lambda _root: green_suite(), logger=RecordingLogger())

    assert len(recorded) == 1
    _symbol, action, reason, details = recorded[0]
    assert action == "option_live_readiness"
    assert "ready=True" in reason
    assert details["venue"] == "robinhood_options"
    assert set(details["gates"]) == {gate.key for gate in GATES}


def test_the_report_does_not_change_any_posture(tree: Path) -> None:
    """The gate is a read. Nothing it does may arm or disarm a lane."""
    before = sorted(path.name for path in tree.iterdir())

    report_for(tree)

    assert sorted(path.name for path in tree.iterdir()) == before
    assert (tree / "STOP_TRADING").exists()
    assert not (tree / "STOP_TRADING_OPTIONS").exists()
    assert yaml.safe_load((tree / "config" / "trading_rules.yaml").read_text(encoding="utf-8")) == RULES


# --- the gate table itself --------------------------------------------------


def test_every_gate_proving_test_exists_in_the_real_suite() -> None:
    """The gate table names real tests. A renamed test must be renamed here too,
    or its gate silently reads as ABSENT forever."""
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
