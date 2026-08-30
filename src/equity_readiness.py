"""The equities lane's live-readiness gate -- the analog of main.py's
`live-readiness` command for the crypto lane.

WHAT THIS IS FOR
    Before anyone flips the equities lane from paper to live, one command has
    to answer a single question honestly: is every safety property this lane
    claims to have actually PROVEN right now, on this tree, in this checkout?
    Not "was it proven when it was written" -- proven by a run that just
    happened.

HOW A GATE IS PROVEN (this is the whole design)
    A gate is proven by NAMED TESTS THAT MUST EXIST AND MUST PASS. Each gate
    below carries the pytest node ids that demonstrate it. The report runs the
    project's full suite once, records the outcome of every test, and then asks
    of each gate: are all of my proving tests present in that run, and did all
    of them pass?

    That makes two failure modes indistinguishable from each other, which is
    the point:
      - a proving test FAILED   -> the gate is not proven -> ready:false
      - a proving test is ABSENT -> the gate is not proven -> ready:false
    Deleting the test that proves a gate can therefore never turn a red gate
    green. It reads exactly like a failure, because it is one.

    A few gates carry additional non-test EVIDENCE that no unit test can
    speak to -- what the config caps are actually set to right now, whether
    the paper lane really did run twice unattended and reconcile clean, and
    whether any credential-shaped literal has appeared in the source. A gate
    with evidence passes only if BOTH its tests and its evidence pass.

WHAT ready:true MEANS
    Every gate proven, the full suite green, and the suite non-vacuous (a run
    that collected zero tests is not a green run). `readiness_verdict` is a
    pure function of the suite result and the gate rows so it can be tested
    directly, and it re-asserts the two conditions that must never be
    negotiable -- suite green, and the order-symbol guard proven -- after the
    generic per-gate loop, so no future gate-table edit can route around them.

WHAT THIS DELIBERATELY DOES NOT DO
    It does not touch the connector, does not place or preview an order, and
    does not change any posture. It is a read: run the tests, read the config,
    read the audit log, scan the source. Switching paper->live remains the
    operator's explicit call and is never a side effect of running this.
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import yaml

VENUE = "robinhood_equities"

# The gate whose proving tests are re-asserted by name in readiness_verdict.
ORDER_SYMBOL_GUARD = "order_symbol_guard"

# A paper proving run counts only if the lane completed a BOUNDED loop
# unattended and the reconcile that followed it came back clean. Two of them.
REQUIRED_CLEAN_PAPER_RUNS = 2

# A decision logged inside a run's window that is a MANUAL state mutation --
# a daily-counter reset being the one this repo actually produced -- means the
# run was not left alone: someone reached in and changed the lane's state, so
# it no longer counts as an independent, unattended run. Matched by action name.
_MANUAL_MUTATION = re.compile(r"(counter_reset|_reset|daily_reset|manual_)", re.IGNORECASE)

# A run's ledger has to have actually MOVED by at least this much (summed
# absolute fill notional in the window) to count -- a run that filled nothing
# proves the loop can idle, not that the lane can trade and reconcile.
_LEDGER_DELTA_EPSILON = 1e-9

# --- the risk caps this lane may not exceed ---------------------------------
# Mirrors the bounds main.py's _bounded_live_gate_reasons enforces for crypto,
# read here as evidence rather than re-implemented as a second risk path.
CAP_BOUNDS: tuple[tuple[str, str, float], ...] = (
    ("max_trade_amount_usd", "<=", 100.0),
    ("max_daily_loss_usd", "<=", 100.0),
    ("max_trades_per_day", "<=", 5),
    ("max_open_positions", "<=", 10),
    ("max_symbol_allocation_percent", "<=", 25.0),
    ("min_order_cooldown_seconds", ">=", 300),
)

# Flags that must be OFF for this lane, whatever else the config says.
FORBIDDEN_FLAGS: tuple[str, ...] = ("allow_margin", "allow_shorts", "allow_shorting")


# ---------------------------------------------------------------------------
# running the suite
# ---------------------------------------------------------------------------

# "tests/test_x.py::test_y PASSED [ 42%]" and its parametrized form
# "tests/test_x.py::test_y[True-False-dry_run off] PASSED [ 42%]" -- a param id
# really can contain spaces, so the node id is matched lazily and the line is
# anchored at its end instead of splitting on the first whitespace. The outcome
# word is matched generically rather than enumerated, so an outcome this code
# has never heard of is still recorded -- and, per _PROVEN below, still counts
# as not proving anything.
_RESULT_LINE = re.compile(r"^(?P<nodeid>\S+\.py::.+?)\s+(?P<outcome>[A-Z]+)(?:\s+\[\s*\d+%\])?\s*$")

# The ONLY outcome that counts as "this test proved its point". A skipped,
# expected-failure or unknown outcome proves nothing and is treated as not
# passing, on purpose, so a skip mark cannot quietly retire a safety property.
_PROVEN = "PASSED"

# Outcomes that mean the test actively went red, as opposed to merely not
# having proven anything.
_RED = {"FAILED", "ERROR"}


@dataclass(frozen=True)
class SuiteResult:
    """The outcome of one full pytest run, per test."""

    returncode: int
    outcomes: dict[str, str]  # base node id (no [params]) -> worst outcome seen
    output_tail: str = ""

    @property
    def tests_run(self) -> int:
        """Distinct tests recorded. Parametrized cases roll up under one base
        node id, so this is lower than the raw case count pytest prints -- the
        gate table names tests, not parameter cases."""
        return len(self.outcomes)

    @property
    def failed(self) -> list[str]:
        return sorted(node for node, outcome in self.outcomes.items() if outcome in _RED)

    @property
    def passed(self) -> bool:
        """Green means pytest itself said so AND at least one test ran.

        pytest exits 5 on "no tests collected"; a suite that collected nothing
        is not evidence of anything, so it is never green here.
        """
        return self.returncode == 0 and self.tests_run > 0 and not self.failed


def _base_nodeid(nodeid: str) -> str:
    """Strip a parametrized suffix so `test_x[a-b]` rolls up under `test_x`."""
    return nodeid.replace("\\", "/").split("[", 1)[0]


def parse_pytest_output(output: str) -> dict[str, str]:
    """Per-test outcomes from `pytest -v` output, keyed by base node id.

    When a test is parametrized, every case rolls up under the base id and the
    worst outcome wins -- one failing parameter case fails the whole proving
    test, which is the conservative reading.
    """

    def severity(outcome: str) -> int:
        if outcome in _RED:
            return 2
        return 0 if outcome == _PROVEN else 1

    outcomes: dict[str, str] = {}
    for line in output.splitlines():
        match = _RESULT_LINE.match(line.rstrip())
        if not match:
            continue
        node = _base_nodeid(match.group("nodeid"))
        outcome = match.group("outcome")
        previous = outcomes.get(node)
        if previous is None or severity(outcome) > severity(previous):
            outcomes[node] = outcome
    return outcomes


def run_project_test_suite(root: Path) -> SuiteResult:
    """Run the whole pytest suite once, verbosely, and record every outcome."""
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-v", "--tb=short", "-p", "no:cacheprovider"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    return SuiteResult(
        returncode=completed.returncode,
        outcomes=parse_pytest_output(output),
        output_tail=output[-2000:],
    )


# ---------------------------------------------------------------------------
# evidence beyond the tests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    passed: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


def load_rules(root: Path) -> dict[str, Any]:
    path = root / "config" / "trading_rules.yaml"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _compare(value: float, operator: str, bound: float) -> bool:
    return value <= bound if operator == "<=" else value >= bound


def risk_caps_evidence(root: Path, rules: dict[str, Any]) -> Evidence:
    """The caps in config/trading_rules.yaml right now, against the bounds
    this lane may not exceed. A cap that is missing entirely fails -- an
    absent cap is not a permissive default here."""
    risk = rules.get("risk", {})
    failures: list[str] = []
    observed: dict[str, Any] = {}
    for key, operator, bound in CAP_BOUNDS:
        raw = risk.get(key)
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
        if key == "max_symbol_allocation_percent" and value <= 0:
            failures.append(f"{key}={value:g} must be greater than 0")
    for flag in FORBIDDEN_FLAGS:
        observed[flag] = risk.get(flag, False)
        if bool(risk.get(flag, False)):
            failures.append(f"{flag} is true; this lane is long-only cash equities")
    universe = rules.get("equities", {}).get("universe", []) or []
    observed["equities_universe"] = list(universe)
    if not universe:
        failures.append("equities.universe is empty; the lane has no allowlist to enforce")
    if failures:
        return Evidence(False, "; ".join(failures), observed)
    return Evidence(
        True,
        f"all {len(CAP_BOUNDS)} caps within bounds, margin/shorts off, "
        f"{len(universe)} symbol(s) allowlisted",
        observed,
    )


# An account number reaching the order path must come from the connector's own
# account list, never from a literal. These are the identifier names that
# actually route an order; a dict key of prose ("Designated account number" on
# the dashboard) is not one of them, which is why an identifier match is
# required rather than a raw text search for the digits.
_ACCOUNT_ROUTING_NAME = re.compile(r"^(account_number|account_id|accountnumber|acct_number)$", re.IGNORECASE)
_ACCOUNT_DIGITS = re.compile(r"\d{3,}")


def hardcoded_account_literals(root: Path) -> list[str]:
    """`account_number="...1234"` anywhere under src/ -- an account routed to
    by a literal instead of by the connector's resolved, pinned account."""
    found: list[str] = []
    for path in sorted((root / "src").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = str(path.relative_to(root)).replace("\\", "/")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            found.append(f"{relative}: could not be parsed")
            continue

        def record(name: str, node: ast.AST, value: str) -> None:
            if _ACCOUNT_ROUTING_NAME.match(name) and _ACCOUNT_DIGITS.search(value):
                found.append(f"{relative}:{getattr(node, 'lineno', 0)} {name}={value!r}")

        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg and isinstance(node.value, ast.Constant):
                record(node.arg, node.value, str(node.value.value))
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                for target in node.targets:
                    name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
                    if name:
                        record(name, node, str(node.value.value))
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and isinstance(key.value, str) and isinstance(value, ast.Constant):
                        if key.value.isidentifier():
                            record(key.value, value, str(value.value))
    return found


def agentic_account_evidence(root: Path, rules: dict[str, Any]) -> Evidence:
    """The binding doc must still designate a single agent-tradable account,
    and no source file may route an order to an account by literal.

    The account itself is resolved at RUNTIME from the connector's own account
    list -- RobinhoodEquityClient pins the one account Robinhood flags as
    agent-tradable and refuses every other -- so there is deliberately no
    account number in the code to check against. What IS checkable statically
    is that the decision doc recording the binding still says the same thing,
    and that nothing under src/ assigns an account number to an order-routing
    argument as a literal.
    """
    doc = root / "docs" / "rh-equities-binding.md"
    if not doc.exists():
        return Evidence(False, "docs/rh-equities-binding.md is missing; the account binding is unrecorded")
    text = doc.read_text(encoding="utf-8", errors="replace")
    missing = [phrase for phrase in ('"Agentic"', "2092") if phrase not in text]
    if missing:
        return Evidence(False, f"the binding doc no longer records: {', '.join(missing)}")
    literals = hardcoded_account_literals(root)
    if literals:
        return Evidence(False, f"an account number is routed to by literal: {'; '.join(literals)}", {"literals": literals})
    return Evidence(
        True,
        'the binding doc still designates a single agent-tradable account (nickname "Agentic", the ..2092 cash account); '
        "nothing under src/ routes to an account by literal -- the client resolves and asserts it at runtime",
    )


def _decision_rows(db_path: Path, actions: tuple[str, ...]) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    placeholders = ", ".join("?" for _ in actions)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT id, timestamp, action, reason, details FROM decisions WHERE action IN ({placeholders}) ORDER BY id ASC",
            actions,
        ).fetchall()
    parsed: list[dict[str, Any]] = []
    for row in rows:
        try:
            details = json.loads(row[4]) if row[4] else {}
        except json.JSONDecodeError:
            details = {}
        parsed.append({"id": row[0], "timestamp": row[1], "action": row[2], "reason": row[3], "details": details})
    return parsed


