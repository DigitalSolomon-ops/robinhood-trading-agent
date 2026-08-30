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

HOW A SYMBOL IS DETECTED
    Tier 1 -- vocabulary. A curated US equity/ETF ticker list, PLUS every symbol
        the repo's own config lists under an equities/stock key. Tier 1 is the
        authoritative tier: whatever this lane is configured to trade can never
        be hardcoded, and it matches 'aapl' exactly as it matches 'AAPL'.
    Tier 2 -- shape. An all-caps ticker-shaped literal that is not a documented
        stop-word (SQL, HTTP, order-protocol and prose tokens this repo really
        contains). Catches a ticker outside the vocabulary. Its stop-word list
        is the reason tier 1 exists: tickers that collide with English ('A',
        'KEY', 'ALL') are only reachable through tier 1's config-fed vocabulary.
    Tier 3 -- POSITION. A ticker-shaped literal, matched case-INSENSITIVELY and
        with no stop-word excuse, sitting in a `symbol=` slot at an order
        call-site. Tier 2 has to stay upper-case-only because a bare lowercase
        token cannot be told from English prose by shape alone -- but a literal
        in a symbol argument to place/review/cancel_equity_order is not prose,
        the position itself says it is a ticker. Without this tier a lowercase
        OFF-vocabulary ticker (`place_equity_order(symbol="gevity")`) evaded
        tier 1 (not in the list) and tier 2 (not upper-case) and reached the
        broker, which up-cased it into a well-formed live order.
        The runtime half of the same property is the broker's own
        `assert_in_universe` (src/robinhood_equity_broker.py): this tier stops
        an unapproved ticker being written into src/ at all, and that check
        stops one that got there anyway from reaching Robinhood.

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
    SOFI RIVN LCID NIO F GM DAL UAL AAL CCL NCLH MARA RIOT HOOD SNAP PINS RBLX
    ROKU DKNG CVNA GME AMC BBBY NOK BB SPCE TLRY
    SPY QQQ IWM DIA VOO VTI VEA VWO AGG BND TLT IEF GLD SLV USO XLF XLE
    XLK XLV XLY XLP XLI XLU XLB XLRE SMH SOXL TQQQ SQQQ ARKK VIX UVXY
    """.split()
)

# Tier 2 stop-words: all-caps short tokens this codebase genuinely contains --
# SQL, HTTP verbs, order protocol, shouted prose, and the technical-indicator
# names the equities strategy labels its rationale with (SMA/EMA/RSI/MACD).
# Some collide with real tickers (A, ALL, KEY, ON, SET, and RSI -- Rush Street
# Interactive); those are reachable through tier 1, which is fed by config, so
# a symbol this lane may actually trade is never excused here.
NON_TICKER_WORDS = frozenset(
    """
    A AN AND ANY ARE AS ASC AT BE BUY BY CAN DAY DESC DO DONE ELSE END ERROR
    EXIT FAIL FALSE FOK FOR FROM GET GTC HEAD IF IN INDEX INTO IOC IS IT JSON
    KEY LIKE LIMIT LIVE MODE NEW NO NOT NOW NULL OK ON OPEN OR ORDER PAPER
    PASS PATCH PLACE POST PUT REAL SELL SET SQL TABLE TEXT THIS TO TRUE URL
    USD UTC VALUE WHERE WILL WITH YES ALL API CLI IAP ROOT ID GMT DELETE
    TRACE HTTP HTTPS USER PASS NAME TYPE SIDE QTY OPTS ARGS SELF NONE INIT
    MAIN TEST DEBUG INFO WARN
    SMA EMA RSI MACD HIST
    SELECT INSERT UPDATE VALUES CREATE EXISTS TABLE INDEX GROUP COUNT HAVING
    PASSED FAILED ABSENT CRYPTO A-Z
    """.split()
)

# Tier-2 shape, widened to the broker's own character grammar
# (robinhood_equity_broker.py:_EQUITY_SYMBOL is `^[A-Za-z][A-Za-z.\-]{0,5}$`):
# the old `^[A-Z]{1,5}$` missed a SIX-letter symbol ('GEVITY') and a
# class-share dot ('BRK.B') purely on length and punctuation. The shape tier
# stays anchored to UPPER-case, though -- a bare, context-free lowercase token
# ('rblx' but also 'must', 'both', 'agent') cannot be told apart from English
# prose by shape alone. Lowercase tickers are reached two other ways instead:
# the case-insensitive tier-1 vocabulary below (where 'rblx' now lives), and
# tier 3, which applies the case-insensitive _CONFIG_TICKER_SHAPE to a literal
# in a `symbol=` position at an order call-site -- where CONTEXT, not shape,
# establishes that the token is a ticker.
_TICKER_SHAPE = re.compile(r"^[A-Z][A-Z.\-]{0,5}$")
_CONFIG_TICKER_SHAPE = re.compile(r"^[A-Za-z][A-Za-z.\-]{0,5}$")

# Punctuation a ticker carries that its vocabulary entry does not: BRK.B and
# BRK-B both normalize to the curated 'BRKB'. Stripped, and the token
# upper-cased, before a tier-1 lookup -- that is what makes tier 1
# case-insensitive: 'brk.b', 'BRK-B' and 'rblx' all resolve to a vocabulary key.
_TICKER_PUNCT = re.compile(r"[.\-/]")


def _vocab_key(token: str) -> str:
    """A token normalized for a tier-1 vocabulary lookup: punctuation stripped
    and upper-cased, so 'brk.b' and 'BRK-B' both resolve to 'BRKB' and 'rblx'
    resolves to 'RBLX'."""
    return _TICKER_PUNCT.sub("", token).upper()


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


def _fold_concat(node: ast.AST) -> str | None:
    """The folded value of a constant string concatenation, or None.

    `"place_equity" + "_order"` is a single tool name split across two
    literals so that no `\\bplace_equity_order\\b` appears in the raw source --
    folding the `+` recovers it. Handles arbitrarily nested `+` of literals.
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
    """Every string literal in a subtree, plus the folded value of any
    constant string concatenation, so a tool name assembled from `+`-joined
    fragments is seen as the whole name it forms."""
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


