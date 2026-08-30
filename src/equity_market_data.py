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

    def save_price(self, symbol: str, price: float, source: str = "robinhood_equity_quote") -> None:
        now = self.now()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO equity_candles(timestamp, symbol, price, source, created_at) VALUES (?, ?, ?, ?, ?)",
                (now, symbol, price, source, now),
            )

    def get_latest_prices(
        self, symbols: list[str], logger: SQLiteLogger | None = None
    ) -> dict[str, float]:
        """Read-only: the only connector call here is get_equity_quotes.

        A halted/delisted symbol -- or one whose quote carries no positive
        price -- is skipped before it can reach the trade path, so the
        (formerly dead) validate_equity_symbols._quote_is_active gate now runs
        on every cycle's live quotes rather than only in an unused pre-flight.
        Each skip writes its own rationale when a logger is supplied, so a
        symbol dropped for being inactive is auditable, not silent.
        """
        if not symbols:
            return {}
        payload = self.client.get_quotes(*symbols)
        prices: dict[str, float] = {}
        for row in _quote_rows(payload):
            symbol = row.get("symbol")
            if not symbol:
                continue
            symbol = str(symbol).upper()
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
