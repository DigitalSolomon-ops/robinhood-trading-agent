"""The analysis-only boundary, enforced in code: sector_scout has an
authenticated-broker-adjacent data path now, so the no-order rule stops being
policy and becomes a failing test.

Walks the AST of every module in src/sector_scout/ and fails on:
  * an import of any broker or order module (robinhood_option_broker,
    robinhood_equity_broker, order_manager, live_broker, paper_broker,
    strategy_engine, option_trader, robinhood_crypto_client, and the
    connector-backed trading clients themselves)
  * any attribute access or bare name matching place_*, review_*, preview_*,
    cancel_*, exercise_* -- under any condition, including retries and
    fallbacks, in any expression position.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "sector_scout"

FORBIDDEN_MODULES = {
    "robinhood_option_broker",
    "robinhood_equity_broker",
    "robinhood_equity_client",
    "robinhood_option_client",
    "robinhood_crypto_client",
    "order_manager",
    "live_broker",
    "paper_broker",
    "strategy_engine",
    "option_trader",
    "option_strategy",
    "option_runtime",
    "equity_runtime",
    "portfolio",
    "risk_manager",
}

FORBIDDEN_CALL = re.compile(r"^(place|review|preview|cancel|exercise)_", re.IGNORECASE)


def _module_tail(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _module_tail(alias.name) in FORBIDDEN_MODULES:
                    out.append(f"{path.name}:{node.lineno} imports {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _module_tail(module) in FORBIDDEN_MODULES:
                out.append(f"{path.name}:{node.lineno} imports from {module}")
            for alias in node.names:
                if _module_tail(alias.name) in FORBIDDEN_MODULES:
                    out.append(f"{path.name}:{node.lineno} imports {alias.name} from {module}")
                if FORBIDDEN_CALL.match(alias.name):
                    out.append(f"{path.name}:{node.lineno} imports order verb {alias.name}")
        elif isinstance(node, ast.Attribute):
            if FORBIDDEN_CALL.match(node.attr):
                out.append(f"{path.name}:{node.lineno} attribute access .{node.attr}")
        elif isinstance(node, ast.Name):
            if FORBIDDEN_CALL.match(node.id):
                out.append(f"{path.name}:{node.lineno} name {node.id}")
    return out


def test_no_broker_import_and_no_order_verbs_anywhere() -> None:
    assert PACKAGE.is_dir(), PACKAGE
    hits: list[str] = []
    for path in sorted(PACKAGE.glob("*.py")):
        hits.extend(_violations(path))
    assert not hits, "analysis-only boundary violated:\n" + "\n".join(hits)


def test_guard_actually_detects_a_violation(tmp_path: Path) -> None:
    """The guard must not be a tautology: prove it fires on each class of
    violation it claims to catch."""
    bad_import = tmp_path / "bad_import.py"
    bad_import.write_text("from ..robinhood_option_broker import anything\n", encoding="utf-8")
    assert _violations(bad_import)

    bad_client = tmp_path / "bad_client.py"
    bad_client.write_text("from src.robinhood_equity_client import RobinhoodEquityClient\n", encoding="utf-8")
    assert _violations(bad_client)

    bad_attr = tmp_path / "bad_attr.py"
    bad_attr.write_text("def f(c):\n    return c.place_option_order(x=1)\n", encoding="utf-8")
    assert _violations(bad_attr)

    bad_fallback = tmp_path / "bad_fallback.py"
    bad_fallback.write_text(
        "def f(c):\n    try:\n        return 1\n    except Exception:\n        return c.cancel_order(1)\n",
        encoding="utf-8",
    )
    assert _violations(bad_fallback)

    clean = tmp_path / "clean.py"
    clean.write_text("def f(c):\n    return c.get_option_quotes(['id'])\n", encoding="utf-8")
    assert not _violations(clean)