# A thin dispatch wrapper: a module that routes a call through one of these is
# executing SOMETHING against the connector, so it is scanned even when it
# never spells an order-tool name and carries no equity filename hint.
_WRAPPER_METHODS = frozenset({"_call", "_invoke", "dispatch"})


def invokes_generic_wrapper(tree: ast.AST) -> bool:
    """True if the module calls a generic `_call`/`_invoke`/`dispatch` method."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _WRAPPER_METHODS:
                return True
    return False


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


def discover_execution_modules(
    root: Path,
) -> tuple[dict[str, ast.Module], set[str], set[str], frozenset[str]]:
    """Return (parsed modules, in-scope paths, direct order-tool modules, tainted names).

    Scope = direct order-tool references
          UNION modules reached by constant propagation (to a fixpoint)
          UNION filename hints.

    The tainted-name set is returned as well as used: tier 3 below needs it to
    recognise a dispatch call-site (`connector._call(_ORDER_TOOL, {...})`) as
    an order call-site, and recomputing the fixpoint there would only risk the
    two answers drifting apart.
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
    in_scope |= {relative for relative, tree in parsed.items() if invokes_generic_wrapper(tree)}
    in_scope &= set(parsed)

    return parsed, in_scope, direct | (unparsable & direct), frozenset(tainted)


# --- symbol detection -------------------------------------------------------