def _all_decision_actions(db_path: Path) -> list[tuple[int, str]]:
    """Every decision as (id, action), in id order -- used to detect a manual
    state mutation anywhere inside a run's window, not just the two actions the
    run itself is built from."""
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        return [(int(row[0]), str(row[1])) for row in conn.execute("SELECT id, action FROM decisions ORDER BY id ASC").fetchall()]


def paper_proving_runs(root: Path) -> list[dict[str, Any]]:
    """Completed unattended paper runs, each paired with the reconcile that
    followed it and judged clean against what actually happened in its window.

    `equity_paper_loop_completed` is only ever logged by run_equity_paper_loop
    when the loop finished its own bound WITHOUT being halted, so its presence
    is the "ran unattended to completion" evidence. A run is clean only if ALL
    of the following hold, so that a run being clean is a real, falsifiable
    claim rather than a tautology:

      - a reconcile falls in this run's OWN window -- after this loop and
        before the NEXT loop_completed -- and each reconcile is consumed by at
        most one run, so a single reconcile can never vouch for two runs;
      - that reconcile reported no errors;
      - the loop actually iterated;
      - the run's activity window holds at least one paper_order_filled AND the
        summed fill notional moved the ledger non-trivially -- a zero-trade run
        proves nothing;
      - no manual state mutation (a *_counter_reset and the like) appears in
        the window, which would mean the run was not left unattended.

    A run's activity window is (previous loop_completed, this loop_completed]:
    the fills and the reset that this repo's runs produced land BEFORE the
    loop_completed that closes the run, not after it.
    """
    db_path = root / "data" / "trading_agent.db"
    rows = _decision_rows(db_path, ("equity_paper_loop_completed", "equity_paper_reconcile", "paper_order_filled"))
    all_actions = _all_decision_actions(db_path)
    loops = [row for row in rows if row["action"] == "equity_paper_loop_completed"]
    reconciles = [row for row in rows if row["action"] == "equity_paper_reconcile"]
    fills = [row for row in rows if row["action"] == "paper_order_filled"]

    consumed: set[int] = set()
    runs: list[dict[str, Any]] = []
    for index, loop in enumerate(loops):
        loop_id = loop["id"]
        lower = loops[index - 1]["id"] if index > 0 else 0
        next_loop_id = loops[index + 1]["id"] if index + 1 < len(loops) else None

        following = None
        for rec in reconciles:
            if rec["id"] in consumed:
                continue
            if rec["id"] > loop_id and (next_loop_id is None or rec["id"] < next_loop_id):
                following = rec
                consumed.add(rec["id"])
                break

        window_fills = [fill for fill in fills if lower < fill["id"] <= loop_id]
        ledger_delta = sum(abs(float(fill["details"].get("notional") or 0.0)) for fill in window_fills)
        mutated = any(lower < action_id <= loop_id and _MANUAL_MUTATION.search(action) for action_id, action in all_actions)

        iterations = int(loop["details"].get("iterations_completed") or 0)
        errors = (following or {}).get("details", {}).get("errors", None)
        adjusted = (following or {}).get("details", {}).get("adjusted", [])
        runs.append(
            {
                "completed_at": loop["timestamp"],
                "iterations_completed": iterations,
                "reconciled": following is not None,
                "reconcile_at": (following or {}).get("timestamp"),
                "reconcile_errors": errors,
                "reconcile_adjusted": adjusted,
                "fills": len(window_fills),
                "ledger_delta": ledger_delta,
                "manual_mutation_in_window": mutated,
                "clean": (
                    following is not None
                    and errors == []
                    and iterations > 0
                    and len(window_fills) >= 1
                    and ledger_delta > _LEDGER_DELTA_EPSILON
                    and not mutated
                ),
            }
        )
    return runs


