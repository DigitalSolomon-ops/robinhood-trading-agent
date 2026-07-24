from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .robinhood_crypto_client import RobinhoodCryptoClient


class MarketDataService:
    def __init__(self, client: RobinhoodCryptoClient | None = None, db_path: Path | str = "data/market_data.db") -> None:
        self.client = client
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._price_history: dict[str, list[float]] = {}
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS candles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    bid REAL,
                    ask REAL,
                    mid REAL NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_candles_symbol_timestamp ON candles(symbol, timestamp)")

    @staticmethod
    def _extract_results(payload: Any) -> list[dict]:
        if isinstance(payload, dict):
            results = payload.get("results")
            if isinstance(results, list):
                return results
            return [payload]
        if isinstance(payload, list):
            return payload
        return []

    @staticmethod
    def _quote_values(entry: dict) -> tuple[float | None, float | None, float | None]:
        bid = entry.get("bid") or entry.get("bid_inclusive_of_sell_spread") or entry.get("bid_price")
        ask = entry.get("ask") or entry.get("ask_inclusive_of_buy_spread") or entry.get("ask_price")
        if bid is not None and ask is not None:
            bid_float = float(bid)
            ask_float = float(ask)
            return bid_float, ask_float, (bid_float + ask_float) / 2
        for key in ("price", "mark_price"):
            if entry.get(key) is not None:
                mid = float(entry[key])
                return float(bid) if bid is not None else None, float(ask) if ask is not None else None, mid
        return None, None, None

    def save_quote(self, symbol: str, bid: float | None, ask: float | None, mid: float, source: str, timestamp: str | None = None) -> None:
        now = datetime.now(UTC).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO candles(timestamp, symbol, bid, ask, mid, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (timestamp or now, symbol, bid, ask, mid, source, now),
            )

    def recent_history(self, symbol: str, limit: int = 250) -> list[float]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT mid FROM (
                    SELECT timestamp, mid FROM candles
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
            row = conn.execute("SELECT COUNT(*) FROM candles WHERE symbol = ?", (symbol,)).fetchone()
        return int(row[0] or 0)

    def total_rows(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM candles").fetchone()
        return int(row[0] or 0)

    def get_latest_prices(self, symbols: list[str]) -> dict[str, float]:
        if not self.client or not self.client.has_credentials:
            return {}
        payload = self.client.get_best_bid_ask(*symbols)
        prices: dict[str, float] = {}
        for entry in self._extract_results(payload):
            symbol = entry.get("symbol")
            bid, ask, mid = self._quote_values(entry)
            if symbol and mid:
                timestamp = entry.get("timestamp")
                prices[symbol] = mid
                self.save_quote(symbol, bid, ask, mid, "robinhood_best_bid_ask", timestamp)
                self._price_history.setdefault(symbol, []).append(mid)
        return prices

    def history_for(self, symbol: str) -> list[float]:
        stored = self.recent_history(symbol)
        return stored or self._price_history.get(symbol, [])

    def seed_history(self, symbol: str, prices: list[float]) -> None:
        self._price_history[symbol] = list(prices)
