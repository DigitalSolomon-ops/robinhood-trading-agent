from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class SQLiteLogger:
    def __init__(self, db_path: Path | str = "data/trading_agent.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    @staticmethod
    def now() -> str:
        return datetime.now(UTC).isoformat()

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    details TEXT
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    client_order_id TEXT,
                    symbol TEXT,
                    side TEXT,
                    order_type TEXT,
                    quantity REAL,
                    limit_price REAL,
                    notional REAL,
                    status TEXT,
                    details TEXT
                );
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    average_price REAL,
                    pnl REAL,
                    details TEXT
                );
                CREATE TABLE IF NOT EXISTS risk_blocks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT,
                    side TEXT,
                    reason TEXT NOT NULL,
                    details TEXT
                );
                CREATE TABLE IF NOT EXISTS daily_summary (
                    date TEXT PRIMARY KEY,
                    realized_pnl REAL DEFAULT 0,
                    trade_count INTEGER DEFAULT 0,
                    blocked_count INTEGER DEFAULT 0,
                    details TEXT
                );
                CREATE TABLE IF NOT EXISTS errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    context TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details TEXT
                );
                """
            )

    def _insert(self, table: str, row: dict[str, Any]) -> None:
        with self.connect() as conn:
            keys = list(row)
            placeholders = ", ".join("?" for _ in keys)
            conn.execute(
                f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({placeholders})",
                [row[key] for key in keys],
            )

    def log_decision(self, symbol: str | None, action: str, reason: str, details: dict[str, Any] | None = None) -> None:
        self._insert("decisions", {"timestamp": self.now(), "symbol": symbol, "action": action, "reason": reason, "details": json.dumps(details or {})})

    def log_order(self, order: dict[str, Any]) -> None:
        row = {
            "timestamp": self.now(),
            "client_order_id": order.get("client_order_id"),
            "symbol": order.get("symbol"),
            "side": order.get("side"),
            "order_type": order.get("order_type") or order.get("type"),
            "quantity": order.get("quantity"),
            "limit_price": order.get("limit_price"),
            "notional": order.get("notional"),
            "status": order.get("status"),
            "details": json.dumps(order),
        }
        self._insert("orders", row)

    def log_risk_block(self, symbol: str | None, side: str | None, reason: str, details: dict[str, Any] | None = None) -> None:
        self._insert("risk_blocks", {"timestamp": self.now(), "symbol": symbol, "side": side, "reason": reason, "details": json.dumps(details or {})})
        today = datetime.now(UTC).date().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_summary(date, blocked_count) VALUES(?, 1)
                ON CONFLICT(date) DO UPDATE SET blocked_count = blocked_count + 1
                """,
                (today,),
            )

    def log_error(self, context: str, message: str, details: dict[str, Any] | None = None) -> None:
        self._insert("errors", {"timestamp": self.now(), "context": context, "message": message, "details": json.dumps(details or {})})

    def increment_trade_count(self, pnl: float = 0.0) -> None:
        today = datetime.now(UTC).date().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_summary(date, realized_pnl, trade_count) VALUES(?, ?, 1)
                ON CONFLICT(date) DO UPDATE SET
                    realized_pnl = realized_pnl + excluded.realized_pnl,
                    trade_count = trade_count + 1
                """,
                (today, pnl),
            )

    def get_daily_summary(self) -> dict[str, Any]:
        today = datetime.now(UTC).date().isoformat()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT realized_pnl, trade_count, blocked_count FROM daily_summary WHERE date = ?",
                (today,),
            ).fetchone()
        if not row:
            return {"realized_pnl": 0.0, "trade_count": 0, "blocked_count": 0}
        return {"realized_pnl": float(row[0] or 0), "trade_count": int(row[1] or 0), "blocked_count": int(row[2] or 0)}

    def reset_daily_summary(self) -> dict[str, Any]:
        today = datetime.now(UTC).date().isoformat()
        before = self.get_daily_summary()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_summary(date, realized_pnl, trade_count, blocked_count)
                VALUES(?, 0, 0, 0)
                ON CONFLICT(date) DO UPDATE SET
                    realized_pnl = 0,
                    trade_count = 0,
                    blocked_count = 0
                """,
                (today,),
            )
        return {"date": today, "before": before, "after": self.get_daily_summary()}

    def get_last_decision(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT timestamp, symbol, action, reason FROM decisions ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return {"timestamp": row[0], "symbol": row[1], "action": row[2], "reason": row[3]}

    def get_last_order(self, symbol: str | None = None) -> dict[str, Any] | None:
        sql = "SELECT timestamp, client_order_id, symbol, side, status, notional, details FROM orders"
        params: tuple[Any, ...] = ()
        if symbol:
            sql += " WHERE symbol = ?"
            params = (symbol,)
        sql += " ORDER BY id DESC LIMIT 1"
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        details: dict[str, Any] = {}
        if row[6]:
            try:
                details = json.loads(row[6])
            except json.JSONDecodeError:
                details = {}
        return {
            "timestamp": row[0],
            "client_order_id": row[1],
            "symbol": row[2],
            "side": row[3],
            "status": row[4],
            "notional": row[5],
            "details": details,
        }

    def recent_audit_rows(self, limit: int = 200) -> dict[str, list[dict[str, Any]]]:
        output: dict[str, list[dict[str, Any]]] = {}
        queries = {
            "decisions": "SELECT timestamp, symbol, action, reason, details FROM decisions ORDER BY id DESC LIMIT ?",
            "orders": "SELECT timestamp, client_order_id, symbol, side, order_type, quantity, limit_price, notional, status, details FROM orders ORDER BY id DESC LIMIT ?",
            "risk_blocks": "SELECT timestamp, symbol, side, reason, details FROM risk_blocks ORDER BY id DESC LIMIT ?",
            "errors": "SELECT timestamp, context, message, details FROM errors ORDER BY id DESC LIMIT ?",
        }
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            for name, sql in queries.items():
                output[name] = [dict(row) for row in conn.execute(sql, (limit,)).fetchall()]
        return output