def paper_runs_evidence(root: Path, rules: dict[str, Any]) -> Evidence:
    runs = paper_proving_runs(root)
    clean = [run for run in runs if run["clean"]]
    data = {"runs": runs, "clean_run_count": len(clean), "required": REQUIRED_CLEAN_PAPER_RUNS}
    if len(clean) < REQUIRED_CLEAN_PAPER_RUNS:
        return Evidence(
            False,
            f"{len(clean)} clean unattended paper run(s) in the audit log; "
            f"{REQUIRED_CLEAN_PAPER_RUNS} are required",
            data,
        )
    iterations = ", ".join(str(run["iterations_completed"]) for run in clean[-REQUIRED_CLEAN_PAPER_RUNS:])
    return Evidence(
        True,
        f"{len(clean)} unattended bounded paper run(s) completed and reconciled with no errors "
        f"(latest iteration counts: {iterations})",
        data,
    )


def crypto_lane_untouched_evidence(root: Path, rules: dict[str, Any]) -> Evidence:
    """This build must leave the crypto lane disarmed. STOP_TRADING is the
    crypto lane's stop file and must still exist; the equities lane must still
    use a DIFFERENT one, so re-arming crypto later cannot silently arm
    equities or vice versa."""
    crypto_stop = str(rules.get("kill_switch", {}).get("stop_file", "STOP_TRADING"))
    equities_stop = str(rules.get("equities", {}).get("kill_switch", {}).get("stop_file", ""))
    failures: list[str] = []
    if not (root / crypto_stop).exists():
        failures.append(f"{crypto_stop} is missing; the crypto lane is no longer disarmed")
    if not equities_stop:
        failures.append("the equities lane has no kill switch stop file configured")
    elif equities_stop == crypto_stop:
        failures.append(f"the equities lane shares the crypto stop file {crypto_stop!r}")
    if failures:
        return Evidence(False, "; ".join(failures), {"crypto_stop_file": crypto_stop, "equities_stop_file": equities_stop})
    return Evidence(
        True,
        f"crypto lane still disarmed ({crypto_stop} present); equities uses its own {equities_stop}",
        {"crypto_stop_file": crypto_stop, "equities_stop_file": equities_stop},
    )


