from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class IntelligenceStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @staticmethod
    def now() -> str:
        return datetime.now(UTC).isoformat()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS news_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT,
                    provider TEXT,
                    title TEXT,
                    url TEXT,
                    sentiment TEXT,
                    sentiment_value REAL,
                    important INTEGER DEFAULT 0,
                    details TEXT
                );
                CREATE TABLE IF NOT EXISTS coin_sentiment (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    score REAL NOT NULL,
                    label TEXT NOT NULL,
                    event_count INTEGER NOT NULL,
                    details TEXT
                );
                CREATE TABLE IF NOT EXISTS macro_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    provider TEXT,
                    series_id TEXT NOT NULL,
                    value REAL,
                    previous_value REAL,
                    direction TEXT,
                    details TEXT
                );
                CREATE TABLE IF NOT EXISTS market_context (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    provider TEXT,
                    metric TEXT NOT NULL,
                    value REAL,
                    details TEXT
                );
                CREATE TABLE IF NOT EXISTS intelligence_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    micro_signal TEXT,
                    micro_score REAL,
                    news_sentiment_score REAL,
                    macro_risk_score REAL,
                    market_context_score REAL,
                    combined_intelligence_score REAL,
                    recommendation TEXT,
                    reasons TEXT,
                    missing_data TEXT,
                    details TEXT
                );
                CREATE TABLE IF NOT EXISTS intelligence_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    provider TEXT,
                    context TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details TEXT
                );
                """
            )

    def insert(self, table: str, row: dict[str, Any]) -> None:
        with self.connect() as conn:
            keys = list(row)
            conn.execute(
                f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                [row[key] for key in keys],
            )

    def log_error(self, provider: str, context: str, message: str, details: dict[str, Any] | None = None) -> None:
        self.insert(
            "intelligence_errors",
            {
                "timestamp": self.now(),
                "provider": provider,
                "context": context,
                "message": message,
                "details": json.dumps(details or {}),
            },
        )

    def save_news_event(self, event: dict[str, Any]) -> None:
        self.insert(
            "news_events",
            {
                "timestamp": event.get("timestamp") or self.now(),
                "symbol": event.get("symbol"),
                "provider": event.get("provider"),
                "title": event.get("title"),
                "url": event.get("url"),
                "sentiment": event.get("sentiment"),
                "sentiment_value": float(event.get("sentiment_value", 0) or 0),
                "important": 1 if event.get("important") else 0,
                "details": json.dumps(event.get("details", {})),
            },
        )

    def save_coin_sentiment(self, symbol: str, score: float, label: str, event_count: int, details: dict[str, Any]) -> None:
        self.insert(
            "coin_sentiment",
            {
                "timestamp": self.now(),
                "symbol": symbol,
                "score": score,
                "label": label,
                "event_count": event_count,
                "details": json.dumps(details),
            },
        )

    def save_macro_observation(self, observation: dict[str, Any]) -> None:
        self.insert(
            "macro_observations",
            {
                "timestamp": self.now(),
                "provider": observation.get("provider"),
                "series_id": observation.get("series_id"),
                "value": observation.get("value"),
                "previous_value": observation.get("previous_value"),
                "direction": observation.get("direction"),
                "details": json.dumps(observation.get("details", {})),
            },
        )

    def save_market_context(self, provider: str, metric: str, value: float | None, details: dict[str, Any]) -> None:
        self.insert(
            "market_context",
            {
                "timestamp": self.now(),
                "provider": provider,
                "metric": metric,
                "value": value,
                "details": json.dumps(details),
            },
        )

    def save_score(self, score: dict[str, Any]) -> None:
        self.insert(
            "intelligence_scores",
            {
                "timestamp": score.get("timestamp") or self.now(),
                "symbol": score["symbol"],
                "micro_signal": score.get("micro_signal"),
                "micro_score": score.get("micro_score"),
                "news_sentiment_score": score.get("news_sentiment_score"),
                "macro_risk_score": score.get("macro_risk_score"),
                "market_context_score": score.get("market_context_score"),
                "combined_intelligence_score": score.get("combined_intelligence_score"),
                "recommendation": score.get("recommendation"),
                "reasons": json.dumps(score.get("reasons", [])),
                "missing_data": json.dumps(score.get("missing_data", [])),
                "details": json.dumps(score),
            },
        )

    def latest_rows(self, table: str, limit: int = 25) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def latest_score(self, symbol: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT details FROM intelligence_scores WHERE symbol = ? ORDER BY id DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["details"])
        except json.JSONDecodeError:
            return None

    def latest_coin_sentiment(self, symbol: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM coin_sentiment WHERE symbol = ? ORDER BY id DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        return dict(row) if row else None

    def latest_market_context(self) -> dict[str, Any]:
        rows = self.latest_rows("market_context", 25)
        return {str(row["metric"]): row for row in rows}

    def latest_macro_observations(self) -> list[dict[str, Any]]:
        return self.latest_rows("macro_observations", 25)

    def latest_errors(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.latest_rows("intelligence_errors", limit)
