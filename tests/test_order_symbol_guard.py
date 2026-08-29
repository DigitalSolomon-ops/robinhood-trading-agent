"""The build fails if an order-placing SYMBOL is hardcoded in an equity
execution module.

This is the project's `first_task` (project.yaml gate `order-symbol-test-first`):
a safety property added at the end was absent for the whole build, so it goes in
before the equities lane exists. It is written to still be correct once the lane
does exist.

WHAT IT GUARDS
    An equity execution module must take its symbol from configuration, a signal
    or a caller argument -- never from a literal baked into the code. A literal
    ticker beside an order call is how a lane ends up trading something nobody
    approved.

HOW A MODULE GETS INTO SCOPE (discovery is by CONTENT, not by filename)
    1. Direct reference: the source text mentions a connector order tool
       (place_equity_order / review_equity_order / cancel_equity_order) anywhere
       -- as a call, a string in a constant, a value in a dispatch table, or an
       argument to a thin _call/_invoke/dispatch wrapper. Raw text is used, so a
       mention in a comment also pulls the module in (conservative on purpose).
    2. Constant propagation: a name assigned an order-tool string is "tainted",
       and any module referencing that name is in scope too -- transitively.
       That is what puts a module like src/rh_lane.py, which imports a constant
       and dispatches through it and never spells the tool name itself, in scope.
    3. Filename hint: an equity / stock / rh-prefixed path, UNION-ed with the
       above. A hint alone is enough; the crypto lane's robinhood_crypto_client
       is not hinted (its path token is "robinhood", not "rh") and does not
       touch the equity order tools, so the crypto lane stays out of scope.

HOW A SYMBOL IS DETECTED (case-insensitive both tiers)
    Tier 1 -- vocabulary. A curated US equity/ETF ticker list, PLUS every symbol
        the repo's own config lists under an equities/stock key. Tier 1 is the
        authoritative tier: whatever this lane is configured to trade can never
        be hardcoded, and it matches 'aapl' exactly as it matches 'AAPL'.
    Tier 2 -- shape. An all-caps 1-5 letter literal that is not a documented
        stop-word (SQL, HTTP, order-protocol and prose tokens this repo really
        contains). Catches a ticker outside the vocabulary. Its stop-word list
        is the reason tier 1 exists: tickers that collide with English ('A',
        'KEY', 'ALL') are only reachable through tier 1's config-fed vocabulary.

WHAT IT DELIBERATELY DOES NOT DO
    No static "no order without a confirm flag" polarity analysis. That is not
    sound -- an `if not confirm: submit(...)` shape passes it -- so the human
    gate is enforced at RUNTIME by the equity-client / equity-broker tests
    instead, and is not faked here.

Structure mirrors tests/test_safety.py: plain helpers, fixtures built in
tmp_path, and one assertion pinned against the real tree.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
TRADING_RULES = REPO_ROOT / "config" / "trading_rules.yaml"

# The connector's order-placing toolset (agent/docs/rh-equities-binding.md).
ORDER_TOOLS = (
    "place_equity_order",
    "review_equity_order",
    "cancel_equity_order",
)

# Path tokens that mark a module as equities work regardless of its contents.
# "rh" must start a token: robinhood_crypto_client.py is the crypto lane.
FILENAME_HINTS = re.compile(r"(equit|stock|share|^rh$|^rh[_a-z0-9]*$)", re.IGNORECASE)

# Tier 1, part A: a curated large-cap / major-ETF universe. Matched
# case-insensitively, so 'aapl' is caught as surely as 'AAPL'.
KNOWN_TICKERS = frozenset(
    """
    AAPL MSFT NVDA AMZN GOOG GOOGL META TSLA BRKB AVGO LLY JPM XOM UNH V MA
    PG JNJ HD COST ABBV WMT MRK NFLX KO PEP ADBE CRM BAC TMO AMD CSCO ACN
    LIN MCD ABT PFE DHR CMCSA WFC TXN DIS VZ INTU PM COP NEE INTC AMGN
    CAT UNP LOW SPGI IBM GE BA HON RTX QCOM NOW AMAT BKNG SBUX GS ELV DE
    BLK MDT ADP PLD LMT SYK TJX MMC CVS MO ISRG REGN VRTX ZTS CI SO PANW
    MU LRCX ADI KLAC SNPS CDNS ORCL CRWD SHOP UBER ABNB PYPL SQ COIN PLTR
    SOFI RIVN LCID NIO F GM DAL UAL AAL CCL NCLH MARA RIOT HOOD SNAP PINS
    ROKU DKNG CVNA GME AMC BBBY NOK BB SPCE TLRY
    SPY QQQ IWM DIA VOO VTI VEA VWO AGG BND TLT IEF GLD SLV USO XLF XLE
    XLK XLV XLY XLP XLI XLU XLB XLRE SMH SOXL TQQQ SQQQ ARKK VIX UVXY
    """.split()
)

# Tier 2 stop-words: all-caps short tokens this codebase genuinely contains --
# SQL, HTTP verbs, order protocol, shouted prose. Some collide with real
# tickers (A, ALL, KEY, ON, SET); those are reachable through tier 1, which is
# fed by config, so a symbol this lane may actually trade is never excused here.
NON_TICKER_WORDS = frozenset(
    """
    A AN AND ANY ARE AS ASC AT BE BUY BY CAN DAY DESC DO DONE ELSE END ERROR
    EXIT FAIL FALSE FOK FOR FROM GET GTC HEAD IF IN INDEX INTO IOC IS IT JSON
    KEY LIKE LIMIT LIVE MODE NEW NO NOT NOW NULL OK ON OPEN OR ORDER PAPER
    PASS PATCH PLACE POST PUT REAL SELL SET SQL TABLE TEXT THIS TO TRUE URL
    USD UTC VALUE WHERE WILL WITH YES ALL API CLI IAP ROOT ID GMT DELETE
    TRACE HTTP HTTPS USER PASS NAME TYPE SIDE QTY OPTS ARGS SELF NONE INIT
    MAIN TEST DEBUG INFO WARN
    """.split()
)

_TICKER_SHAPE = re.compile(r"^[A-Z]{1,5}$")
_CONFIG_TICKER_SHAPE = re.compile(r"^[A-Za-z][A-Za-z.\-]{0,5}$")


# --- findings ---------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    module: str
    symbol: str
    line: int
    tier: str

    def __str__(self) -> str:
        return f"{self.module}:{self.line} hardcodes order symbol {self.symbol!r} ({self.tier})"


@dataclass
class ScanReport:
    """What the scan looked at, and what it found."""

    order_tool_modules: set[str] = field(default_factory=set)
    scanned: set[str] = field(default_factory=set)
    violations: list[Violation] = field(default_factory=list)
    unparsable: list[str] = field(default_factory=list)

    def symbols(self) -> set[str]:
        return {violation.symbol.upper() for violation in self.violations}


class VacuousScan(AssertionError):
    """Raised when the scan proved nothing because it looked at nothing."""


def assert_scan_is_sound(report: ScanReport) -> None:
    """A clean report is only meaningful if the scan was not empty.

    If any module references an order tool, the scanned set must be non-empty
    and must include that module -- otherwise a discovery bug reads as a pass.
    """
    if report.unparsable:
        raise AssertionError("unparsable modules cannot be cleared: " + ", ".join(sorted(report.unparsable)))
    if report.order_tool_modules and not report.scanned:
        raise VacuousScan(
            "order-tool references exist but nothing was scanned: "
            + ", ".join(sorted(report.order_tool_modules))
        )
    missed = report.order_tool_modules - report.scanned
    if missed:
        raise VacuousScan("order-tool modules missing from the scan: " + ", ".join(sorted(missed)))


# --- vocabulary -------------------------------------------------------------


def config_equity_symbols(config_path: Path = TRADING_RULES) -> set[str]:
    """Every symbol the config lists beneath an equities/stock key.

    Empty today (the lane has no config section yet) and self-extending: the
    moment an equities allowlist lands, hardcoding one of its symbols fails the
    build without anyone remembering to update this test. Crypto pairs never
    qualify -- their key path says nothing about equities, and 'BTC-USD' is not
    ticker-shaped.
    """
    if not config_path.exists():
        return set()
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    found: set[str] = set()

    def walk(node: object, equities_context: bool) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                key_text = str(key).lower()
                walk(value, equities_context or "equit" in key_text or "stock" in key_text)
        elif isinstance(node, list):
            for item in node:
                walk(item, equities_context)
        elif isinstance(node, str) and equities_context and _CONFIG_TICKER_SHAPE.match(node):
            found.add(node.upper())

    walk(loaded, False)
    return found


def ticker_vocabulary(config_path: Path = TRADING_RULES) -> frozenset[str]:
    return frozenset(KNOWN_TICKERS | config_equity_symbols(config_path))


# --- discovery --------------------------------------------------------------


def python_modules(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def references_order_tool(source: str) -> bool:
    """True if the raw source mentions any connector order tool."""
    return any(re.search(rf"\b{tool}\b", source, re.IGNORECASE) for tool in ORDER_TOOLS)


def has_filename_hint(relative_path: str) -> bool:
    tokens = re.split(r"[/\\._\-]+", relative_path)
    return any(FILENAME_HINTS.search(token) for token in tokens if token)


def _string_constants(node: ast.AST) -> list[str]:
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def _referenced_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.split(".")[-1])
            if node.asname:
                names.add(node.asname)
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


def _tainted_assignments(tree: ast.Module, tainted: set[str]) -> set[str]:
    """Module-level names bound to something carrying an order tool.

    Covers `TOOL = "place_equity_order"`, a dispatch table
    `TOOLS = {"place": "place_equity_order"}`, and second-order aliases
    `HANDLER = TOOLS["place"]` once TOOLS is known to be tainted.
    """
    found: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if value is None:
            continue
        carries = any(
            re.search(rf"\b{tool}\b", literal, re.IGNORECASE)
            for literal in _string_constants(value)
            for tool in ORDER_TOOLS
        ) or bool(_referenced_names(value) & tainted)
        if carries:
            found.update(_assignment_targets(node))
    return found


def discover_execution_modules(root: Path) -> tuple[dict[str, ast.Module], set[str], set[str]]:
    """Return (parsed modules, in-scope module paths, direct order-tool modules).

    Scope = direct order-tool references
          UNION modules reached by constant propagation (to a fixpoint)
          UNION filename hints.
    """
    parsed: dict[str, ast.Module] = {}
    unparsable: set[str] = set()
    sources: dict[str, str] = {}

    for path in python_modules(root):
        relative = _rel(path, root)
        source = path.read_text(encoding="utf-8")
        sources[relative] = source
        try:
            parsed[relative] = ast.parse(source, filename=relative)
        except SyntaxError:
            unparsable.add(relative)

    direct = {relative for relative, source in sources.items() if references_order_tool(source)}

    tainted: set[str] = set()
    while True:
        grown = set(tainted)
        for tree in parsed.values():
            grown |= _tainted_assignments(tree, grown)
        if grown == tainted:
            break
        tainted = grown

    in_scope = set(direct)
    if tainted:
        for relative, tree in parsed.items():
            if _referenced_names(tree) & tainted:
                in_scope.add(relative)
    in_scope |= {relative for relative in sources if has_filename_hint(relative)}
    in_scope &= set(parsed)

    return parsed, in_scope, direct | (unparsable & direct)


# --- symbol detection -------------------------------------------------------


def _docstring_ids(tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
                if isinstance(node.body[0].value.value, str):
                    ids.add(id(node.body[0].value))
    return ids


def hardcoded_symbols(
    module: str,
    tree: ast.Module,
    vocabulary: frozenset[str],
) -> list[Violation]:
    """Ticker literals inside a module, case-insensitively.

    Docstrings are skipped -- a docstring cannot place an order -- but every
    other string constant is examined, including dict keys, list items, default
    arguments and the literal parts of f-strings.
    """
    skip = _docstring_ids(tree)
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        for part in re.split(r"[,\s;|]+", node.value.strip()):
            token = part.strip("\"'`()[]{}").rstrip(".")
            if not token:
                continue
            if token.upper() in vocabulary:
                violations.append(Violation(module, token, node.lineno, "known ticker"))
            elif _TICKER_SHAPE.match(token) and token.upper() not in NON_TICKER_WORDS:
                violations.append(Violation(module, token, node.lineno, "ticker-shaped literal"))
    return violations


def scan_tree(root: Path, config_path: Path = TRADING_RULES) -> ScanReport:
    """Scan a source tree for hardcoded order symbols in execution modules."""
    parsed, in_scope, _ = discover_execution_modules(root)
    unparsable = [
        _rel(path, root)
        for path in python_modules(root)
        if _rel(path, root) not in parsed
    ]
    direct = {
        _rel(path, root)
        for path in python_modules(root)
        if _rel(path, root) in parsed and references_order_tool(path.read_text(encoding="utf-8"))
    }
    vocabulary = ticker_vocabulary(config_path)
    violations: list[Violation] = []
    for relative in sorted(in_scope):
        violations.extend(hardcoded_symbols(relative, parsed[relative], vocabulary))
    return ScanReport(
        order_tool_modules=direct,
        scanned=set(in_scope),
        violations=violations,
        unparsable=unparsable,
    )


# --- fixtures ---------------------------------------------------------------


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


LITERAL_CALL = '''
def submit(client, confirm_real_order=False):
    return client.place_equity_order(symbol="AAPL", side="buy", quantity=1)
'''

# The evasion the review found: no order-tool name at the call site, dispatch
# through a module-level constant, and a lowercase ticker.
CONSTANT_DISPATCH = '''
_ORDER_TOOL = "place_equity_order"


def submit(connector, quantity):
    return connector._call(_ORDER_TOOL, {"symbol": "aapl", "quantity": quantity})
'''

TABLE_DISPATCH = '''
TOOLS = {
    "review": "review_equity_order",
    "place": "place_equity_order",
}


def submit(connector):
    return connector.dispatch(TOOLS["place"], {"symbol": "nvda"})
'''

# The constant lives in another module; this one never spells a tool name.
TOOL_NAMES_MODULE = '''
PLACE_ORDER_TOOL = "place_equity_order"
CANCEL_ORDER_TOOL = "cancel_equity_order"
'''

IMPORTED_CONSTANT_DISPATCH = '''
from tool_names import PLACE_ORDER_TOOL


class Lane:
    def __init__(self, connector):
        self._connector = connector

    def submit(self):
        return self._connector._invoke(PLACE_ORDER_TOOL, {"symbol": "MSFT", "quantity": 1})
'''

HINT_ONLY_MODULE = '''
DEFAULT_UNIVERSE = ["TSLA"]


def universe():
    return DEFAULT_UNIVERSE
'''

# A correct module: the symbol arrives as an argument, never as a literal.
CLEAN_EXECUTION_MODULE = '''
"""Places equity orders for AAPL-style tickers -- docstrings are not orders."""

ORDER_TOOL = "place_equity_order"
TIME_IN_FORCE = "GTC"


def submit(connector, symbol, quantity, confirm_real_order=False):
    if not confirm_real_order:
        return {"submitted": False, "status": "dry_run_order_preview", "symbol": symbol}
    payload = {"symbol": symbol, "side": "buy", "quantity": quantity, "tif": TIME_IN_FORCE}
    return connector._call(ORDER_TOOL, payload)
'''


# --- discovery tests --------------------------------------------------------


def test_direct_call_site_is_discovered_and_flagged(tmp_path: Path) -> None:
    report = scan_tree(write_tree(tmp_path, {"executor.py": LITERAL_CALL}))

    assert report.scanned == {"executor.py"}
    assert report.symbols() == {"AAPL"}


def test_constant_dispatch_module_is_in_scope_and_lowercase_ticker_is_caught(tmp_path: Path) -> None:
    """src/rh_lane.py-shaped: no order-tool name at the call site, and 'aapl'."""
    report = scan_tree(write_tree(tmp_path, {"rh_lane.py": CONSTANT_DISPATCH}))

    assert "rh_lane.py" in report.scanned
    assert [violation.symbol for violation in report.violations] == ["aapl"]


def test_dispatch_table_module_is_in_scope_and_flagged(tmp_path: Path) -> None:
    report = scan_tree(write_tree(tmp_path, {"router.py": TABLE_DISPATCH}))

    assert "router.py" in report.scanned
    assert report.symbols() == {"NVDA"}


def test_module_dispatching_via_an_imported_constant_is_in_scope(tmp_path: Path) -> None:
    """The hardest evasion: the tool name is never spelled in this module and
    the filename carries no equity hint. Constant propagation must reach it."""
    report = scan_tree(
        write_tree(
            tmp_path,
            {"tool_names.py": TOOL_NAMES_MODULE, "lane_runner.py": IMPORTED_CONSTANT_DISPATCH},
        )
    )

    assert "lane_runner.py" in report.scanned
    assert report.symbols() == {"MSFT"}


def test_filename_hint_alone_puts_a_module_in_scope(tmp_path: Path) -> None:
    report = scan_tree(write_tree(tmp_path, {"equity_universe.py": HINT_ONLY_MODULE}))

    assert "equity_universe.py" in report.scanned
    assert report.symbols() == {"TSLA"}


def test_case_insensitive_detection_covers_both_cases(tmp_path: Path) -> None:
    report = scan_tree(
        write_tree(
            tmp_path,
            {
                "upper.py": 'def go(c):\n    return c.place_equity_order(symbol="AAPL")\n',
                "lower.py": 'def go(c):\n    return c.place_equity_order(symbol="aapl")\n',
            },
        )
    )

    assert {violation.module for violation in report.violations} == {"upper.py", "lower.py"}


def test_config_listed_equity_symbols_extend_the_vocabulary(tmp_path: Path) -> None:
    """A symbol is guarded because config says the lane may trade it, even if
    it is nowhere near the curated list."""
    config = tmp_path / "trading_rules.yaml"
    config.write_text(
        "equities:\n  allowed_symbols:\n  - ZZZQ\ncrypto:\n  allowed_symbols:\n  - BTC-USD\n",
        encoding="utf-8",
    )
    tree = write_tree(tmp_path / "src", {"lane.py": 'def go(c):\n    return c.place_equity_order(symbol="zzzq")\n'})

    assert "ZZZQ" in config_equity_symbols(config)
    assert "BTC-USD" not in config_equity_symbols(config)
    assert scan_tree(tree, config_path=config).symbols() == {"ZZZQ"}


# --- precision tests --------------------------------------------------------


def test_a_correct_execution_module_passes(tmp_path: Path) -> None:
    """The guard must not fire on the shape it is asking people to write, or it
    gets allowlisted away."""
    report = scan_tree(write_tree(tmp_path, {"equity_broker.py": CLEAN_EXECUTION_MODULE}))

    assert "equity_broker.py" in report.scanned
    assert report.violations == []


def test_modules_with_no_equity_involvement_are_not_scanned(tmp_path: Path) -> None:
    report = scan_tree(
        write_tree(tmp_path, {"crypto_client.py": 'PAIR = "BTC-USD"\n\n\ndef quote(symbol):\n    return symbol\n'})
    )

    assert report.scanned == set()
    assert report.violations == []


def test_the_crypto_lane_module_name_is_not_an_equities_hint() -> None:
    assert not has_filename_hint("robinhood_crypto_client.py")
    assert has_filename_hint("rh_lane.py")
    assert has_filename_hint("equity_client.py")
    assert has_filename_hint("intelligence/stock_universe.py")


# --- non-vacuity ------------------------------------------------------------


def test_soundness_check_rejects_an_empty_scan_when_order_tools_exist() -> None:
    """A silent pass over an empty discovery set is itself a failure."""
    report = ScanReport(order_tool_modules={"rh_lane.py"}, scanned=set(), violations=[])

    with pytest.raises(VacuousScan, match="nothing was scanned"):
        assert_scan_is_sound(report)


def test_soundness_check_rejects_a_scan_that_missed_an_order_tool_module() -> None:
    report = ScanReport(order_tool_modules={"rh_lane.py"}, scanned={"equity_util.py"}, violations=[])

    with pytest.raises(VacuousScan, match="missing from the scan"):
        assert_scan_is_sound(report)


def test_soundness_check_rejects_an_unparsable_module() -> None:
    report = ScanReport(order_tool_modules=set(), scanned=set(), unparsable=["broken.py"])

    with pytest.raises(AssertionError, match="unparsable"):
        assert_scan_is_sound(report)


def test_soundness_check_passes_a_real_non_empty_scan(tmp_path: Path) -> None:
    report = scan_tree(write_tree(tmp_path, {"rh_lane.py": CONSTANT_DISPATCH}))

    assert report.order_tool_modules == {"rh_lane.py"}
    assert_scan_is_sound(report)


def test_scan_is_pinned_non_empty_on_a_tree_that_references_order_tools(tmp_path: Path) -> None:
    """End-to-end of the pin: order-tool references present, scan non-empty."""
    report = scan_tree(
        write_tree(
            tmp_path,
            {
                "tool_names.py": TOOL_NAMES_MODULE,
                "lane_runner.py": IMPORTED_CONSTANT_DISPATCH,
                "notes.py": "VALUE = 1\n",
            },
        )
    )

    assert report.order_tool_modules
    assert report.scanned >= {"tool_names.py", "lane_runner.py"}
    assert_scan_is_sound(report)


# --- the real tree ----------------------------------------------------------


def test_repo_src_has_no_hardcoded_order_symbols() -> None:
    """The build gate. Empty scope today (the equities lane has no modules yet);
    the moment one lands it is discovered by content and checked here."""
    report = scan_tree(SRC)

    assert_scan_is_sound(report)
    assert report.violations == [], "\n".join(str(violation) for violation in report.violations)