def order_symbol_guard_evidence(root: Path, rules: dict[str, Any]) -> Evidence:
    """The guard's test file must be on disk. Its tests passing is checked
    separately; this catches the file being deleted outright, which would
    otherwise show up only as absent node ids."""
    path = root / "tests" / "test_order_symbol_guard.py"
    if not path.exists():
        return Evidence(False, "tests/test_order_symbol_guard.py is missing; the order-symbol guard is absent")
    return Evidence(True, "tests/test_order_symbol_guard.py present")


# ---------------------------------------------------------------------------
# the secret scan
# ---------------------------------------------------------------------------

# Directories whose contents count as "the code". data/, logs/ and .venv/ hold
# runtime state, not source, and .env is git-ignored by design.
_SCANNED_DIRS = ("src", "tests", "scripts", "config", "deploy")
_SCANNED_SUFFIXES = {".py", ".yaml", ".yml", ".json", ".sh", ".service", ".toml", ".cfg", ".example"}
_SKIP_PARTS = {"__pycache__", ".venv", "node_modules", ".git"}

_SECRET_NAME = re.compile(
    r"(?i)(api[_-]?key|secret|token|passwd|password|passphrase|private[_-]?key|credential|access[_-]?key)"
)
_PEM_BLOCK = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
# The name a private-key finding is reported under, and the token the
# allowlist pins one against.
_PEM_FINDING = "<pem block>"
_ASSIGNMENT = re.compile(r"^\s*(?P<name>[A-Za-z_][A-Za-z0-9_.\-]*)\s*[:=]\s*(?P<value>[^\s#]+)\s*(?:#.*)?$")

# A literal carrying any of these reads as a documented placeholder, not a
# credential. Lowercased before matching.
_PLACEHOLDER_MARKERS = (
    "example",
    "placeholder",
    "your-",
    "your_",
    "changeme",
    "change-me",
    "dummy",
    "redacted",
    "sample",
    "fake",
    "test",
    "stub",
    "xxxx",
    "<",
    "${",
    "{{",
    "todo",
    "not-a-real",
    "notreal",
)

# An env-var NAME (ROBINHOOD_API_KEY) is a reference, not a value.
_ENV_VAR_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Literals that read as credentials and provably are not. Each entry pins an
# exact FILE and an exact VALUE together, so this excuses one known string and
# never a location: a new credential-shaped literal in the same file is still
# a finding. Adding an entry is a deliberate, reviewable act.
ALLOWED_LITERALS: tuple[tuple[str, str, str], ...] = (
    (
        "tests/test_auth.py",
        "rh-api-6148effc-c0b1-486c-8940-a1d099456be6",
        "fixed test vector for the CRYPTO lane's request signing "
        "(test_robinhood_signature_matches_official_docs_example): the test asserts a known signature "
        "at a fixed timestamp, which makes the value a published example rather than a live credential",
    ),
    (
        "tests/test_auth.py",
        "xQnTJVeQLmw1/Mg2YimEViSpw/SdJcgNXZ5kQkAXNPU=",
        "the signing key half of the same fixed test vector; changing it would break the pinned signature "
        "assertion, which is what makes it a test constant rather than a secret",
    ),
    (
        "tests/test_equity_readiness.py",
        # Pinned by the finding's NAME rather than by the marker text, so that
        # writing this entry does not itself paste a key header into the tree
        # for the scanner to find. Same rule as every other entry: one file,
        # one excused thing.
        _PEM_FINDING,
        "the fixture this scanner's own private-key detector is tested against -- a bare header with no key "
        "material, written into a temporary tree by the test and excused only where the test itself lives",
    ),
)


def _is_allowed_literal(relative_path: str, value: str) -> bool:
    candidate = value.strip().strip("'\"")
    return any(path == relative_path and literal == candidate for path, literal, _ in ALLOWED_LITERALS)


def _looks_like_a_secret_value(value: str) -> bool:
    candidate = value.strip().strip("'\"")
    if len(candidate) < 12:
        return False
    lowered = candidate.lower()
    if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
        return False
    if _ENV_VAR_NAME.match(candidate):
        return False
    if candidate.startswith(("http://", "https://", "/", "./", "../")) or candidate.endswith((".py", ".yaml", ".json")):
        return False
    if " " in candidate:
        return False
    return True


@dataclass(frozen=True)
class SecretFinding:
    path: str
    line: int
    name: str
    why: str


