from __future__ import annotations

from typing import Any

from .equity_intelligence.liquidity import (
    LiquidityProvider,
    LiquiditySnapshot,
    evaluate_liquidity,
    liquidity_config,
)
from .logger import SQLiteLogger
from .robinhood_equity_client import RobinhoodEquityClient

VENUE = "robinhood_equities"

# States the connector's quote payload uses to mark a symbol as not currently
# tradable. Anything else (or a missing state field) is read as active, then
# double-checked against a positive last price below.
_INACTIVE_STATES = {"halted", "delisted", "inactive", "closed", "suspended"}


def equities_universe(rules: dict[str, Any]) -> list[str]:
    """The equities lane's own symbol list -- config/trading_rules.yaml's
    equities.universe, never trading.allowed_symbols or symbols.* (those are
    the crypto lane's)."""
    configured = rules.get("equities", {}).get("universe", [])
    return [str(symbol).upper() for symbol in configured]


def _quote_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows = payload.get("quotes", payload.get("results", []))
        return rows or []
    if isinstance(payload, list):
        return payload
    return []


def _quote_state(row: dict[str, Any]) -> str:
    return str(row.get("state") or row.get("tradability") or row.get("status") or "active").lower()


def _quote_is_active(row: dict[str, Any]) -> bool:
    if _quote_state(row) in _INACTIVE_STATES:
        return False
    price = row.get("price") or row.get("last_trade_price") or row.get("ask_price")
    try:
        return float(price) > 0
    except (TypeError, ValueError):
        return False


def _quote_block_reason(row: dict[str, Any]) -> str | None:
    """Why this connector quote disqualifies its symbol, or None if it does not.

    The readable half of the halted/delisted check: the gate's rationale names
    the state the connector actually reported instead of asserting a verdict.
    """
    state = _quote_state(row)
    if state in _INACTIVE_STATES:
        return f"connector quote state is {state!r}, which is not tradable"
    price = row.get("price") or row.get("last_trade_price") or row.get("ask_price")
    try:
        if float(price) > 0:
            return None
    except (TypeError, ValueError):
        pass
    return f"connector quote carries no positive price (got {price!r})"


