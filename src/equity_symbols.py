from __future__ import annotations

from typing import Any

from .logger import SQLiteLogger
from .robinhood_equity_client import RobinhoodEquityClient

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


def _quote_is_active(row: dict[str, Any]) -> bool:
    state = str(row.get("state") or row.get("tradability") or row.get("status") or "active").lower()
    if state in _INACTIVE_STATES:
        return False
    price = row.get("price") or row.get("last_trade_price") or row.get("ask_price")
    try:
        return float(price) > 0
    except (TypeError, ValueError):
        return False


def validate_equity_symbols(
    client: RobinhoodEquityClient,
    rules: dict[str, Any],
    logger: SQLiteLogger | None = None,
) -> dict[str, Any]:
    """Confirm every equities.universe ticker is tradable via the connector.

    Read-only: the only connector call is get_equity_quotes (client.get_quotes),
    never an order path. A ticker counts as available only when the connector
    returns a quote for it AND that quote is not flagged in an inactive state
    AND it carries a positive price -- a missing symbol, a halted/delisted
    state, or a zero/blank price all land it in `unavailable` instead.
    """
    symbols = equities_universe(rules)
    available: list[str] = []
    unavailable: list[str] = []
    details: dict[str, Any] = {}

    rows_by_symbol: dict[str, dict[str, Any]] = {}
    if symbols:
        payload = client.get_quotes(*symbols)
        for row in _quote_rows(payload):
            row_symbol = row.get("symbol")
            if row_symbol:
                rows_by_symbol[str(row_symbol).upper()] = row

    for symbol in symbols:
        row = rows_by_symbol.get(symbol)
        if row is None:
            unavailable.append(symbol)
            details[symbol] = {"active": False, "reason": "connector returned no quote"}
            continue
        active = _quote_is_active(row)
        (available if active else unavailable).append(symbol)
        details[symbol] = {"active": active, "quote": row}

    result = {
        "universe": symbols,
        "available": available,
        "unavailable": unavailable,
        "details": details,
    }
    if logger is not None:
        reason = f"equities symbol validation: {len(available)}/{len(symbols)} tickers returned an active quote"
        logger.log_decision(None, "equity_symbols_validated", reason, result)
    return result
