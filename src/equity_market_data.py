from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .equity_symbols import _quote_is_active
from .logger import SQLiteLogger
from .robinhood_equity_client import RobinhoodEquityClient

# Kept in its own db file (data/equity_market_data.db) so a proving run's
# equities quote history never mixes with the crypto lane's market_data.db.
_QUOTE_PRICE_KEYS = ("price", "last_trade_price", "ask_price", "mark_price")

# The name this service records its prices under. It is the EXECUTION price
# source -- the connector's own quote, read live -- and is deliberately NOT the
# source a proving run may be counted on (see src/equity_intelligence/
# massive_history.py: a proving run is priced from real Massive daily bars, and
# src/equity_readiness.py asserts that).
CONNECTOR_QUOTE_SOURCE = "robinhood_equity_quote"


def _quote_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows = payload.get("quotes", payload.get("results", []))
        return rows or []
    if isinstance(payload, list):
        return payload
    return []


def _quote_price(row: dict[str, Any]) -> float | None:
    for key in _QUOTE_PRICE_KEYS:
        if row.get(key) not in (None, ""):
            try:
                return float(row[key])
            except (TypeError, ValueError):
                continue
    return None


class EquityMarketDataService:
    """Regular-hours quote history for the equities lane, read over the
    connector's get_equity_quotes -- there is no HTTP client to poll instead.

    Mirrors MarketDataService's shape (save/recent_history/history_count) so
    the equities strategy profile is fed the same way the crypto lane's is,
    without touching market_data.db or RobinhoodCryptoClient.
    """

    #: What a price read through this service records itself as. A quote source
    #: swapped in for a proving run (the Massive history feed) carries its own.
    quote_source_name = CONNECTOR_QUOTE_SOURCE

    def __init__(self, client: RobinhoodEquityClient, db_path: Path | str = "data/equity_market_data.db") -> None:
        self.client = client
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS equity_candles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    price REAL NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_equity_candles_symbol_timestamp ON equity_candles(symbol, timestamp)")

    @staticmethod
    def now() -> str:
        return datetime.now(UTC).isoformat()

    def save_price(self, symbol: str, price: float, source: str = CONNECTOR_QUOTE_SOURCE) -> None:
        now = self.now()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO equity_candles(timestamp, symbol, price, source, created_at) VALUES (?, ?, ?, ?, ?)",
                (now, symbol, price, source, now),
            )

    def save_prices(self, prices: dict[str, float], source: str) -> None:
        """Persist a batch of prices that came from somewhere OTHER than this
        service's own connector read -- a proving run's real Massive bars, say --
        under the name of the source they actually came from, so the candle
        ledger can never claim a connector quote it never made."""
        for symbol, price in prices.items():
            self.save_price(symbol, price, source=source)

    def sources(self) -> dict[str, int]:
        """Row count per recorded price source, for evidence and the dashboard."""
        with self.connect() as conn:
            rows = conn.execute("SELECT source, COUNT(*) FROM equity_candles GROUP BY source").fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def read_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        """The cycle's ONE connector quote read, returned as raw rows.

        Split out from get_latest_prices so run_equity_cycle can hand the same
        rows to the tradability gate (src/equity_symbols.py) and to the price
        ledger below without polling the connector twice. Read-only: the only
        connector call here is get_equity_quotes.
        """
        if not symbols:
            return []
        return [row for row in _quote_rows(self.client.get_quotes(*symbols)) if isinstance(row, dict)]

    def prices_from_rows(
        self,
        rows: list[dict[str, Any]],
        logger: SQLiteLogger | None = None,
        allowed: list[str] | None = None,
    ) -> dict[str, float]:
        """Persist and return a price per quoted, active, allowed symbol.

        A halted/delisted symbol -- or one whose quote carries no positive
        price -- is skipped before it can reach the trade path, so the
        (formerly dead) validate_equity_symbols._quote_is_active gate runs on
        every cycle's live quotes rather than only in an unused pre-flight.
        Each skip writes its own rationale when a logger is supplied, so a
        symbol dropped for being inactive is auditable, not silent.

        `allowed` is the tradability gate's surviving symbol list. Anything
        outside it is dropped SILENTLY here, because the gate has already
        written that symbol's rationale -- a second row would say the same
        thing twice. Left None, every quoted symbol is considered.
        """
        allowed_symbols = {str(symbol).upper() for symbol in allowed} if allowed is not None else None
        prices: dict[str, float] = {}
        for row in rows:
            symbol = row.get("symbol")
            if not symbol:
                continue
            symbol = str(symbol).upper()
            if allowed_symbols is not None and symbol not in allowed_symbols:
                continue
            if not _quote_is_active(row):
                if logger is not None:
                    logger.log_decision(
                        symbol,
                        "equity_symbol_unavailable",
                        f"{symbol} skipped this cycle: quote is halted/delisted/suspended "
                        f"or carries no positive price; strategy not evaluated",
                        {"quote": row},
                    )
                continue
            price = _quote_price(row)
            if price:
                prices[symbol] = price
                self.save_price(symbol, price)
        return prices

    def get_latest_prices(
        self, symbols: list[str], logger: SQLiteLogger | None = None
    ) -> dict[str, float]:
        """One connector quote read, filtered and persisted. Read-only."""
        if not symbols:
            return {}
        return self.prices_from_rows(self.read_quotes(symbols), logger=logger)

    def get_prices(self, symbols: list[str], logger: SQLiteLogger | None = None) -> dict[str, float]:
        """The quote-source protocol name, so run_equity_cycle can hold either
        this service or a replayed Massive history feed without knowing which."""
        return self.get_latest_prices(list(symbols), logger=logger)

    def recent_history(self, symbol: str, limit: int = 250) -> list[float]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT price FROM (
                    SELECT timestamp, price FROM equity_candles
                    WHERE symbol = ?
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                )
                ORDER BY timestamp ASC
                """,
                (symbol, limit),
            ).fetchall()
        return [float(row[0]) for row in rows]

    def history_count(self, symbol: str) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM equity_candles WHERE symbol = ?", (symbol,)).fetchone()
        return int(row[0] or 0)

    def total_rows(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM equity_candles").fetchone()
        return int(row[0] or 0)
