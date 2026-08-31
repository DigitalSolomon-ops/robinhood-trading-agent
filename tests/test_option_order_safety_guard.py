"""The build fails if the OPTIONS lane can place an ungated live order, or can
open a naked-short / undefined-risk option position.

This is the options lane's `first_task`, the counterpart of
tests/test_order_symbol_guard.py: the safety property goes in BEFORE the lane
exists, so it is never "added at the end" to code that already shipped without
it. It is written to still be correct once the lane does exist -- discovery is
by CONTENT, so the guard picks up an options execution module the moment one
lands, with nobody remembering to update this file.

TWO PROPERTIES
    A. NO UNGATED SUBMIT. Every call that reaches the connector's
       place_option_order must be dominated, inside its own function, by a gate
       that establishes BOTH facts: an explicit human CONFIRM flag and the
       OPTIONS lane being ARMED. Either alone is a violation, and so is a
       submit with no gate at all.
    B. NO NAKED SHORT / UNDEFINED RISK. An option order payload that opens a
       SELL leg with no covering BUY-to-open leg is refused, as is a credit
       opening order with no long leg, as is any function/call/payload value
       naming a forbidden strategy (naked, uncovered, sell-to-open, short
       call/put/straddle/strangle, covered call, ratio spread).

WHY A POLARITY ANALYSIS HERE, WHEN THE EQUITIES GUARD DECLINED ONE
    test_order_symbol_guard.py says, correctly, that a naive "an order call
    must be near a confirm flag" check is not sound -- `if not confirm:
    submit(...)` passes it. This guard does not do the naive version. It
    abstractly interprets the boolean expression of every enclosing branch and
    early-return guard, tracking POLARITY, so `if not confirm: submit(...)` is
    reported and `if not (confirm and armed): return preview` clears both
    facts. `and` / `or` / `not` / `is True` / `== False` / `assert` are all
    handled, with the undecidable direction resolved to "establishes nothing"
    (an `or` taken TRUE proves neither disjunct), which fails closed.

    The analysis is per-FUNCTION on purpose. A gate in some distant caller does
    not count: the confirm+arm check must sit with the irreversible call, the
    same way the equities broker re-checks its universe, its clock and its kill
    switch immediately before the connector call rather than trusting the layer
    above.

WHY CANCEL AND REVIEW ARE NOT HELD TO "ARMED"
    Only place_option_order opens or closes risk. review_option_order is
    Robinhood's non-committal preview (the equities client calls it
    unconditionally for exactly this reason), and cancel_option_order REDUCES
    exposure -- requiring the lane to be ARMED before it could cancel would mean
    disarming locked in every open order, which is the opposite of a kill
    switch. Both still pass through the client's own dry_run/confirm flags at
    runtime; this static gate is about the submit path.

HOW A MODULE GETS INTO SCOPE (by CONTENT, not by filename)
    1. Direct reference to an option order tool (place/review/cancel_option_order)
       anywhere in the raw source -- call, constant, dispatch-table value, even
       a comment (conservative on purpose).
    2. Constant propagation: a name bound to an order-tool string is tainted,
       and any module referencing that name is in scope too, transitively.
       Constant `+` concatenations are folded, so a tool name split across two
       literals still taints.
    3. Filename hint: an option / contract / leg path token, UNION-ed with the
       above. That is what puts the analysis-only src/options_scout in scope --
       a "sell a naked put" play must not be able to appear even there.
    4. A module routing through a generic `_call` / `_invoke` / `dispatch`
       wrapper, which is executing SOMETHING against the connector.

WHAT IS DELIBERATELY OUT OF SCOPE
    A leg whose side or position_effect is computed at runtime cannot be
    classified statically, and is not guessed at (the `direction == "credit"`
    rule below catches the common dynamic case). The runtime half of the same
    property belongs to the options broker: it must refuse a sell-to-open leg
    with no long leg at the irreversible moment, exactly as the equities broker
    re-checks assert_in_universe. This guard stops such a path being written
    into src/ at all.

Structure mirrors tests/test_order_symbol_guard.py: plain helpers, fixtures
built in tmp_path, and one assertion pinned against the real tree.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

# The tree is scanned across BOTH source roots. An options execution module can
# just as easily land under scripts/ as under src/ -- a standalone runner, a
# one-off submit helper -- and a guard that only ever looked at src/ would wave
# it straight through. The subdir list is the single knob: drop "scripts" and
# the scripts-lane fixture below goes red.
SCAN_SUBDIRS = ("src", "scripts")

# The connector's option ORDER toolset. place is the only one that can create
# or close a position; review is a non-committal preview and cancel reduces
# exposure (see the module docstring).
SUBMIT_TOOLS = ("place_option_order",)
ORDER_TOOLS = ("place_option_order", "review_option_order", "cancel_option_order")

# The authorized read-only option tools. Listed so a resolved dispatch through a
# generic wrapper -- `connector._call("get_option_chains", ...)` -- is recognised
# as NOT a submit, while an UNRESOLVED dispatch still is.
READ_TOOLS = (
    "get_option_chains",
    "get_option_quotes",
    "get_option_market_data",
    "get_option_positions",
    "get_option_orders",
    "get_option_level_upgrade_info",
)

ALL_TOOLS = ORDER_TOOLS + READ_TOOLS

_SUBMIT_TOOL_NAMES = frozenset(tool.lower() for tool in SUBMIT_TOOLS)
_ORDER_TOOL_NAMES = frozenset(tool.lower() for tool in ORDER_TOOLS)
_ALL_TOOL_NAMES = frozenset(tool.lower() for tool in ALL_TOOLS)

# Path tokens that mark a module as options work regardless of its contents.
# Whole tokens only: "options_scout" hits on "options", while the equities and
# crypto lanes ("robinhood_equity_broker", "robinhood_crypto_client") do not.
FILENAME_HINTS = re.compile(r"^(option|options|contract|contracts|leg|legs|greeks)$", re.IGNORECASE)

# A thin dispatch wrapper: a module routing a call through one of these is
# executing SOMETHING against the connector.
_WRAPPER_METHODS = frozenset({"_call", "_invoke", "dispatch"})

# --- the two gate facts -------------------------------------------------------

CONFIRM = "confirm"
ARM = "armed"
REQUIRED_FACTS = frozenset({CONFIRM, ARM})

# Names that ASSERT the human-gate fact when true. Anchored so a name is matched
# on a token boundary: `self.confirm_live_order` and `human_gate` match,
# `unconfirmed` does not.
_CONFIRM_TOKENS = re.compile(r"(?:^|[^a-z])(confirm\w*|human_gate|gate_cleared|will_submit)", re.IGNORECASE)

# Names that assert the OPTIONS lane is armed. `arm`, `armed`, `is_armed`,
# `arm_store` match; `alarm`, `disarmed`, `farm` do not.
_ARM_TOKENS = re.compile(r"(?:^|[^a-z])(arm|armed|arming|rearm)(?:$|[^a-z])", re.IGNORECASE)

# A statement-level guard helper: `self._assert_lane_armed()`,
# `require_confirmation()`. Only a call NAMED like an assertion contributes a
# fact, so an ordinary call that happens to mention a gate word does not.
_GUARD_CALL = re.compile(r"^_*(assert|require|ensure|enforce|verify)_", re.IGNORECASE)

# --- forbidden strategies -----------------------------------------------------

# Undefined-risk / short-premium strategy vocabulary. A covered call is included
# deliberately: this lane's rule is "no selling to open without the long leg",
# and a covered call's cover is stock, not an option leg.
_NAKED_NAME = re.compile(
    r"(naked|uncovered|sell_?to_?open|short_?(call|put|straddle|strangle)|"
    r"write_?(call|put)|covered_?call|ratio_?spread|undefined_?risk|cash_?secured_?put)",
    re.IGNORECASE,
)

# A name that FORBIDS the thing it is named after is not a violation:
# `assert_no_naked_short`, `_reject_uncovered_leg`, `is_naked_short`.
_FORBIDDING_NAME = re.compile(
    r"^_*(assert|require|ensure|enforce|verify|check|validate|guard|is|has|no|not|"
    r"refuse|reject|forbid|block|deny|prevent|without|test)_",
    re.IGNORECASE,
)

# Dict keys / kwargs whose VALUE names a strategy or a leg role. A forbidden
# token is only read out of one of these positions, so a refusal message --
# `raise RuntimeError("refusing a naked short")` -- stays clean.
_STRATEGY_KEYS = frozenset(
    {"strategy", "play", "play_type", "trade_type", "type", "order_type", "side", "position_effect", "leg_type"}
)

# Leg fields, in the shape Robinhood's option order payload takes:
# {"side": "buy", "position_effect": "open", "ratio_quantity": 1, "option": url}
_SIDE_KEY = "side"
_EFFECT_KEY = "position_effect"
_DIRECTION_KEY = "direction"


# --- findings -----------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    module: str
    line: int
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.module}:{self.line} [{self.kind}] {self.detail}"


@dataclass
class GuardReport:
    """What the scan looked at, and what it found."""

    scanned: set[str] = field(default_factory=set)
    order_tool_modules: set[str] = field(default_factory=set)
    submit_sites: int = 0
    ungated: list[Violation] = field(default_factory=list)
    naked: list[Violation] = field(default_factory=list)
    unparsable: list[str] = field(default_factory=list)

    @property
    def violations(self) -> list[Violation]:
        return [*self.ungated, *self.naked]

    def kinds(self) -> set[str]:
        return {violation.kind for violation in self.violations}


class VacuousScan(AssertionError):
    """Raised when the scan proved nothing because it looked at nothing."""


def assert_scan_is_sound(report: GuardReport) -> None:
    """A clean report is only meaningful if the scan was not empty.

    If any module references an option order tool, the scanned set must be
    non-empty and must include that module -- otherwise a discovery bug reads
    as a pass.
    """
    if report.unparsable:
        raise AssertionError("unparsable modules cannot be cleared: " + ", ".join(sorted(report.unparsable)))
    if report.order_tool_modules and not report.scanned:
        raise VacuousScan(
            "option order-tool references exist but nothing was scanned: "
            + ", ".join(sorted(report.order_tool_modules))
        )
    missed = report.order_tool_modules - report.scanned
    if missed:
        raise VacuousScan("option order-tool modules missing from the scan: " + ", ".join(sorted(missed)))


# --- small AST helpers --------------------------------------------------------


def python_modules(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _fold_concat(node: ast.AST) -> str | None:
    """The folded value of a constant string concatenation, or None.

    `"place_option" + "_order"` is a tool name split so that no
    `\\bplace_option_order\\b` appears in the raw source; folding recovers it.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _fold_concat(node.left)
        right = _fold_concat(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _string_constants(node: ast.AST) -> list[str]:
    """Every string literal in a subtree, plus folded `+` concatenations."""
    values = [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]
    for child in ast.walk(node):
        if isinstance(child, ast.BinOp) and isinstance(child.op, ast.Add):
            folded = _fold_concat(child)
            if folded is not None:
                values.append(folded)
    return values


def _referenced_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
        elif isinstance(child, ast.alias):
            names.add(child.name.split(".")[-1])
            if child.asname:
                names.add(child.asname)
    return names


def _assignment_targets(node: ast.stmt) -> list[str]:
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    names: list[str] = []
    for target in targets:
        for child in ast.walk(target):
            if isinstance(child, ast.Name):
                names.append(child.id)
    return names


def _expr_text(node: ast.AST) -> str:
    """A dotted, readable spelling of a name-ish expression.

    `self.arm_store.is_armed(lane)` -> "self.arm_store.is_armed";
    `flags["confirm_live_order"]` -> "flags.confirm_live_order".
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _expr_text(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _expr_text(node.func)
    if isinstance(node, ast.Subscript):
        base = _expr_text(node.value)
        key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            return f"{base}.{key.value}" if base else key.value
        return base
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _constant_str(node: ast.AST | None, consts: dict[str, str] | None = None) -> str | None:
    """The lower-cased string value of a node, folding constants.

    A bare Name is resolved through `consts` -- the module/class-level string
    bindings -- so a leg written `{"side": SIDE_SELL}` with `SIDE_SELL = "sell"`
    classifies exactly as the literal `{"side": "sell"}` would. This is the same
    constant fold the discovery half already does for tool names; property B was
    blind to it, letting a naked short hide its `side` behind a module constant.
    """
    if node is None:
        return None
    if isinstance(node, ast.Name) and consts and node.id in consts:
        return consts[node.id].strip().lower()
    folded = _fold_concat(node)
    return folded.strip().lower() if isinstance(folded, str) else None


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level and class-level names bound to a constant string.

    Only literal / folded-`+` string values are captured, so a name is resolved
    to a side or effect token only when its value is statically knowable.
    """
    consts: dict[str, str] = {}
    for node in _module_assignments(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if node.value is None:
            continue
        folded = _fold_concat(node.value)
        if isinstance(folded, str):
            for target in _assignment_targets(node):
                consts[target] = folded
    return consts


# --- discovery ----------------------------------------------------------------


def references_order_tool(source: str) -> bool:
    return any(re.search(rf"\b{tool}\b", source, re.IGNORECASE) for tool in ORDER_TOOLS)


def has_filename_hint(relative_path: str) -> bool:
    tokens = re.split(r"[/\\._\-]+", relative_path)
    return any(FILENAME_HINTS.match(token) for token in tokens if token)


def invokes_generic_wrapper(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _WRAPPER_METHODS:
                return True
    return False


def _tool_literals(node: ast.AST) -> set[str]:
    """Option tool names carried by the string literals of a subtree."""
    found: set[str] = set()
    for literal in _string_constants(node):
        text = literal.strip().lower()
        if text in _ALL_TOOL_NAMES:
            found.add(text)
            continue
        for tool in _ALL_TOOL_NAMES:
            if re.search(rf"\b{tool}\b", text):
                found.add(tool)
    return found


def _module_assignments(tree: ast.Module) -> list[ast.stmt]:
    """Module-level and class-level assignments -- a dispatch table is as often
    a class attribute as a module constant."""
    statements: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            statements.extend(node.body)
        else:
            statements.append(node)
    return statements


def _tainted_assignments(tree: ast.Module, tainted: dict[str, frozenset[str]]) -> dict[str, frozenset[str]]:
    """Names bound to something carrying an option order tool, mapped to the
    tools they carry -- so a name tainted with `cancel_option_order` is in scope
    without being mistaken for a submit."""
    found: dict[str, frozenset[str]] = {}
    for node in _module_assignments(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if value is None:
            continue
        tools = set(_tool_literals(value))
        for name in _referenced_names(value):
            tools |= tainted.get(name, frozenset())
        if tools:
            for target in _assignment_targets(node):
                found[target] = frozenset(tools | set(found.get(target, frozenset())))
    return found


def _collect_sources(root: Path, base: Path | None = None) -> dict[str, str]:
    """Every Python module under `root`, keyed by its path relative to `base`.

    `base` defaults to `root` (bare filenames, as the tmp_path fixtures expect);
    the real scan passes REPO_ROOT so `src/...` and `scripts/...` stay distinct
    when both trees are merged into one scan.
    """
    base = base or root
    sources: dict[str, str] = {}
    for path in python_modules(root):
        sources[path.relative_to(base).as_posix()] = path.read_text(encoding="utf-8")
    return sources


def discover_execution_modules(
    sources: dict[str, str],
) -> tuple[dict[str, ast.Module], set[str], set[str], dict[str, frozenset[str]]]:
    """Return (parsed modules, in-scope paths, direct order-tool modules, taint map)."""
    parsed: dict[str, ast.Module] = {}

    for relative, source in sources.items():
        try:
            parsed[relative] = ast.parse(source, filename=relative)
        except SyntaxError:
            continue

    direct = {relative for relative, source in sources.items() if references_order_tool(source)}

    tainted: dict[str, frozenset[str]] = {}
    while True:
        grown = dict(tainted)
        for tree in parsed.values():
            for name, tools in _tainted_assignments(tree, grown).items():
                grown[name] = frozenset(tools | set(grown.get(name, frozenset())))
        if grown == tainted:
            break
        tainted = grown

    in_scope = set(direct)
    if tainted:
        for relative, tree in parsed.items():
            if _referenced_names(tree) & set(tainted):
                in_scope.add(relative)
    in_scope |= {relative for relative in sources if has_filename_hint(relative)}
    in_scope |= {relative for relative, tree in parsed.items() if invokes_generic_wrapper(tree)}
    in_scope &= set(parsed)

    return parsed, in_scope, direct, tainted


# --- property A: no submit without confirm AND armed --------------------------


def _callee_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return ""


def _call_arguments(call: ast.Call) -> list[ast.expr]:
    return [*call.args, *(keyword.value for keyword in call.keywords)]


def is_submit_call(call: ast.Call, tainted: dict[str, frozenset[str]]) -> bool:
    """True if this call can reach the connector's place_option_order.

    Four routes: the callee IS the tool; an argument literal (or folded `+`
    concatenation) names it; an argument references a constant tainted with it;
    or the call routes through a generic dispatch wrapper whose tool argument
    could NOT be resolved to a known tool -- an unresolved dispatch out of an
    options execution module is treated as a possible submit, which fails closed.
    """
    name = _callee_name(call).lower()
    if name in _SUBMIT_TOOL_NAMES:
        return True
    if name in _ORDER_TOOL_NAMES or name in _ALL_TOOL_NAMES:
        return False  # review / cancel / a read: named explicitly, not a submit

    literals: set[str] = set()
    named: set[str] = set()
    for argument in _call_arguments(call):
        literals |= _tool_literals(argument)
        for reference in _referenced_names(argument):
            named |= tainted.get(reference, frozenset())
    if (literals | named) & _SUBMIT_TOOL_NAMES:
        return True
    if name in _WRAPPER_METHODS and not (literals | named):
        return True  # unresolved dynamic dispatch
    return False


def _fact_names(node: ast.AST) -> set[str]:
    """The gate facts a name-ish expression asserts when it is TRUE."""
    text = _expr_text(node)
    if not text:
        return set()
    facts: set[str] = set()
    if _CONFIRM_TOKENS.search(text):
        facts.add(CONFIRM)
    if _ARM_TOKENS.search(text):
        facts.add(ARM)
    return facts


def _compare_facts(node: ast.Compare, want_true: bool) -> set[str]:
    """Facts from `flag is True` / `flag == False` / `flag is not False`."""
    if len(node.ops) != 1 or len(node.comparators) != 1:
        return set()
    operator, comparator = node.ops[0], node.comparators[0]
    if not isinstance(comparator, ast.Constant) or not isinstance(comparator.value, bool):
        return set()
    if isinstance(operator, (ast.Is, ast.Eq)):
        asserts_target_true = comparator.value is True
    elif isinstance(operator, (ast.IsNot, ast.NotEq)):
        asserts_target_true = comparator.value is False
    else:
        return set()
    target_is_true = asserts_target_true if want_true else not asserts_target_true
    return _fact_names(node.left) if target_is_true else set()


def gate_facts(node: ast.AST, want_true: bool = True) -> set[str]:
    """The gate facts established by KNOWING this expression is true / false.

    The undecidable directions resolve to the empty set, which fails closed:
    an `and` known FALSE says nothing (either operand could be the false one),
    and an `or` known TRUE says nothing (either operand could be the true one).
    """
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return gate_facts(node.operand, not want_true)
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            if not want_true:
                return set()
            return set().union(*(gate_facts(value, True) for value in node.values))
        if want_true:
            return set()
        return set().union(*(gate_facts(value, False) for value in node.values))
    if isinstance(node, ast.Compare):
        return _compare_facts(node, want_true)
    if not want_true:
        return set()
    return _fact_names(node)


def guard_call_facts(call: ast.Call) -> set[str]:
    """Facts from a statement-level guard helper: `self._assert_lane_armed()`."""
    name = _callee_name(call)
    if not _GUARD_CALL.match(name):
        return set()
    text = name + " " + " ".join(literal for literal in _string_constants(call))
    facts: set[str] = set()
    if _CONFIRM_TOKENS.search(text):
        facts.add(CONFIRM)
    if _ARM_TOKENS.search(text):
        facts.add(ARM)
    return facts


def _always_exits(body: list[ast.stmt]) -> bool:
    """True if control can never fall off the end of this block."""
    if not body:
        return False
    last = body[-1]
    if isinstance(last, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
        return True
    if isinstance(last, ast.If):
        return _always_exits(last.body) and _always_exits(last.orelse)
    if isinstance(last, (ast.With, ast.AsyncWith)):
        return _always_exits(last.body)
    return False


class _GateScanner:
    """Walks a module's statements tracking which gate facts hold at each point."""

    def __init__(self, module: str, tainted: dict[str, frozenset[str]]) -> None:
        self.module = module
        self.tainted = tainted
        self.violations: list[Violation] = []
        self.submit_sites = 0

    def scan(self, tree: ast.Module) -> None:
        self._block(tree.body, frozenset())

    # -- expression level

    def _expression(self, node: ast.AST, established: frozenset[str]) -> None:
        """Walk an expression tracking the facts that hold at each sub-node.

        Plain descent carries `established` unchanged, but three expression
        forms establish facts for the sub-tree they short-circuit into, exactly
        as an enclosing `if` would:

          * a ternary `submit() if gate else preview` -- the `body` sees the
            test true, the `orelse` sees it false;
          * `gate and submit()` -- the tail runs only when the head is true;
          * `not gate or submit()` -- the tail runs only when the head is false.

        Without this a fully-gated ternary submit is read as ungated and
        (allowlist-riskily) flagged; with it the gate is honoured, and the
        polarity handling keeps an INVERTED ternary flagged.
        """
        if isinstance(node, ast.IfExp):
            self._expression(node.test, established)
            self._expression(node.body, established | gate_facts(node.test, True))
            self._expression(node.orelse, established | gate_facts(node.test, False))
            return
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            want_true = isinstance(node.op, ast.And)
            accumulated = established
            for value in node.values:
                self._expression(value, accumulated)
                accumulated = accumulated | gate_facts(value, want_true)
            return
        if isinstance(node, ast.Call) and is_submit_call(node, self.tainted):
            self.submit_sites += 1
            missing = REQUIRED_FACTS - established
            if missing:
                self.violations.append(
                    Violation(
                        module=self.module,
                        line=node.lineno,
                        kind="ungated_submit",
                        detail=(
                            f"{_expr_text(node.func) or 'dispatch'}(...) can submit a live option "
                            f"order without {' and '.join(sorted(missing))}"
                        ),
                    )
                )
        for child in ast.iter_child_nodes(node):
            self._expression(child, established)

    # -- statement level

    def _block(self, body: list[ast.stmt], established: frozenset[str]) -> None:
        current = established
        for statement in body:
            current = self._statement(statement, current)

    def _statement(self, statement: ast.stmt, current: frozenset[str]) -> frozenset[str]:
        if isinstance(statement, ast.If):
            self._expression(statement.test, current)
            self._block(statement.body, current | gate_facts(statement.test, True))
            self._block(statement.orelse, current | gate_facts(statement.test, False))
            if _always_exits(statement.body):
                # An early-return / raise guard: past it, the test was false.
                return current | frozenset(gate_facts(statement.test, False))
            return current
        if isinstance(statement, (ast.For, ast.AsyncFor)):
            self._expression(statement.iter, current)
            self._block(statement.body, current)
            self._block(statement.orelse, current)
            return current
        if isinstance(statement, ast.While):
            self._expression(statement.test, current)
            self._block(statement.body, current | gate_facts(statement.test, True))
            self._block(statement.orelse, current)
            return current
        if isinstance(statement, (ast.With, ast.AsyncWith)):
            for item in statement.items:
                self._expression(item.context_expr, current)
            self._block(statement.body, current)
            return current
        if isinstance(statement, ast.Try):
            self._block(statement.body, current)
            for handler in statement.handlers:
                self._block(handler.body, current)
            self._block(statement.orelse, current)
            self._block(statement.finalbody, current)
            return current
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # A nested def inherits the facts holding where it was defined: a
            # closure written after the gate is inside the gate's scope.
            self._block(statement.body, current)
            return current
        if isinstance(statement, ast.Assert):
            self._expression(statement.test, current)
            return current | frozenset(gate_facts(statement.test, True))
        self._expression(statement, current)
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            return current | frozenset(guard_call_facts(statement.value))
        return current


# --- property B: no naked short / undefined risk ------------------------------


@dataclass(frozen=True)
class Leg:
    node: ast.AST
    side: str | None
    effect: str | None
    line: int


def _dict_entries(node: ast.Dict) -> dict[str, ast.expr]:
    entries: dict[str, ast.expr] = {}
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            entries[key.value.strip().lower()] = value
    return entries


def _leg_from_dict(node: ast.Dict, consts: dict[str, str] | None = None) -> Leg | None:
    entries = _dict_entries(node)
    if _SIDE_KEY not in entries and _EFFECT_KEY not in entries:
        return None
    return Leg(
        node=node,
        side=_constant_str(entries.get(_SIDE_KEY), consts),
        effect=_constant_str(entries.get(_EFFECT_KEY), consts),
        line=node.lineno,
    )


def _leg_from_call(call: ast.Call, consts: dict[str, str] | None = None) -> Leg | None:
    """A single-leg order spelled as keyword arguments:
    `place_option_order(side="sell", position_effect="open", ...)`."""
    keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
    if _SIDE_KEY not in keywords and _EFFECT_KEY not in keywords:
        return None
    return Leg(
        node=call,
        side=_constant_str(keywords.get(_SIDE_KEY), consts),
        effect=_constant_str(keywords.get(_EFFECT_KEY), consts),
        line=call.lineno,
    )


def _parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _direction_of(node: ast.AST, consts: dict[str, str] | None = None) -> str | None:
    """The literal `direction` ("debit" / "credit") declared ON this node."""
    if isinstance(node, ast.Dict):
        return _constant_str(_dict_entries(node).get(_DIRECTION_KEY), consts)
    if isinstance(node, ast.Call):
        for keyword in node.keywords:
            if keyword.arg == _DIRECTION_KEY:
                return _constant_str(keyword.value, consts)
        for argument in node.args:
            for child in ast.walk(argument):
                if isinstance(child, ast.Dict):
                    found = _constant_str(_dict_entries(child).get(_DIRECTION_KEY), consts)
                    if found is not None:
                        return found
    return None


def _enclosing_direction(
    node: ast.AST, parents: dict[int, ast.AST], consts: dict[str, str] | None = None
) -> str | None:
    """The order's `direction`, read from the payload node or any node
    enclosing it. A legs LIST is the tightest group, but `direction` lives one
    level out on the payload dict that carries the list -- so the lookup climbs.
    """
    current: ast.AST | None = node
    while current is not None:
        found = _direction_of(current, consts)
        if found is not None:
            return found
        current = parents.get(id(current))
    return None


def _enclosing_function(node: ast.AST, parents: dict[int, ast.AST]) -> ast.AST | None:
    """The nearest enclosing function/module node -- the scope a local `legs`
    list lives in."""
    current: ast.AST | None = parents.get(id(node))
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            return current
        current = parents.get(id(current))
    return None


def _append_leg_groups(
    tree: ast.AST, parents: dict[int, ast.AST], consts: dict[str, str] | None
) -> tuple[dict[int, tuple[int, str]], dict[tuple[int, str], ast.AST]]:
    """Leg dicts appended to a list, keyed by (enclosing scope, list name).

    A defined-risk spread is often assembled imperatively --
    `legs = []; legs.append({buy...}); legs.append({sell...})` -- so the legs
    never share a literal payload node. Without this, each appended dict is its
    own group and the covered short reads as a lone naked short. Grouping the
    appends to one list re-unites them into a single payload.
    """
    member_of: dict[int, tuple[int, str]] = {}
    group_root: dict[tuple[int, str], ast.AST] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "append" or not node.args:
            continue
        arg = node.args[0]
        if not (isinstance(arg, ast.Dict) and _leg_from_dict(arg, consts)):
            continue
        list_name = _expr_text(node.func.value)
        if not list_name:
            continue
        scope = _enclosing_function(node, parents)
        key = (id(scope) if scope is not None else 0, list_name)
        member_of[id(arg)] = key
        # The scope node is a stable anchor for the group's line / direction climb.
        group_root.setdefault(key, scope if scope is not None else arg)
    return member_of, group_root


def naked_short_violations(module: str, tree: ast.Module, tainted: dict[str, frozenset[str]]) -> list[Violation]:
    """Order payloads that open a SELL leg with no covering BUY-to-open leg.

    Legs are grouped by their nearest enclosing PAYLOAD node -- an order call, a
    literal list of legs, or a dict carrying a `legs` key -- so the two legs of a
    vertical spread are read as one defined-risk payload, and a lone sell-to-open
    is read as the naked short it is. Legs assembled by `legs.append({...})` are
    grouped by their target list, the same way.
    """
    consts = _module_string_constants(tree)
    parents = _parent_map(tree)
    append_member, append_root = _append_leg_groups(tree, parents, consts)

    group_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (
            is_submit_call(node, tainted) or _callee_name(node).lower() in _ORDER_TOOL_NAMES
        ):
            group_nodes.add(id(node))
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            if any(isinstance(element, ast.Dict) and _leg_from_dict(element, consts) for element in node.elts):
                group_nodes.add(id(node))
        elif isinstance(node, ast.Dict) and "legs" in _dict_entries(node):
            group_nodes.add(id(node))

    legs: list[Leg] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            leg = _leg_from_dict(node, consts)
        elif isinstance(node, ast.Call):
            leg = _leg_from_call(node, consts)
        else:
            leg = None
        if leg is not None:
            legs.append(leg)

    def group_of(leg: Leg) -> ast.AST:
        node: ast.AST | None = leg.node
        if id(node) in group_nodes and isinstance(node, ast.Call):
            return node  # a call spelling its own single leg IS its payload
        node = parents.get(id(leg.node))
        while node is not None:
            if id(node) in group_nodes:
                return node
            node = parents.get(id(node))
        return leg.node

    grouped: dict[object, list[Leg]] = {}
    roots: dict[object, ast.AST] = {}
    for leg in legs:
        append_key = append_member.get(id(leg.node))
        if append_key is not None:
            grouped.setdefault(append_key, []).append(leg)
            roots[append_key] = append_root[append_key]
            continue
        root = group_of(leg)
        grouped.setdefault(id(root), []).append(leg)
        roots[id(root)] = root

    violations: list[Violation] = []
    for key, members in grouped.items():
        has_long_leg = any(leg.side == "buy" and leg.effect == "open" for leg in members)
        if has_long_leg:
            continue
        naked_legs = [leg for leg in members if leg.side == "sell" and leg.effect == "open"]
        for leg in naked_legs:
            violations.append(
                Violation(
                    module=module,
                    line=leg.line,
                    kind="naked_short",
                    detail="sell-to-open leg with no covering buy-to-open leg (naked short / undefined risk)",
                )
            )
        if naked_legs:
            continue
        # The dynamic case: a CREDIT opening order with no long leg is collecting
        # premium against undefined risk. A leg counts as a possible opening sell
        # when neither of its two classifying fields RULES that out -- effect is
        # not a known "close" and side is not a known "buy". That catches both a
        # dynamic side over a literal `open` effect AND a dynamic position_effect
        # under a literal `sell` side; only a leg proven to be a close or a buy
        # (e.g. a sell-to-close exit) clears it.
        opening_sell = [leg for leg in members if leg.effect != "close" and leg.side != "buy"]
        if _enclosing_direction(roots[key], parents, consts) == "credit" and opening_sell:
            violations.append(
                Violation(
                    module=module,
                    line=getattr(roots[key], "lineno", members[0].line),
                    kind="naked_short",
                    detail="credit opening order with no buy-to-open leg (undefined risk)",
                )
            )
    return violations


def _docstring_ids(tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
                if isinstance(node.body[0].value.value, str):
                    ids.add(id(node.body[0].value))
    return ids


def forbidden_strategy_violations(module: str, tree: ast.Module) -> list[Violation]:
    """Definitions, calls and payload values naming an undefined-risk strategy.

    Read only from positions where the token IS the instruction -- a def/class
    name, a callee, or the value of a strategy/side/position_effect key. A
    docstring or a refusal message that says "no naked shorts" is not a path.
    """
    skip = _docstring_ids(tree)
    violations: list[Violation] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if _NAKED_NAME.search(node.name) and not _FORBIDDING_NAME.match(node.name):
                violations.append(
                    Violation(module, node.lineno, "undefined_risk_name", f"defines {node.name!r}")
                )
        elif isinstance(node, ast.Call):
            name = _callee_name(node)
            if name and _NAKED_NAME.search(name) and not _FORBIDDING_NAME.match(name):
                violations.append(Violation(module, node.lineno, "undefined_risk_name", f"calls {name!r}"))
            for keyword in node.keywords:
                if keyword.arg and keyword.arg.strip().lower() in _STRATEGY_KEYS:
                    violations.extend(_strategy_value_violations(module, keyword.value, skip))
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    if key.value.strip().lower() in _STRATEGY_KEYS:
                        violations.extend(_strategy_value_violations(module, value, skip))
    return violations


def _strategy_value_violations(module: str, value: ast.AST, skip: set[int]) -> list[Violation]:
    found: list[Violation] = []
    for child in ast.walk(value):
        if not isinstance(child, ast.Constant) or not isinstance(child.value, str):
            continue
        if id(child) in skip:
            continue
        if _NAKED_NAME.search(child.value):
            found.append(
                Violation(module, child.lineno, "undefined_risk_name", f"strategy value {child.value!r}")
            )
    return found


# --- the scan -----------------------------------------------------------------


def scan_sources(sources: dict[str, str]) -> GuardReport:
    """Scan a map of {relative_path: source} for ungated submits and naked shorts."""
    parsed, in_scope, direct, tainted = discover_execution_modules(sources)
    unparsable = [relative for relative in sources if relative not in parsed]

    report = GuardReport(
        scanned=set(in_scope),
        order_tool_modules=set(direct),
        unparsable=unparsable,
    )
    for relative in sorted(in_scope):
        tree = parsed[relative]
        scanner = _GateScanner(relative, tainted)
        scanner.scan(tree)
        report.ungated.extend(scanner.violations)
        report.submit_sites += scanner.submit_sites
        report.naked.extend(naked_short_violations(relative, tree, tainted))
        report.naked.extend(forbidden_strategy_violations(relative, tree))
    return report


def scan_tree(root: Path) -> GuardReport:
    """Scan a single source tree (bare filenames, relative to `root`)."""
    return scan_sources(_collect_sources(root))


def scan_repo(base: Path = REPO_ROOT) -> GuardReport:
    """Scan every configured source root under `base` as ONE merged tree.

    This is the real build gate: an options execution module is held to both
    properties whether it lands in src/ or scripts/. Paths are kept relative to
    `base`, so a module reads as `src/...` or `scripts/...`.
    """
    sources: dict[str, str] = {}
    for subdir in SCAN_SUBDIRS:
        root = base / subdir
        if root.exists():
            sources.update(_collect_sources(root, base=base))
    return scan_sources(sources)


# --- fixtures -----------------------------------------------------------------


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


UNGATED_SUBMIT = '''
def submit(connector, payload):
    return connector.place_option_order(**payload)
'''

CONFIRM_ONLY_GATE = '''
def submit(connector, payload, confirm_live_order=False):
    if confirm_live_order is not True:
        return {"submitted": False}
    return connector.place_option_order(**payload)
'''

ARM_ONLY_GATE = '''
def submit(connector, payload, arm_store):
    if not arm_store.is_armed("options"):
        return {"submitted": False}
    return connector.place_option_order(**payload)
'''

# The shape the equities guard's docstring names as the reason it declined a
# polarity analysis: a confirm flag is present, and read BACKWARDS.
INVERTED_GATE = '''
def submit(connector, payload, confirm_live_order, armed):
    if not (confirm_live_order and armed):
        return connector.place_option_order(**payload)
    return {"submitted": False}
'''

# `or` taken TRUE proves neither operand -- one flag alone reaches the submit.
DISJUNCTIVE_GATE = '''
def submit(connector, payload, confirm_live_order, armed):
    if confirm_live_order or armed:
        return connector.place_option_order(**payload)
    return {"submitted": False}
'''

MODULE_LEVEL_SUBMIT = '''
CONNECTOR = None

RESULT = CONNECTOR.place_option_order(symbol="SPY")
'''

# The gate idiom the equities broker uses, plus the options lane's arm read.
CLEAN_BROKER = '''
"""Places DEFINED-RISK option orders. No naked shorts, ever."""

VENUE = "robinhood_options"


class OptionsBroker:
    def __init__(self, client, arm_store, dry_run=True, confirm_live_order=False):
        self.client = client
        self.arm_store = arm_store
        self.dry_run = dry_run
        self.confirm_live_order = confirm_live_order

    def place_long_call(self, payload):
        if not (self.dry_run is False and self.confirm_live_order is True):
            return {"submitted": False, "status": "dry_run_order_preview"}
        if not self.arm_store.is_armed("options"):
            return {"submitted": False, "status": "options_lane_disarmed"}
        legs = [{"side": "buy", "position_effect": "open", "ratio_quantity": 1}]
        return self.client.place_option_order(legs=legs, direction="debit", **payload)
'''

EARLY_RETURN_GATE = '''
def submit(connector, payload, confirm_live_order, armed):
    if not (confirm_live_order is True and armed is True):
        return {"submitted": False, "status": "dry_run_order_preview"}
    return connector.place_option_order(**payload)
'''

DE_MORGAN_GATE = '''
def submit(connector, payload, confirm_live_order, arm_store):
    if not confirm_live_order or not arm_store.is_armed("options"):
        return {"submitted": False}
    return connector.place_option_order(**payload)
'''

ASSERT_GATE = '''
def submit(connector, payload, confirm_live_order, armed):
    assert confirm_live_order and armed
    return connector.place_option_order(**payload)
'''

HELPER_GUARD_GATE = '''
class Lane:
    def _assert_confirmed(self):
        raise NotImplementedError

    def _assert_options_lane_armed(self):
        raise NotImplementedError

    def submit(self, connector, payload):
        self._assert_confirmed()
        self._assert_options_lane_armed()
        return connector.place_option_order(**payload)
'''

TOOL_NAMES_MODULE = '''
PLACE_OPTION_TOOL = "place_option_order"
CANCEL_OPTION_TOOL = "cancel_option_order"
'''

IMPORTED_CONSTANT_DISPATCH = '''
from tool_names import PLACE_OPTION_TOOL


class Lane:
    def __init__(self, connector):
        self._connector = connector

    def submit(self, payload):
        return self._connector._invoke(PLACE_OPTION_TOOL, payload)
'''

SPLIT_LITERAL_TOOL_NAME = '''
ORDER = "place_option" + "_order"


def submit(connector, payload):
    return connector.route(ORDER, payload)
'''

DYNAMIC_WRAPPER_DISPATCH = '''
def submit(connector, tool, payload):
    return connector._call(tool, payload)
'''

RESOLVED_READ_DISPATCH = '''
def chains(connector, symbol):
    return connector._call("get_option_chains", {"symbol": symbol})
'''

CANCEL_WITHOUT_ARM = '''
def cancel(connector, order_id, confirm_live_order):
    if confirm_live_order is not True:
        return {"cancelled": False}
    return connector.cancel_option_order(order_id=order_id)
'''

SINGLE_LEG_SELL_TO_OPEN = '''
def submit(connector, confirm_live_order, armed):
    if not (confirm_live_order and armed):
        return {"submitted": False}
    return connector.place_option_order(side="sell", position_effect="open", quantity="1")
'''

NAKED_SHORT_LEGS = '''
def build_order():
    return {
        "direction": "credit",
        "legs": [
            {"side": "sell", "position_effect": "open", "ratio_quantity": 1, "option": "url"},
        ],
    }
'''

DEFINED_RISK_VERTICAL = '''
def build_order():
    return {
        "direction": "debit",
        "legs": [
            {"side": "buy", "position_effect": "open", "ratio_quantity": 1, "option": "long"},
            {"side": "sell", "position_effect": "open", "ratio_quantity": 1, "option": "short"},
        ],
    }
'''

LONG_CALL_ONLY = '''
def build_order():
    return {
        "direction": "debit",
        "legs": [{"side": "buy", "position_effect": "open", "ratio_quantity": 1}],
    }
'''

SELL_TO_CLOSE = '''
def build_exit():
    return {
        "direction": "credit",
        "legs": [{"side": "sell", "position_effect": "close", "ratio_quantity": 1}],
    }
'''

DYNAMIC_SIDE_CREDIT_OPEN = '''
def build_order(side):
    return {
        "direction": "credit",
        "legs": [{"side": side, "position_effect": "open", "ratio_quantity": 1}],
    }
'''

NAKED_STRATEGY_FUNCTION = '''
def sell_naked_put(connector, payload):
    return connector.place_option_order(**payload)
'''

FORBIDDEN_STRATEGY_VALUE = '''
def build_order(legs):
    return {"strategy": "short_strangle", "legs": legs}
'''

# The precision counterpart: a module whose whole job is REFUSING naked shorts.
# Its guard names and its refusal message both say "naked short"; neither is a
# path. If this fired, the guard would be allowlisted away within a week.
NAKED_SHORT_REFUSAL = '''
"""Refuses naked shorts: this lane never sells to open without a long leg."""


def assert_no_naked_short(legs):
    """A sell-to-open leg with no buy-to-open leg is a naked short."""
    opening = [leg for leg in legs if leg.get("position_effect") == "open"]
    if any(leg.get("side") == "sell" for leg in opening):
        if not any(leg.get("side") == "buy" for leg in opening):
            raise RuntimeError("refusing a naked short / uncovered option leg")
    return legs


def is_covered_call(legs):
    return False
'''

# HARDENING FIXTURE (gap 1): the naked leg's `side` is a module constant, not a
# literal. No `direction` at all, and a literal `open` effect, so ONLY the
# side-classification path can catch it -- fold the constant or it scans clean.
SIDE_CONSTANT_NAKED = '''
SIDE_SELL = "sell"


def build_order():
    return {
        "legs": [{"side": SIDE_SELL, "position_effect": "open", "ratio_quantity": 1}],
    }
'''

# HARDENING FIXTURE (gap 3): a dynamic position_effect under a LITERAL sell side,
# in a credit opening order. No leg can be classified as sell-to-open (effect is
# unknown), and the old credit fallback demanded a literal `open` effect -- so
# this collected-premium-with-no-long-leg order scanned clean.
DYNAMIC_EFFECT_CREDIT = '''
def build_order(effect):
    return {
        "direction": "credit",
        "legs": [{"side": "sell", "position_effect": effect, "ratio_quantity": 1}],
    }
'''

# HARDENING FIXTURE (gap 4, PASS): a gated ternary submit. Both facts gate the
# true branch; the guard must read the IfExp test as a gate, not flag the call.
GATED_TERNARY_SUBMIT = '''
def submit(connector, payload, confirm_live_order, armed):
    return connector.place_option_order(**payload) if confirm_live_order and armed else None
'''

# HARDENING FIXTURE (gap 4, FLAG): the same ternary read BACKWARDS -- the submit
# is the ELSE branch, reached only when the gate is false. Polarity must survive
# into the IfExp, or this inverted shape passes.
INVERTED_TERNARY_SUBMIT = '''
def submit(connector, payload, confirm_live_order, armed):
    return None if confirm_live_order and armed else connector.place_option_order(**payload)
'''

# HARDENING FIXTURE (gap 5): a defined-risk vertical whose legs are assembled by
# two `legs.append({...})` calls. The long and short legs never share a literal
# payload node; group the appends to one list or the short reads as naked.
DEFINED_RISK_VERTICAL_APPEND = '''
def build_order():
    legs = []
    legs.append({"side": "buy", "position_effect": "open", "ratio_quantity": 1, "option": "long"})
    legs.append({"side": "sell", "position_effect": "open", "ratio_quantity": 1, "option": "short"})
    return {"direction": "debit", "legs": legs}
'''

# HARDENING FIXTURE (gap 5, still FLAG): appends to one list that are a LONE
# sell-to-open -- grouping must not become a blanket amnesty for appended legs.
NAKED_SHORT_APPEND = '''
def build_order():
    legs = []
    legs.append({"side": "sell", "position_effect": "open", "ratio_quantity": 1, "option": "short"})
    return {"direction": "credit", "legs": legs}
'''


# --- property A tests: no submit without confirm AND armed --------------------


def test_an_ungated_submit_is_flagged(tmp_path: Path) -> None:
    report = scan_tree(write_tree(tmp_path, {"options_broker.py": UNGATED_SUBMIT}))

    assert "options_broker.py" in report.scanned
    assert [violation.kind for violation in report.ungated] == ["ungated_submit"]
    assert "armed" in report.ungated[0].detail and "confirm" in report.ungated[0].detail


def test_a_submit_gated_only_by_confirm_is_flagged(tmp_path: Path) -> None:
    """The equities gate, ported verbatim, is NOT enough here: the OPTIONS lane
    must also be armed via the dashboard toggle."""
    report = scan_tree(write_tree(tmp_path, {"options_broker.py": CONFIRM_ONLY_GATE}))

    assert [violation.kind for violation in report.ungated] == ["ungated_submit"]
    assert report.ungated[0].detail.endswith("without armed")


def test_a_submit_gated_only_by_the_arm_toggle_is_flagged(tmp_path: Path) -> None:
    """Armed is a human toggle, not a human CONFIRM. A real order needs both."""
    report = scan_tree(write_tree(tmp_path, {"options_broker.py": ARM_ONLY_GATE}))

    assert [violation.kind for violation in report.ungated] == ["ungated_submit"]
    assert report.ungated[0].detail.endswith("without confirm")


def test_an_inverted_gate_is_flagged(tmp_path: Path) -> None:
    """MUTATION TEST for the polarity analysis. Both flag names are present and
    read BACKWARDS -- the submit happens when the gate is NOT cleared. This is
    the exact shape a proximity check passes; delete the polarity handling in
    gate_facts and this module scans clean."""
    report = scan_tree(write_tree(tmp_path, {"options_broker.py": INVERTED_GATE}))

    assert [violation.kind for violation in report.ungated] == ["ungated_submit"]


def test_a_disjunctive_gate_is_flagged(tmp_path: Path) -> None:
    """`confirm or armed` lets EITHER flag alone reach the connector. An `or`
    known true establishes neither operand, so nothing is cleared."""
    report = scan_tree(write_tree(tmp_path, {"options_broker.py": DISJUNCTIVE_GATE}))

    assert [violation.kind for violation in report.ungated] == ["ungated_submit"]


def test_a_module_level_submit_is_flagged(tmp_path: Path) -> None:
    """An import-time order is gated by nothing at all."""
    report = scan_tree(write_tree(tmp_path, {"options_boot.py": MODULE_LEVEL_SUBMIT}))

    assert [violation.kind for violation in report.ungated] == ["ungated_submit"]


def test_a_gated_ternary_submit_passes(tmp_path: Path) -> None:
    """HARDENING (gap 4). `submit(...) if confirm and armed else None` is fully
    gated -- the IfExp test dominates the true branch. Delete the IfExp handling
    in _expression and this clean, idiomatic shape is (wrongly) flagged."""
    report = scan_tree(write_tree(tmp_path, {"options_broker.py": GATED_TERNARY_SUBMIT}))

    assert "options_broker.py" in report.scanned
    assert report.submit_sites == 1
    assert report.violations == []


def test_an_inverted_ternary_submit_is_flagged(tmp_path: Path) -> None:
    """HARDENING (gap 4, polarity). The submit is the ELSE branch: it runs only
    when the gate is FALSE. Reading the IfExp without carrying polarity would
    clear it; it must stay flagged."""
    report = scan_tree(write_tree(tmp_path, {"options_broker.py": INVERTED_TERNARY_SUBMIT}))

    assert [violation.kind for violation in report.ungated] == ["ungated_submit"]


def test_the_early_return_guard_shape_passes(tmp_path: Path) -> None:
    report = scan_tree(write_tree(tmp_path, {"options_broker.py": EARLY_RETURN_GATE}))

    assert "options_broker.py" in report.scanned
    assert report.submit_sites == 1
    assert report.violations == []


def test_a_de_morgan_guard_passes(tmp_path: Path) -> None:
    """`if not confirm or not armed: return` -- known FALSE, both operands are
    false, so both facts hold past the guard."""
    report = scan_tree(write_tree(tmp_path, {"options_broker.py": DE_MORGAN_GATE}))

    assert report.submit_sites == 1
    assert report.violations == []


def test_an_assert_guard_passes(tmp_path: Path) -> None:
    report = scan_tree(write_tree(tmp_path, {"options_broker.py": ASSERT_GATE}))

    assert report.submit_sites == 1
    assert report.violations == []


def test_named_guard_helpers_clear_the_gate(tmp_path: Path) -> None:
    """`self._assert_confirmed()` / `self._assert_options_lane_armed()` -- the
    fact-carrying names are the assertion helpers themselves."""
    report = scan_tree(write_tree(tmp_path, {"options_broker.py": HELPER_GUARD_GATE}))

    assert report.submit_sites == 1
    assert report.violations == []


def test_the_clean_broker_shape_passes(tmp_path: Path) -> None:
    """The guard must not fire on the shape it is asking people to write."""
    report = scan_tree(write_tree(tmp_path, {"robinhood_options_broker.py": CLEAN_BROKER}))

    assert "robinhood_options_broker.py" in report.scanned
    assert report.submit_sites == 1
    assert report.violations == []


def test_a_submit_dispatched_through_an_imported_constant_is_analyzed(tmp_path: Path) -> None:
    """The tool name is never spelled in this module and its filename carries no
    options hint; constant propagation is what reaches it."""
    report = scan_tree(
        write_tree(tmp_path, {"tool_names.py": TOOL_NAMES_MODULE, "lane_runner.py": IMPORTED_CONSTANT_DISPATCH})
    )

    assert "lane_runner.py" in report.scanned
    assert [violation.module for violation in report.ungated] == ["lane_runner.py"]


def test_a_split_literal_tool_name_taints_and_is_analyzed(tmp_path: Path) -> None:
    """`"place_option" + "_order"` forms a tool name no substring search sees."""
    report = scan_tree(write_tree(tmp_path, {"router_x.py": SPLIT_LITERAL_TOOL_NAME}))

    assert "router_x.py" in report.scanned
    assert [violation.kind for violation in report.ungated] == ["ungated_submit"]


def test_an_unresolved_dynamic_dispatch_counts_as_a_submit(tmp_path: Path) -> None:
    """A connector dispatch whose tool is computed cannot be shown to be a read,
    so it is treated as a possible submit -- failing closed."""
    report = scan_tree(write_tree(tmp_path, {"options_router.py": DYNAMIC_WRAPPER_DISPATCH}))

    assert [violation.kind for violation in report.ungated] == ["ungated_submit"]


def test_a_resolved_read_dispatch_is_not_a_submit(tmp_path: Path) -> None:
    """Precision: the read tools are the lane's whole analysis surface, and
    every one of them would need a confirm+arm gate if this fired."""
    report = scan_tree(write_tree(tmp_path, {"options_reads.py": RESOLVED_READ_DISPATCH}))

    assert "options_reads.py" in report.scanned
    assert report.submit_sites == 0
    assert report.violations == []


def test_a_cancel_is_not_required_to_be_armed(tmp_path: Path) -> None:
    """Deliberate asymmetry: cancel REDUCES exposure. Requiring the lane to be
    armed to cancel would mean disarming locked in every open order."""
    report = scan_tree(write_tree(tmp_path, {"options_broker.py": CANCEL_WITHOUT_ARM}))

    assert "options_broker.py" in report.scanned
    assert report.violations == []


# --- property B tests: no naked short / undefined risk ------------------------


def test_a_single_leg_sell_to_open_is_flagged(tmp_path: Path) -> None:
    """FIXTURE PROVING THE BUILD FAILS. Fully human-gated -- confirm and armed
    both cleared -- and still refused, because the position itself is naked."""
    report = scan_tree(write_tree(tmp_path, {"options_broker.py": SINGLE_LEG_SELL_TO_OPEN}))

    assert report.ungated == []
    assert [violation.kind for violation in report.naked] == ["naked_short"]


def test_a_sell_to_open_leg_with_no_long_leg_is_flagged(tmp_path: Path) -> None:
    report = scan_tree(write_tree(tmp_path, {"options_legs.py": NAKED_SHORT_LEGS}))

    assert "options_legs.py" in report.scanned
    assert [violation.kind for violation in report.naked] == ["naked_short"]


def test_a_credit_opening_order_with_a_dynamic_side_is_flagged(tmp_path: Path) -> None:
    """The side is computed, so no leg can be classified -- but a CREDIT opening
    order with no buy-to-open leg is collecting premium with no long leg."""
    report = scan_tree(write_tree(tmp_path, {"options_legs.py": DYNAMIC_SIDE_CREDIT_OPEN}))

    assert [violation.kind for violation in report.naked] == ["naked_short"]
    assert "credit opening order" in report.naked[0].detail


def test_a_naked_leg_whose_side_is_a_module_constant_is_flagged(tmp_path: Path) -> None:
    """HARDENING (gap 1). `{"side": SIDE_SELL, "position_effect": "open"}` with
    `SIDE_SELL = "sell"`. There is no `direction`, so ONLY side classification
    can catch it -- stop folding the side constant and this scans clean."""
    report = scan_tree(write_tree(tmp_path, {"options_legs.py": SIDE_CONSTANT_NAKED}))

    assert "options_legs.py" in report.scanned
    assert [violation.kind for violation in report.naked] == ["naked_short"]


def test_a_credit_order_with_a_dynamic_effect_and_literal_sell_is_flagged(tmp_path: Path) -> None:
    """HARDENING (gap 3). Side is a literal `sell`, position_effect is computed.
    No leg classifies as sell-to-open, but a CREDIT opening order whose only leg
    is not provably a close or a buy is collecting premium naked."""
    report = scan_tree(write_tree(tmp_path, {"options_legs.py": DYNAMIC_EFFECT_CREDIT}))

    assert [violation.kind for violation in report.naked] == ["naked_short"]
    assert "credit opening order" in report.naked[0].detail


def test_a_selling_to_close_credit_order_still_passes(tmp_path: Path) -> None:
    """HARDENING (gap 3, precision). The broadened credit rule must NOT swallow a
    legitimate exit: a leg with a literal `close` effect is proven not-opening."""
    report = scan_tree(write_tree(tmp_path, {"options_legs.py": SELL_TO_CLOSE}))

    assert "options_legs.py" in report.scanned
    assert report.violations == []


def test_a_defined_risk_vertical_built_by_append_passes(tmp_path: Path) -> None:
    """HARDENING (gap 5). Legs assembled by `legs.append({...})` are one payload;
    the covered short must not read as naked. Drop the append grouping and the
    sell leg is flagged."""
    report = scan_tree(write_tree(tmp_path, {"options_legs.py": DEFINED_RISK_VERTICAL_APPEND}))

    assert "options_legs.py" in report.scanned
    assert report.submit_sites == 0
    assert report.violations == []


def test_a_lone_appended_sell_to_open_is_still_flagged(tmp_path: Path) -> None:
    """HARDENING (gap 5, precision). Append grouping re-unites legs; it does not
    excuse them. A lone appended sell-to-open is still a naked short."""
    report = scan_tree(write_tree(tmp_path, {"options_legs.py": NAKED_SHORT_APPEND}))

    assert [violation.kind for violation in report.naked] == ["naked_short"]


def test_a_defined_risk_vertical_spread_passes(tmp_path: Path) -> None:
    """A short leg is allowed when a long leg covers it -- that IS a defined-risk
    spread, and the lane exists to trade them."""
    report = scan_tree(write_tree(tmp_path, {"options_legs.py": DEFINED_RISK_VERTICAL}))

    assert "options_legs.py" in report.scanned
    assert report.violations == []


def test_a_long_call_passes(tmp_path: Path) -> None:
    report = scan_tree(write_tree(tmp_path, {"options_legs.py": LONG_CALL_ONLY}))

    assert report.violations == []


def test_selling_to_close_a_long_is_not_a_naked_short(tmp_path: Path) -> None:
    """Exiting a long option is a sell for a credit. If this fired, the lane
    could open positions it was forbidden to close."""
    report = scan_tree(write_tree(tmp_path, {"options_legs.py": SELL_TO_CLOSE}))

    assert "options_legs.py" in report.scanned
    assert report.violations == []


def test_a_naked_strategy_function_is_flagged(tmp_path: Path) -> None:
    report = scan_tree(write_tree(tmp_path, {"options_plays.py": NAKED_STRATEGY_FUNCTION}))

    kinds = sorted({violation.kind for violation in report.violations})
    assert "undefined_risk_name" in kinds


def test_a_forbidden_strategy_value_in_a_payload_is_flagged(tmp_path: Path) -> None:
    report = scan_tree(write_tree(tmp_path, {"options_plays.py": FORBIDDEN_STRATEGY_VALUE}))

    assert [violation.kind for violation in report.naked] == ["undefined_risk_name"]
    assert "short_strangle" in report.naked[0].detail


def test_a_module_that_refuses_naked_shorts_is_not_itself_flagged(tmp_path: Path) -> None:
    """Precision. The runtime guard, its docstrings and its refusal message all
    say "naked short"; none of them is a path to one."""
    report = scan_tree(write_tree(tmp_path, {"options_compliance.py": NAKED_SHORT_REFUSAL}))

    assert "options_compliance.py" in report.scanned
    assert report.violations == []


# --- discovery and precision --------------------------------------------------


def test_the_filename_hint_matches_options_and_not_the_other_lanes() -> None:
    assert has_filename_hint("options_scout/analyzer.py")
    assert has_filename_hint("robinhood_options_broker.py")
    assert has_filename_hint("option_legs.py")
    assert not has_filename_hint("robinhood_crypto_client.py")
    assert not has_filename_hint("robinhood_equity_broker.py")
    assert not has_filename_hint("equity_compliance.py")


def test_modules_with_no_options_involvement_are_not_scanned(tmp_path: Path) -> None:
    report = scan_tree(
        write_tree(tmp_path, {"crypto_client.py": 'PAIR = "BTC-USD"\n\n\ndef quote(symbol):\n    return symbol\n'})
    )

    assert report.scanned == set()
    assert report.violations == []


@pytest.mark.parametrize("tool", ORDER_TOOLS)
def test_every_order_tool_reference_puts_a_module_in_scope(tmp_path: Path, tool: str) -> None:
    source = f'def go(connector, payload):\n    return connector.{tool}(**payload)\n'
    report = scan_tree(write_tree(tmp_path, {"lane_exec.py": source}))

    assert report.order_tool_modules == {"lane_exec.py"}
    assert "lane_exec.py" in report.scanned


def test_an_options_execution_module_under_scripts_is_scanned(tmp_path: Path) -> None:
    """HARDENING (gap 2). The real scan spans src/ AND scripts/. A naked short in
    a scripts/ options module is discovered and flagged exactly as in src/; drop
    "scripts" from SCAN_SUBDIRS and it goes unseen."""
    write_tree(
        tmp_path,
        {
            "src/placeholder.py": 'VALUE = 1\n',
            "scripts/options_exec.py": NAKED_SHORT_LEGS,
        },
    )
    report = scan_repo(tmp_path)

    assert "scripts/options_exec.py" in report.scanned
    assert [violation.module for violation in report.naked] == ["scripts/options_exec.py"]
    assert [violation.kind for violation in report.naked] == ["naked_short"]


def test_scripts_is_one_of_the_scanned_source_roots() -> None:
    """A blunt guard on the knob itself, so the intent survives a refactor."""
    assert "scripts" in SCAN_SUBDIRS


# --- non-vacuity --------------------------------------------------------------


def test_soundness_check_rejects_an_empty_scan_when_order_tools_exist() -> None:
    report = GuardReport(order_tool_modules={"options_broker.py"}, scanned=set())

    with pytest.raises(VacuousScan, match="nothing was scanned"):
        assert_scan_is_sound(report)


def test_soundness_check_rejects_a_scan_that_missed_an_order_tool_module() -> None:
    report = GuardReport(order_tool_modules={"options_broker.py"}, scanned={"options_util.py"})

    with pytest.raises(VacuousScan, match="missing from the scan"):
        assert_scan_is_sound(report)


def test_soundness_check_rejects_an_unparsable_module() -> None:
    report = GuardReport(unparsable=["broken.py"])

    with pytest.raises(AssertionError, match="unparsable"):
        assert_scan_is_sound(report)


def test_soundness_check_passes_a_real_non_empty_scan(tmp_path: Path) -> None:
    report = scan_tree(write_tree(tmp_path, {"robinhood_options_broker.py": CLEAN_BROKER}))

    assert report.order_tool_modules == {"robinhood_options_broker.py"}
    assert_scan_is_sound(report)


# --- the real tree ------------------------------------------------------------


def test_repo_src_has_no_ungated_option_submit_and_no_naked_short_path() -> None:
    """The build gate. The execution lane has no modules yet, so today this
    proves the analysis-only options_scout contains no order path and no
    undefined-risk play; the moment an execution module lands -- under src/ OR
    scripts/ -- it is discovered by content and held to both properties."""
    report = scan_repo()

    assert_scan_is_sound(report)
    assert report.violations == [], "\n".join(str(violation) for violation in report.violations)


def test_the_real_scan_is_not_empty() -> None:
    """Non-vacuity against the real tree: src/options_scout is in scope, so a
    clean report above is a fact about code that was actually read."""
    report = scan_repo()

    assert any(module.startswith("src/options_scout/") for module in report.scanned)


def test_the_equities_and_crypto_lanes_are_out_of_scope() -> None:
    """This guard is the options lane's. It must not start policing -- or
    failing on -- the two lanes it was told not to touch."""
    report = scan_repo()

    assert "src/robinhood_equity_broker.py" not in report.scanned
    assert "src/robinhood_crypto_client.py" not in report.scanned