def _docstring_ids(tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
                if isinstance(node.body[0].value.value, str):
                    ids.add(id(node.body[0].value))
    return ids


# Tier 3. Argument names that hold a ticker, and the order tools whose
# call-sites make that position meaningful.
_SYMBOL_ARGUMENT_NAMES = frozenset({"symbol", "symbols", "ticker", "tickers"})
_ORDER_TOOL_NAMES = frozenset(tool.lower() for tool in ORDER_TOOLS)


def _callee_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return ""


def _is_order_call(call: ast.Call, tainted: frozenset[str]) -> bool:
    """True if this call reaches the connector's order path.

    Three routes, mirroring the three ways a module gets into scope: the callee
    IS an order tool (`connector.place_equity_order(...)`); the callee is a
    generic dispatch wrapper (`connector._call(...)`); or an argument carries an
    order-tool name, either as a literal / folded `+` concatenation or as a
    constant tainted elsewhere in the tree (`connector.route(ORDER, {...})`).
    """
    name = _callee_name(call).lower()
    if name in _ORDER_TOOL_NAMES or name in _WRAPPER_METHODS:
        return True
    for argument in [*call.args, *(keyword.value for keyword in call.keywords)]:
        if any(literal.strip().lower() in _ORDER_TOOL_NAMES for literal in _string_constants(argument)):
            return True
        if _referenced_names(argument) & tainted:
            return True
    return False


def _symbol_position_constants(call: ast.Call) -> list[ast.Constant]:
    """String constants sitting in a SYMBOL position of one call.

    Both spellings the connector's payloads actually take: a keyword argument
    (`symbol="gevity"`) and a payload-dict entry (`{"symbol": "gevity"}`) at any
    depth of an argument. A list or tuple in either position is unpacked, so
    `symbols=["gevity"]` is seen too.
    """
    found: list[ast.Constant] = []

    def collect(value: ast.AST) -> None:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            found.append(value)
        elif isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            for element in value.elts:
                collect(element)

    arguments = [*call.args, *(keyword.value for keyword in call.keywords)]
    for keyword in call.keywords:
        if keyword.arg and keyword.arg.strip().lower() in _SYMBOL_ARGUMENT_NAMES:
            collect(keyword.value)
    for argument in arguments:
        for node in ast.walk(argument):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    if key.value.strip().lower() in _SYMBOL_ARGUMENT_NAMES:
                        collect(value)
    return found


def order_call_symbol_constants(tree: ast.AST, tainted: frozenset[str] = frozenset()) -> set[int]:
    """ids of every string constant in a symbol position at an order call-site."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_order_call(node, tainted):
            ids.update(id(constant) for constant in _symbol_position_constants(node))
    return ids


def hardcoded_symbols(
    module: str,
    tree: ast.Module,
    vocabulary: frozenset[str],
    tainted: frozenset[str] = frozenset(),
) -> list[Violation]:
    """Ticker literals inside a module, case-insensitively.

    Docstrings are skipped -- a docstring cannot place an order -- but every
    other string constant is examined, including dict keys, list items, default
    arguments and the literal parts of f-strings.

    Exactly one tier claims each token, most authoritative first, so a literal
    is never reported twice: config/curated vocabulary, then the order call-site
    POSITION (case-insensitive, no stop-word excuse), then bare upper-case shape.
    """
    skip = _docstring_ids(tree)
    at_order_call_site = order_call_symbol_constants(tree, tainted)
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        in_symbol_position = id(node) in at_order_call_site
        for part in re.split(r"[,\s;|]+", node.value.strip()):
            token = part.strip("\"'`()[]{}").rstrip(".")
            if not token:
                continue
            if _CONFIG_TICKER_SHAPE.match(token) and _vocab_key(token) in vocabulary:
                tier = "known ticker"
            elif in_symbol_position and _CONFIG_TICKER_SHAPE.match(token):
                tier = "order call-site symbol"
            elif _TICKER_SHAPE.match(token) and token.upper() not in NON_TICKER_WORDS:
                tier = "ticker-shaped literal"
            else:
                continue
            violations.append(Violation(module, token, node.lineno, tier))
    return violations


def scan_tree(root: Path, config_path: Path = TRADING_RULES) -> ScanReport:
    """Scan a source tree for hardcoded order symbols in execution modules."""
    parsed, in_scope, _, tainted = discover_execution_modules(root)
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
        violations.extend(hardcoded_symbols(relative, parsed[relative], vocabulary, tainted))
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

# The three shapes a real ticker used to slip past `^[A-Z]{1,5}$`: a lowercase
# spelling, a six-letter symbol, and a class-share dot.
EVASIVE_TICKER_SHAPES = '''
def submit(connector):
    connector.place_equity_order(symbol="rblx")
    connector.place_equity_order(symbol="GEVITY")
    connector.place_equity_order(symbol="BRK.B")
'''

# A tool name split across two literals so `\\bplace_equity_order\\b` never
# appears in the raw source; the module dispatches through a plain method (not a
# wrapper) and its filename carries no equity hint, so ONLY constant folding of
# the `+` can taint it and pull it into scope.
SPLIT_LITERAL_TOOL_NAME = '''
ORDER = "place_equity" + "_order"


def submit(connector):
    return connector.route(ORDER, {"symbol": "brk.b", "quantity": 1})
'''

# A module that never names an order tool, is not tainted, and has no equity
# filename hint -- it is in scope solely because it routes through a generic
# `_call` wrapper.
GENERIC_WRAPPER_INVOCATION = '''
def submit(connector, tool):
    return connector._call(tool, {"symbol": "GEVITY", "quantity": 1})
'''

# THE RESIDUAL RE-AUDIT FINDING. A lowercase, OFF-vocabulary ticker in a
# `symbol=` slot: tier 1 misses it (not curated, not in config) and tier 2
# misses it (upper-case only). Only tier 3's positional reading catches it.
LOWERCASE_OFF_UNIVERSE_CALL_SITE = '''
def submit(connector, quantity):
    return connector.place_equity_order(symbol="gevity", side="buy", quantity=quantity)
'''

# The same evasion routed through a constant dispatch and a payload dict, so
# the positional tier is proved on the shape src/rh_lane.py-style code takes,
# not just on a direct call.
LOWERCASE_OFF_UNIVERSE_DISPATCH = '''
_ORDER_TOOL = "place_equity_order"


def submit(connector):
    return connector._call(_ORDER_TOOL, {"symbol": "gevity", "quantity": 1})
'''

# The precision counterpart to tier 3: an in-scope execution module full of
# short LOWERCASE tokens, none of which sits in a symbol position. If tier 3
# were widened from "a symbol argument" to "any lowercase ticker-shaped
# literal", every one of these would be flagged and the guard would be
# allowlisted away within a week.
LOWERCASE_PROSE_IN_AN_EXECUTION_MODULE = '''
ORDER_TOOL = "place_equity_order"
MODES = ["paper", "live", "both"]


def submit(connector, symbol, mode):
    if mode not in MODES:
        return {"status": "unknown mode", "note": "must be one of them"}
    return connector._call(ORDER_TOOL, {"symbol": symbol, "mode": mode})
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


def test_evasive_ticker_shapes_are_flagged(tmp_path: Path) -> None:
    """The three shapes that evaded `^[A-Z]{1,5}$`: a lowercase ticker, a
    six-letter ticker, and a class-share dot. BRK.B normalizes to the curated
    'BRKB' (tier 1); 'rblx' and 'GEVITY' are caught by the broker-grammar shape
    (tier 2). Reverting the matcher to the all-caps 1-5 form fails this."""
    report = scan_tree(write_tree(tmp_path, {"equity_exec.py": EVASIVE_TICKER_SHAPES}))

    assert "equity_exec.py" in report.scanned
    assert report.symbols() == {"RBLX", "GEVITY", "BRK.B"}


def test_a_split_literal_tool_name_taints_the_module(tmp_path: Path) -> None:
    """`"place_equity" + "_order"` forms an order-tool name no substring search
    sees. Only folding the concatenation taints ORDER and pulls this module --
    which dispatches through a plain `.route`, not a wrapper -- into scope."""
    report = scan_tree(write_tree(tmp_path, {"router_x.py": SPLIT_LITERAL_TOOL_NAME}))

    assert "router_x.py" in report.scanned
    assert report.symbols() == {"BRK.B"}


def test_a_generic_call_wrapper_pulls_a_module_into_scope(tmp_path: Path) -> None:
    """No order-tool name, no taint, no filename hint -- in scope solely because
    it routes a call through a generic `_call` wrapper. Reverting the wrapper
    rule drops it from the scan and 'GEVITY' goes unflagged."""
    report = scan_tree(write_tree(tmp_path, {"generic.py": GENERIC_WRAPPER_INVOCATION}))

    assert "generic.py" in report.scanned
    assert report.symbols() == {"GEVITY"}


def test_a_lowercase_off_universe_ticker_at_an_order_call_site_is_flagged(tmp_path: Path) -> None:
    """MUTATION TEST for the residual re-audit finding. 'gevity' is lowercase
    AND outside every vocabulary, so tier 1 cannot see it and tier 2's
    upper-case anchor excuses it -- it used to reach the broker, which up-cased
    it into a well-formed live order. Delete the positional tier (or narrow
    _CONFIG_TICKER_SHAPE back to the upper-case _TICKER_SHAPE here) and this
    module scans clean again."""
    report = scan_tree(write_tree(tmp_path, {"lane_exec.py": LOWERCASE_OFF_UNIVERSE_CALL_SITE}))

    assert "lane_exec.py" in report.scanned
    assert report.symbols() == {"GEVITY"}
    assert [violation.tier for violation in report.violations] == ["order call-site symbol"]
    # The literal is reported as it was WRITTEN, so the diff line is findable.
    assert report.violations[0].symbol == "gevity"


@pytest.mark.parametrize("tool", ORDER_TOOLS)
def test_the_positional_tier_covers_every_order_tool(tmp_path: Path, tool: str) -> None:
    """place / review / cancel alike -- a preview of an unapproved ticker and a
    cancel aimed at one are order call-sites too."""
    source = f'def submit(connector):\n    return connector.{tool}(symbol="gevity")\n'
    report = scan_tree(write_tree(tmp_path, {"lane_exec.py": source}))

    assert report.symbols() == {"GEVITY"}


def test_a_lowercase_ticker_in_a_dispatched_payload_dict_is_flagged(tmp_path: Path) -> None:
    """The same evasion through a constant dispatch: no ticker-looking callee,
    the symbol buried in a payload dict, and lowercase. The taint set returned
    by discover_execution_modules is what makes this call-site recognisable."""
    report = scan_tree(write_tree(tmp_path, {"rh_lane.py": LOWERCASE_OFF_UNIVERSE_DISPATCH}))

    assert "rh_lane.py" in report.scanned
    assert report.symbols() == {"GEVITY"}


def test_lowercase_prose_outside_a_symbol_position_is_not_flagged(tmp_path: Path) -> None:
    """Tier 3 reads POSITION, not case. An in-scope module whose lowercase
    short words are modes, statuses and prose stays clean -- widening the tier
    to any lowercase ticker-shaped literal would flag all of them."""
    report = scan_tree(write_tree(tmp_path, {"equity_exec.py": LOWERCASE_PROSE_IN_AN_EXECUTION_MODULE}))

    assert "equity_exec.py" in report.scanned
    assert report.violations == []


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


def test_an_indicator_name_stop_word_is_still_caught_when_config_lists_it(tmp_path: Path) -> None:
    """RSI is excused as a tier-2 shape match because the equities strategy
    labels its rationale with SMA/EMA/RSI/MACD -- but RSI is also a real
    ticker (Rush Street Interactive). The moment config says this lane may
    trade it, tier 1 catches the literal anyway. Deleting the tier-1 check
    ahead of the stop-word check is what this pins."""
    config = tmp_path / "trading_rules.yaml"
    config.write_text("equities:\n  universe:\n  - RSI\n", encoding="utf-8")
    tree = write_tree(tmp_path / "src", {"lane.py": 'def go(c):\n    return c.place_equity_order(symbol="RSI")\n'})

    assert "RSI" in NON_TICKER_WORDS
    assert scan_tree(tree, config_path=config).symbols() == {"RSI"}


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