def validate_equity_symbols(
    client: RobinhoodEquityClient,
    rules: dict[str, Any],
    logger: SQLiteLogger | None = None,
    quote_rows: list[dict[str, Any]] | None = None,
    liquidity_provider: LiquidityProvider | None = None,
) -> dict[str, Any]:
    """Confirm every equities.universe ticker is tradable, in two halves.

    Read-only in both halves: the only connector call is get_equity_quotes
    (client.get_quotes), never an order path, and the liquidity half reads
    end-of-day daily bars through the read-only Massive client.

    Half one, the CONNECTOR QUOTE. A ticker passes only when the connector
    returns a quote for it AND that quote is not flagged in an inactive state
    AND it carries a positive price -- a missing symbol, a halted/delisted
    state, or a zero/blank price all land it in `unavailable` instead.

    Half two, LIQUIDITY (src/equity_intelligence/liquidity.py, config section
    `equities.liquidity:`). A ticker that is quoted but has stale, thin or
    absent recent daily bars is not one this lane should be evaluating either.
    Left disabled, or with no provider passed, this half is a no-op and the
    result is exactly the connector-quote check on its own.

    `quote_rows` lets a caller that has ALREADY read this cycle's quotes hand
    them straight in, so wiring this gate into run_equity_cycle costs no extra
    connector call. Passing an empty list means "the read was attempted and
    returned nothing", which fails every symbol closed rather than silently
    passing them.

    Every symbol gets an entry in `rationales`: a single human-readable
    sentence naming what was checked and what it said. Nothing here places,
    previews or cancels anything -- it only ever REMOVES a name from the list
    the risk gates are then asked about.
    """
    symbols = equities_universe(rules)
    config = liquidity_config(rules)
    available: list[str] = []
    unavailable: list[str] = []
    details: dict[str, Any] = {}
    rationales: dict[str, str] = {}

    rows_by_symbol: dict[str, dict[str, Any]] = {}
    payload_rows = quote_rows if quote_rows is not None else (_quote_rows(client.get_quotes(*symbols)) if symbols else [])
    for row in payload_rows:
        if not isinstance(row, dict):
            continue
        row_symbol = row.get("symbol")
        if row_symbol:
            rows_by_symbol[str(row_symbol).upper()] = row

    for symbol in symbols:
        row = rows_by_symbol.get(symbol)
        if row is None:
            unavailable.append(symbol)
            details[symbol] = {"active": False, "reason": "connector returned no quote"}
            rationales[symbol] = (
                f"{symbol} is not tradable this cycle: no quote available from the connector; "
                f"strategy not evaluated"
            )
            continue

        blocked = _quote_block_reason(row)
        if blocked is not None:
            unavailable.append(symbol)
            details[symbol] = {"active": False, "reason": blocked, "quote": row}
            rationales[symbol] = f"{symbol} is not tradable this cycle: {blocked}; strategy not evaluated"
            continue

        snapshot: LiquiditySnapshot | None = None
        if liquidity_provider is not None:
            try:
                snapshot = liquidity_provider.snapshot(symbol)
            except Exception as exc:  # noqa: BLE001 -- degrade, never crash
                snapshot = LiquiditySnapshot(symbol=symbol, error=f"{type(exc).__name__}: {exc}")
        verdict = evaluate_liquidity(snapshot, config)

        entry: dict[str, Any] = {"active": True, "quote": row}
        if config.get("enabled", False) or snapshot is not None:
            entry["liquidity"] = {
                "tradable": verdict.tradable,
                "notes": list(verdict.notes),
                "values": {name: value for name, value in (snapshot.values() if snapshot else ())},
            }
        if verdict.tradable:
            available.append(symbol)
            details[symbol] = entry
            rationales[symbol] = (
                f"{symbol} is tradable this cycle: connector quote is active; {verdict.rationale()}"
            )
            continue
        unavailable.append(symbol)
        entry["active"] = False
        entry["reason"] = "; ".join(verdict.notes) or "liquidity screen refused the symbol"
        details[symbol] = entry
        rationales[symbol] = (
            f"{symbol} is not tradable this cycle: {verdict.rationale()}; strategy not evaluated"
        )

    result = {
        "universe": symbols,
        "available": available,
        "unavailable": unavailable,
        "details": details,
        "rationales": rationales,
    }
    if logger is not None:
        reason = f"equities symbol validation: {len(available)}/{len(symbols)} tickers returned an active quote"
        logger.log_decision(None, "equity_symbols_validated", reason, result)
    return result


def equity_tradability(
    client: RobinhoodEquityClient,
    rules: dict[str, Any],
    logger: SQLiteLogger | None = None,
    quote_rows: list[dict[str, Any]] | None = None,
    liquidity_provider: LiquidityProvider | None = None,
) -> dict[str, Any]:
    """The lane's LIVE tradability gate: validate, then write the rationales.

    This is the function run_equity_cycle calls every cycle. It is deliberately
    thin -- validate_equity_symbols does the deciding -- because its whole job
    is the audit half of the contract: every symbol the gate removes writes a
    readable reason it was removed, and the cycle-level summary says how many
    survived.

    It cannot place, preview or cancel anything. The list it returns is the
    list of symbols the EXISTING gates (RiskManager, OrderManager, the kill
    switch, the human confirm-flag) are then asked about; nothing here is a
    substitute for any of them, and a symbol this gate passes is not thereby
    approved for anything.
    """
    report = validate_equity_symbols(
        client, rules, quote_rows=quote_rows, liquidity_provider=liquidity_provider
    )
    if logger is not None:
        for symbol in report["unavailable"]:
            logger.log_decision(
                symbol,
                "equity_symbol_unavailable",
                report["rationales"][symbol],
                {"venue": VENUE, **report["details"].get(symbol, {})},
            )
        universe = report["universe"]
        logger.log_decision(
            None,
            "equity_symbols_validated",
            f"tradability gate: {len(report['available'])}/{len(universe)} universe ticker(s) are "
            f"quoted, active and liquid enough to evaluate"
            + (f"; skipped {', '.join(report['unavailable'])}" if report["unavailable"] else ""),
            {
                "venue": VENUE,
                "universe": universe,
                "available": report["available"],
                "unavailable": report["unavailable"],
                "rationales": report["rationales"],
            },
        )
    return report