def _scan_python(path: Path, relative: str, text: str) -> list[SecretFinding]:
    """Assignments and dict entries whose NAME reads as a credential and whose
    VALUE is a string literal that does not read as a placeholder."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [SecretFinding(relative, 0, "<unparsable>", "file could not be parsed for a secret scan")]
    findings: list[SecretFinding] = []

    def flag(name: str, node: ast.AST, value: str) -> None:
        if _is_allowed_literal(relative, value):
            return
        if _SECRET_NAME.search(name) and _looks_like_a_secret_value(value):
            findings.append(
                SecretFinding(relative, getattr(node, "lineno", 0), name, f"credential-shaped literal assigned to {name!r}")
            )

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
                if name and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    flag(name, node, node.value.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                flag(node.target.id, node, node.value.value)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    flag(key.value, value, value.value)
        elif isinstance(node, ast.keyword) and node.arg:
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                flag(node.arg, node.value, node.value.value)
    return findings


def _scan_text(relative: str, text: str) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = _ASSIGNMENT.match(line)
        if not match:
            continue
        name = match.group("name")
        if _is_allowed_literal(relative, match.group("value")):
            continue
        if _SECRET_NAME.search(name) and _looks_like_a_secret_value(match.group("value")):
            findings.append(SecretFinding(relative, number, name, f"credential-shaped literal assigned to {name!r}"))
    return findings


def scan_for_secrets(root: Path) -> list[SecretFinding]:
    """Credential-shaped literals in the tree's source and config.

    Two things are caught: a PEM private key block anywhere in a scanned file,
    and a credential-named assignment whose value is a literal that does not
    read as a documented placeholder or an env-var reference. `.env` is not
    scanned -- it is git-ignored and is where a local value is SUPPOSED to
    live; what must stay clean is everything that gets committed.
    """
    findings: list[SecretFinding] = []
    for directory in _SCANNED_DIRS:
        base = root / directory
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in _SCANNED_SUFFIXES:
                continue
            if _SKIP_PARTS & set(path.parts):
                continue
            relative = str(path.relative_to(root)).replace("\\", "/")
            text = path.read_text(encoding="utf-8", errors="replace")
            if _PEM_BLOCK.search(text) and not _is_allowed_literal(relative, _PEM_FINDING):
                line = next(
                    (n for n, value in enumerate(text.splitlines(), start=1) if _PEM_BLOCK.search(value)),
                    0,
                )
                findings.append(SecretFinding(relative, line, _PEM_FINDING, "a private key block is embedded here"))
            findings.extend(_scan_python(path, relative, text) if path.suffix == ".py" else _scan_text(relative, text))
    return findings


def no_secret_in_code_evidence(root: Path, rules: dict[str, Any]) -> Evidence:
    findings = scan_for_secrets(root)
    data = {
        "findings": [finding.__dict__ for finding in findings],
        "scanned_dirs": list(_SCANNED_DIRS),
        # Surfaced, never hidden: every excused literal is reported alongside
        # the verdict so a reviewer sees what the scan chose not to flag.
        "allowed_literals": [{"path": path, "reason": reason} for path, _, reason in ALLOWED_LITERALS],
    }
    if findings:
        listed = "; ".join(f"{f.path}:{f.line} {f.why}" for f in findings[:5])
        return Evidence(False, f"{len(findings)} credential-shaped literal(s) found: {listed}", data)
    excused = f", with {len(ALLOWED_LITERALS)} documented test vector(s) excused by name" if ALLOWED_LITERALS else ""
    return Evidence(
        True,
        f"no credential-shaped literal in {', '.join(name + '/' for name in _SCANNED_DIRS)}{excused} "
        "(equities auth is the OAuth connector, which leaves this lane no key to leak)",
        data,
    )


# ---------------------------------------------------------------------------
# git hygiene -- what a push would actually publish
# ---------------------------------------------------------------------------

# Runtime artifacts the lane writes. Each must be git-ignored, because each is
# reachable by a plain `git add -A`. The SQLite sidecars matter as much as the
# db itself: a run interrupted mid-write leaves a `-journal`/`-wal` beside the
# database holding the same audit rows, and `data/*.db` does not match them.
_MUST_BE_IGNORED = (
    ".env",
    ".env.local",
    "logs/live_audit_example.json",
    "data/trading_agent.db",
    "data/equity_paper_trades.db",
    "data/equity_market_data.db",
    "data/trading_agent.db-journal",
    "data/trading_agent.db-wal",
    "data/trading_agent.db-shm",
    "data/proving_runs/run.json",
)

# The counterweight: these are SUPPOSED to be committed. An over-broad ignore
# rule (`data/`, `*.env*`) that swallowed one of them would hide the documented
# placeholder file or the crypto lane's disarm marker, so the gate fails on a
# rule that is too wide just as it does on one that is too narrow.
_MUST_NOT_BE_IGNORED = (".env.example", "STOP_TRADING", "config/trading_rules.yaml")

# A tracked path matching one of these is a credential or a runtime artifact
# that has already been committed -- the thing that actually leaks on push.
_FORBIDDEN_TRACKED = re.compile(
    r"(^|/)\.env$|(^|/)\.env\.(?!example)|\.db$|\.sqlite3?$|\.db-(journal|wal|shm)$|\.pem$|(^|/)id_(rsa|ed25519)$",
    re.IGNORECASE,
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=False
    )


def tracked_files(root: Path) -> list[str]:
    result = _git(root, "ls-files")
    if result.returncode != 0:
        return []
    return [line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]


def git_hygiene_evidence(root: Path, rules: dict[str, Any]) -> Evidence:
    """Nothing that leaks may be tracked, and every runtime artifact is ignored.

    The secret scan above reads the SOURCE; this reads what git would actually
    PUBLISH. They catch different things: a database full of audit rows carries
    no credential-shaped literal, and `git check-ignore` says nothing about a
    file that was committed before the ignore rule existed.
    """
    if not (root / ".git").exists():
        return Evidence(False, "no .git directory; git hygiene cannot be verified from this tree")

    tracked = tracked_files(root)
    if not tracked:
        return Evidence(False, "`git ls-files` returned nothing; the hygiene check could not be trusted")

    # Already committed, and therefore already published on the next push.
    leaked = sorted(path for path in tracked if _FORBIDDEN_TRACKED.search(path))

    # Reachable by `git add -A` because no ignore rule covers them.
    unignored = [
        path
        for path in _MUST_BE_IGNORED
        if _git(root, "check-ignore", "-q", "--no-index", path).returncode != 0
    ]

    # Swallowed by an ignore rule that is too wide to be safe.
    over_ignored = [
        path
        for path in _MUST_NOT_BE_IGNORED
        if _git(root, "check-ignore", "-q", "--no-index", path).returncode == 0
    ]

    data = {
        "tracked_files": len(tracked),
        "leaked_tracked_paths": leaked,
        "unignored_runtime_artifacts": unignored,
        "over_ignored_committed_paths": over_ignored,
    }
    failures: list[str] = []
    if leaked:
        failures.append(f"credential/artifact paths are already tracked: {', '.join(leaked)}")
    if unignored:
        failures.append(f"runtime artifacts are not git-ignored: {', '.join(unignored)}")
    if over_ignored:
        failures.append(f"an ignore rule swallows paths that must stay committed: {', '.join(over_ignored)}")
    if failures:
        return Evidence(False, "; ".join(failures), data)
    return Evidence(
        True,
        f"{len(tracked)} tracked file(s), none of them a credential or a runtime artifact; "
        f"all {len(_MUST_BE_IGNORED)} runtime artifact path(s) git-ignored (databases and their SQLite "
        f"sidecars, logs, proving runs, .env) with .env.example and STOP_TRADING still committed",
        data,
    )


# ---------------------------------------------------------------------------
# the gate table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Gate:
    key: str
    name: str
    why: str
    proving_tests: tuple[str, ...] = ()
    evidence: Callable[[Path, dict[str, Any]], Evidence] | None = None


B = "tests/test_robinhood_equity_broker.py::"
C = "tests/test_robinhood_equity_client.py::"
K = "tests/test_equity_compliance.py::"
R = "tests/test_equity_runtime.py::"
M = "tests/test_market_hours.py::"
S = "tests/test_safety.py::"
G = "tests/test_order_symbol_guard.py::"
E = "tests/test_equity_readiness.py::"


GATES: tuple[Gate, ...] = (
    Gate(
        key=ORDER_SYMBOL_GUARD,
        name="Order-symbol guard",
        why="No equity execution module may hardcode a ticker beside an order call; the symbol comes from config, a signal or a caller.",
        proving_tests=(
            G + "test_repo_src_has_no_hardcoded_order_symbols",
            G + "test_config_listed_equity_symbols_extend_the_vocabulary",
            G + "test_case_insensitive_detection_covers_both_cases",
            G + "test_soundness_check_rejects_an_empty_scan_when_order_tools_exist",
            G + "test_soundness_check_rejects_a_scan_that_missed_an_order_tool_module",
            G + "test_scan_is_pinned_non_empty_on_a_tree_that_references_order_tools",
        ),
        evidence=order_symbol_guard_evidence,
    ),
    Gate(
        key="kill_switch",
        name="Kill switch",
        why="STOP_TRADING_EQUITIES or TRADING_ENABLED=false halts the lane, and the switch is re-read at the irreversible moment, not just at startup.",
        proving_tests=(
            B + "test_stop_trading_file_blocks_execution",
            B + "test_trading_enabled_false_blocks_execution",
            B + "test_the_kill_switch_is_re_read_at_the_moment_of_submission",
            R + "test_equities_kill_switch_is_its_own_file_not_the_crypto_stop_file",
            R + "test_equities_stop_file_halts_the_loop_immediately",
        ),
    ),
    Gate(
        key="human_gate",
        name="Human gate on every real order",
        why="A real order needs both flags together -- dry_run=False and confirm_live_order=True. Either flag alone, a truthy non-boolean, or neither returns an unsubmitted preview.",
        proving_tests=(
            C + "test_place_order_defaults_to_dry_run_and_returns_a_payload",
            C + "test_every_combination_but_both_flags_stays_unsubmitted",
            C + "test_place_order_submits_only_when_dry_run_false_and_confirm_true",
            C + "test_inverted_confirm_caller_still_cannot_submit",
            C + "test_cancel_order_defaults_to_dry_run_and_does_not_submit",
            B + "test_a_broker_built_with_no_flags_cannot_submit",
            B + "test_every_flag_combination_but_both_stays_unsubmitted",
            B + "test_a_truthy_non_boolean_confirmation_does_not_arm_the_lane",
            B + "test_an_inverted_confirm_caller_still_cannot_submit",
            B + "test_absent_confirmation_does_not_submit_through_the_whole_lane",
            B + "test_an_armed_broker_still_previews_in_live_dry_run_mode",
        ),
    ),
    Gate(
        key="agentic_account_only",
        name="Agentic-account-only",
        why='Every order targets the single Robinhood-designated agent-tradable account (nickname "Agentic", ..2092); the default account is a hard failure, refused ahead of the risk layer.',
        proving_tests=(
            C + "test_resolves_and_pins_the_agent_tradable_account",
            C + "test_raises_when_no_agent_tradable_account_exists",
            C + "test_raises_when_more_than_one_agent_tradable_account_exists",
            C + "test_agentic_allowed_field_is_read_not_the_legacy_agent_tradable_field",
            C + "test_agentic_flag_on_wrong_number_is_rejected_not_pinned",
            C + "test_agentic_flag_on_wrong_nickname_is_rejected_not_pinned",
            C + "test_place_order_refuses_the_default_account",
            C + "test_place_order_targets_the_pinned_account_by_default",
            C + "test_cancel_order_refuses_the_default_account",
            C + "test_review_order_refuses_a_different_account",
            B + "test_a_non_agentic_account_is_rejected_at_the_broker",
            B + "test_a_non_agentic_account_is_rejected_before_the_risk_layer",
            B + "test_payload_targets_the_pinned_agent_account",
            B + "test_reads_resolve_against_the_pinned_account",
        ),
        evidence=agentic_account_evidence,
    ),
    Gate(
        key="regular_trading_hours",
        name="Regular trading hours",
        why="A real order is refused outside 09:30-16:00 eastern; weekends and market holidays are refused even with the extended-hours opt-in on, which itself defaults off.",
        proving_tests=(
            B + "test_a_broker_built_with_no_flags_defaults_extended_hours_off",
            B + "test_an_order_outside_regular_hours_is_refused_by_default",
            B + "test_extended_hours_opt_out_defaults_off_so_the_same_clock_still_blocks",
            B + "test_a_market_holiday_is_refused_even_with_extended_hours_opted_in",
            B + "test_market_hours_guard_refuses_before_the_kill_switch_check",
            B + "test_the_shared_lane_blocks_a_live_signal_outside_regular_hours",
            M + "test_a_weekend_is_blocked_regardless_of_time_of_day",
            M + "test_extended_hours_is_blocked_unless_explicitly_opted_in",
            M + "test_a_market_holiday_blocks_orders_even_during_normal_market_hours",
        ),
    ),
    Gate(
        key="pattern_day_trader",
        name="Pattern-day-trader",
        why="A 4th same-symbol day trade inside 5 business days is blocked while account equity is under FINRA's $25k threshold, and the guard is re-checked through the shared lane.",
        proving_tests=(
            B + "test_pdt_guard_blocks_the_fourth_day_trade_under_25k",
            B + "test_pdt_guard_allows_a_third_day_trade_under_25k",
            B + "test_pdt_guard_does_not_block_once_account_equity_clears_25k",
            B + "test_pdt_guard_is_re_checked_through_the_shared_lane",
            K + "test_the_fourth_day_trade_in_the_window_is_blocked_under_25k",
            K + "test_day_trades_outside_the_five_business_day_window_do_not_count",
            K + "test_day_trades_on_a_different_symbol_do_not_count_toward_this_ones_limit",
        ),
    ),
    Gate(
        key="long_only_no_shorts",
        name="Long-only (no shorts)",
        why="A sell beyond the held quantity would open a short and is refused outright, independent of trading_rules.yaml's allow_shorting flag.",
        proving_tests=(
            B + "test_a_sell_beyond_the_held_quantity_is_refused_as_a_short",
            B + "test_a_sell_with_no_position_at_all_is_refused_as_a_short",
            K + "test_selling_more_than_held_is_a_short_and_is_refused",
            K + "test_a_sell_with_no_position_at_all_is_refused",
            S + "test_live_mode_blocks_short_sell",
        ),
    ),
    Gate(
        key="no_margin",
        name="No margin (cash account, settled funds only)",
        why="The Agentic account is cash-only: a buy must fit inside SETTLED cash, buying_power is never read as available capital, and unsettled sale proceeds cannot be spent.",
        proving_tests=(
            B + "test_a_buy_beyond_settled_cash_is_refused_as_margin",
            B + "test_a_buy_that_would_spend_same_day_unsettled_sale_proceeds_is_refused",
            B + "test_portfolio_uses_settled_cash_not_buying_power",
            K + "test_a_buy_that_would_spend_unsettled_sale_proceeds_is_blocked",
            K + "test_proceeds_settle_after_one_business_day",
        ),
    ),
    Gate(
        key="no_options",
        name="No options",
        why="Only a plain equity ticker shape reaches the order path; a 21-character option contract symbol is refused before anything else happens.",
        proving_tests=(B + "test_option_symbols_are_refused_before_anything_else",),
    ),
    Gate(
        key="risk_caps",
        name="Risk caps",
        why="The shared RiskManager enforces the allowlist, per-trade and daily caps, open-position count, allocation percentage and order cooldown, and the configured caps are within bounds.",
        proving_tests=(
            B + "test_the_shared_risk_layer_blocks_a_symbol_that_is_not_allowlisted",
            B + "test_the_shared_risk_layer_blocks_a_used_up_daily_trade_count",
            S + "test_risk_manager_blocks_symbol_allocation_limit",
            S + "test_risk_manager_blocks_order_cooldown",
            S + "test_risk_manager_default_rules_block_trades",
        ),
        evidence=risk_caps_evidence,
    ),
    Gate(
        key="paper_proven_twice",
        name="Paper ran unattended twice, reconciled clean",
        why=f"The audit log records at least {REQUIRED_CLEAN_PAPER_RUNS} bounded paper loops that finished on their own bound without being halted, each followed by a reconcile reporting no errors.",
        proving_tests=(
            R + "test_bounded_paper_loop_completes_unattended_and_reconciles_clean",
            R + "test_loop_requires_an_explicit_bound",
        ),
        evidence=paper_runs_evidence,
    ),
    Gate(
        key="no_secret_in_code",
        name="No secret in code",
        why="Equities auth is the OAuth connector, which leaves no key to hold; no credential-shaped literal may appear in src/, tests/, scripts/, config/ or deploy/.",
        evidence=no_secret_in_code_evidence,
    ),
    Gate(
        key="git_hygiene",
        name="Nothing leaks on push",
        why="No .env, database, SQLite sidecar or key file is tracked, and every runtime artifact the lane writes is git-ignored -- the secret scan reads the source, this reads what a push would actually publish.",
        proving_tests=(
            E + "test_git_hygiene_passes_on_this_repo",
            E + "test_git_hygiene_fails_when_a_dotenv_is_tracked",
            E + "test_git_hygiene_fails_when_a_database_is_tracked",
            E + "test_git_hygiene_fails_when_a_runtime_artifact_is_not_ignored",
            E + "test_git_hygiene_fails_when_an_ignore_rule_is_too_wide",
            E + "test_sqlite_sidecars_are_ignored_not_just_the_database",
        ),
        evidence=git_hygiene_evidence,
    ),
    Gate(
        key="crypto_lane_untouched",
        name="Crypto lane still disarmed",
        why="This build must not change the crypto lane's posture: STOP_TRADING still exists and the equities lane uses its own separate stop file.",
        proving_tests=(R + "test_equities_kill_switch_is_its_own_file_not_the_crypto_stop_file",),
        evidence=crypto_lane_untouched_evidence,
    ),
)


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------


def evaluate_gate(gate: Gate, suite: SuiteResult, root: Path, rules: dict[str, Any]) -> dict[str, Any]:
    """One gate's row: every proving test with its outcome, the evidence, and
    the pass/fail that follows from both."""
    tests: list[dict[str, Any]] = []
    reasons: list[str] = []
    for nodeid in gate.proving_tests:
        outcome = suite.outcomes.get(nodeid)
        present = outcome is not None
        passed = outcome == _PROVEN
        tests.append({"nodeid": nodeid, "present": present, "outcome": outcome or "ABSENT", "passed": passed})
        if not present:
            reasons.append(f"proving test is ABSENT: {nodeid}")
        elif not passed:
            reasons.append(f"proving test {outcome}: {nodeid}")

    evidence_row: dict[str, Any] | None = None
    if gate.evidence is not None:
        evidence = gate.evidence(root, rules)
        evidence_row = {"passed": evidence.passed, "detail": evidence.detail, **({"data": evidence.data} if evidence.data else {})}
        if not evidence.passed:
            reasons.append(f"evidence: {evidence.detail}")

    return {
        "key": gate.key,
        "name": gate.name,
        "why": gate.why,
        "passed": not reasons,
        "proving_tests": tests,
        "proving_tests_passed": sum(1 for row in tests if row["passed"]),
        "proving_tests_total": len(tests),
        "evidence": evidence_row,
        "reasons": reasons,
    }


def readiness_verdict(suite: SuiteResult, gates: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """The overall verdict -- a pure function of the suite result and the gate
    rows, so it can be tested without running anything.

    ready:true requires a green non-vacuous suite AND every gate passing. The
    two non-negotiable conditions are then re-asserted by name, after the
    generic loop: a red suite and an unproven order-symbol guard each force
    ready:false on their own, whatever the gate table says or how it is later
    edited.
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
    guard = next((gate for gate in gates if gate["key"] == ORDER_SYMBOL_GUARD), None)
    if guard is None:
        blocking.append("the order-symbol guard gate is absent from the readiness report")
    elif not guard["passed"] and not any(ORDER_SYMBOL_GUARD in reason or guard["name"] in reason for reason in blocking):
        blocking.append("the order-symbol guard is not proven")

    return (not blocking), blocking


