from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .portfolio import Portfolio, Position


class PaperBroker:
    def __init__(self, db_path: Path | str = "data/paper_trades.db", starting_cash: float = 10000.0) -> None:
        self.db_path = Path(db_path)
        self.starting_cash = starting_cash
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    client_order_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    price REAL NOT NULL,
                    notional REAL NOT NULL,
                    fees_estimate REAL NOT NULL,
                    reason TEXT,
                    strategy_signal TEXT,
                    status TEXT NOT NULL,
                    pnl REAL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_trades_archive (
                    id INTEGER,
                    timestamp TEXT NOT NULL,
                    client_order_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    price REAL NOT NULL,
                    notional REAL NOT NULL,
                    fees_estimate REAL NOT NULL,
                    reason TEXT,
                    strategy_signal TEXT,
                    status TEXT NOT NULL,
                    pnl REAL DEFAULT 0,
                    archived_at TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def now() -> str:
        return datetime.now(UTC).isoformat()

    def get_portfolio(self) -> Portfolio:
        cash = self.starting_cash
        positions: dict[str, Position] = {}
        with self.connect() as conn:
            for symbol, side, quantity, price, notional, fees in conn.execute(
                "SELECT symbol, side, quantity, price, notional, fees_estimate FROM paper_trades WHERE status = 'filled'"
            ):
                signed_quantity = float(quantity) if side == "buy" else -float(quantity)
                cash += -float(notional) - float(fees) if side == "buy" else float(notional) - float(fees)
                position = positions.setdefault(symbol, Position(symbol=symbol, quantity=0.0, average_price=float(price)))
                new_quantity = position.quantity + signed_quantity
                if new_quantity:
                    position.average_price = ((position.average_price * position.quantity) + (float(price) * signed_quantity)) / new_quantity
                position.quantity = new_quantity
        return Portfolio(cash_usd=cash, positions=positions)

    def reset(self, archive: bool = True) -> int:
        with self.connect() as conn:
            count = int(conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] or 0)
            if archive and count:
                archived_at = self.now()
                conn.execute(
                    """
                    INSERT INTO paper_trades_archive
                    (id, timestamp, client_order_id, symbol, side, quantity, price, notional, fees_estimate, reason, strategy_signal, status, pnl, archived_at)
                    SELECT id, timestamp, client_order_id, symbol, side, quantity, price, notional, fees_estimate, reason, strategy_signal, status, pnl, ?
                    FROM paper_trades
                    """,
                    (archived_at,),
                )
            conn.execute("DELETE FROM paper_trades")
        return count

    def reconcile_positions(self, epsilon: float = 0.000001) -> dict[str, Any]:
        portfolio = self.get_portfolio()
        adjusted: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for symbol, position in portfolio.positions.items():
            quantity = float(position.quantity)
            if quantity < 0 and abs(quantity) <= epsilon:
                adjustment_qty = abs(quantity)
                price = float(position.average_price or 0)
                notional = adjustment_qty * price
                row = {
                    "timestamp": self.now(),
                    "client_order_id": f"reconcile-{uuid.uuid4()}",
                    "symbol": symbol,
                    "side": "buy",
                    "quantity": adjustment_qty,
                    "price": price,
                    "notional": notional,
                    "fees_estimate": 0.0,
                    "reason": "paper_reconcile_zero_negative",
                    "strategy_signal": "reconciliation",
                    "status": "filled",
                    "pnl": 0.0,
                }
                with self.connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO paper_trades
                        (timestamp, client_order_id, symbol, side, quantity, price, notional, fees_estimate, reason, strategy_signal, status, pnl)
                        VALUES (:timestamp, :client_order_id, :symbol, :side, :quantity, :price, :notional, :fees_estimate, :reason, :strategy_signal, :status, :pnl)
                        """,
                        row,
                    )
                adjusted.append({"symbol": symbol, "previous_quantity": quantity, "adjustment_quantity": adjustment_qty})
            elif quantity < 0:
                errors.append({"symbol": symbol, "quantity": quantity, "epsilon": epsilon})
        return {"adjusted": adjusted, "errors": errors}

    def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        quantity = float(order["quantity"])
        price = float(order["limit_price"])
        notional = float(order.get("notional") or quantity * price)
        fees = round(notional * 0.001, 8)
        client_order_id = order.get("client_order_id") or str(uuid.uuid4())
        row = {
            "timestamp": self.now(),
            "client_order_id": client_order_id,
            "symbol": order["symbol"],
            "side": order["side"],
            "quantity": quantity,
            "price": price,
            "notional": notional,
            "fees_estimate": fees,
            "reason": order.get("reason", ""),
            "strategy_signal": order.get("strategy_signal", ""),
            "status": "filled",
            "pnl": 0.0,
        }
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO paper_trades
                (timestamp, client_order_id, symbol, side, quantity, price, notional, fees_estimate, reason, strategy_signal, status, pnl)
                VALUES (:timestamp, :client_order_id, :symbol, :side, :quantity, :price, :notional, :fees_estimate, :reason, :strategy_signal, :status, :pnl)
                """,
                row,
            )
        return {**row, "order_type": "limit"}

    def cancel_order(self, order_id: str) -> dict[str, str]:
        return {"id": order_id, "status": "cancel_not_supported_for_filled_paper_orders"}

    def get_order_status(self, order_id: str) -> dict[str, str]:
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM paper_trades WHERE client_order_id = ?", (order_id,)).fetchone()
        return {"id": order_id, "status": row[0] if row else "not_found"}