def equity_live_readiness(
    root: Path,
    suite_runner: Callable[[Path], SuiteResult] = run_project_test_suite,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Produce the equities lane's live-readiness report.

    Read-only: runs the test suite, reads config, reads the audit log, scans
    the source. Never touches the connector and never changes a posture.
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
            "execution_surface": "authorized Robinhood OAuth connector toolset (agent-hosted; no equities web service, no key to mint)",
            "auth": "the OAuth connector is itself the credential -- no vault entry, no .env key",
            "paper_broker": (
                "local paper_broker simulating fills against a deterministic SYNTHETIC quote feed; "
                "the recorded paper proving runs did not read live market quotes"
            ),
            "quote_source": "synthetic",
            "extended_hours_opt_in": bool(rules.get("equities", {}).get("allow_extended_hours", False)),
            "equities_universe": list(rules.get("equities", {}).get("universe", []) or []),
            "note": "switching paper->live is the operator's explicit call and is never a side effect of this report",
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
        summary = f"equities live-readiness: ready={ready}, {report['gates_passed']}/{report['gates_total']} gates proven"
        logger.log_decision(
            None,
            "equity_live_readiness",
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
    """The same report as a human-readable page -- the project's stated purpose
    is a readable rationale, so the verdict has to be readable too."""
    verdict = "READY: true" if report["ready"] else "READY: false"
    suite = report["test_suite"]
    lines = [
        "# Equities lane -- live-readiness report",
        "",
        f"**{verdict}**  -- {report['gates_passed']}/{report['gates_total']} gates proven, "
        f"{suite['tests_run']} distinct tests recorded (parametrized cases rolled up), "
        f"{len(suite['failed'])} failed.",
        "",
        f"Generated {report['generated_at']} for lane `{report['lane']}`.",
        "",
        "This report is read-only. Switching the lane from paper to live is the operator's",
        "explicit call and is never a side effect of generating it.",
        "",
        "## Verdict",
        "",
    ]
    if report["ready"]:
        lines += ["Every gate below is proven by tests that exist and pass, the full suite is green,", "and every non-test evidence check holds.", ""]
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
        f"- Paper: {posture['paper_broker']}",
        f"- Extended-hours opt-in: {posture['extended_hours_opt_in']}",
        f"- Universe: {', '.join(posture['equities_universe']) or '(empty)'}",
        "",
    ]
    return "\n".join(lines)
