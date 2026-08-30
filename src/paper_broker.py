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

    def _recompute_from_ledger(self) -> tuple[float, dict[str, float]]:
        """Independent truth: cash and per-symbol quantity recomputed straight
        from the raw filled rows, by the explicit accounting identity rather
        than through get_portfolio(). Reconciliation compares the two so a bug
        in either path shows up as a mismatch instead of agreeing with itself.

            cash == starting_cash
                    - sum(buy notional + fees)
                    + sum(sell notional - fees)
        """
        cash = float(self.starting_cash)
        quantities: dict[str, float] = {}
        with self.connect() as conn:
            for symbol, side, quantity, notional, fees in conn.execute(
                "SELECT symbol, side, quantity, notional, fees_estimate FROM paper_trades WHERE status = 'filled'"
            ):
                q, n, f = float(quantity), float(notional), float(fees)
                if side == "buy":
                    cash -= n + f
                    quantities[symbol] = quantities.get(symbol, 0.0) + q
                else:
                    cash += n - f
                    quantities[symbol] = quantities.get(symbol, 0.0) - q
        return cash, quantities

    @staticmethod
    def _parse_live_positions(payload: Any) -> dict[str, float]:
        """Normalize a connector get_equity_positions() payload to symbol->qty."""
        rows = payload.get("positions", []) if isinstance(payload, dict) else (payload or [])
        result: dict[str, float] = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            symbol = row.get("symbol") or row.get("ticker")
            if symbol is None:
                continue
            raw = row.get("quantity", row.get("qty", 0))
            try:
                result[symbol] = result.get(symbol, 0.0) + float(raw)
            except (TypeError, ValueError):
                result[symbol] = result.get(symbol, 0.0)
        return result

    def reconcile_positions(
        self,
        epsilon: float = 0.000001,
        expected_positions: dict[str, float] | None = None,
        connector: Any | None = None,
        account_number: str | None = None,
    ) -> dict[str, Any]:
        """Reconcile the paper ledger against INDEPENDENT truth, so 'clean'
        (errors == []) has a reachable failure mode rather than being a
        tautology that only a negative quantity -- impossible in a long-only
        lane -- could ever trip.

        Checks, any of which populate `errors`:
          - a real (non-dust) negative position is a short and cannot exist;
          - get_portfolio()'s cash must equal the ledger-recomputed cash;
          - cash may never be negative (cash account, settled funds only);
          - get_portfolio()'s per-symbol quantity must equal the ledger's;
          - if `expected_positions` is supplied, it must match holdings;
          - if a live `connector` is supplied, its get_equity_positions() must
            match the local ledger (the live-mode diff).
        """
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
                errors.append({"issue": "negative_position", "symbol": symbol, "quantity": quantity, "epsilon": epsilon})

        # Refetch after any dust adjustment, then cross-check against the
        # independently recomputed ledger truth.
        portfolio = self.get_portfolio()
        ledger_cash, ledger_qty = self._recompute_from_ledger()
        observed_qty = {symbol: float(position.quantity) for symbol, position in portfolio.positions.items()}

        if abs(ledger_cash - float(portfolio.cash_usd)) > epsilon:
            errors.append({"issue": "cash_mismatch", "ledger_cash": ledger_cash, "portfolio_cash": float(portfolio.cash_usd)})
        if ledger_cash < -epsilon:
            errors.append({"issue": "negative_cash", "cash": ledger_cash})
        for symbol in set(ledger_qty) | set(observed_qty):
            if abs(ledger_qty.get(symbol, 0.0) - observed_qty.get(symbol, 0.0)) > epsilon:
                errors.append(
                    {"issue": "position_mismatch", "symbol": symbol, "ledger": ledger_qty.get(symbol, 0.0), "portfolio": observed_qty.get(symbol, 0.0)}
                )

        if expected_positions is not None:
            for symbol in set(expected_positions) | set(observed_qty):
                expected = float(expected_positions.get(symbol, 0.0))
                actual = observed_qty.get(symbol, 0.0)
                if abs(expected - actual) > epsilon:
                    errors.append({"issue": "expected_position_mismatch", "symbol": symbol, "expected": expected, "actual": actual})

        if connector is not None:
            try:
                payload = connector.get_equity_positions(account_number) if account_number is not None else connector.get_equity_positions()
            except TypeError:
                payload = connector.get_equity_positions()
            live_qty = self._parse_live_positions(payload)
            for symbol in set(live_qty) | set(observed_qty):
                if abs(live_qty.get(symbol, 0.0) - observed_qty.get(symbol, 0.0)) > epsilon:
                    errors.append(
                        {"issue": "live_position_mismatch", "symbol": symbol, "live": live_qty.get(symbol, 0.0), "local": observed_qty.get(symbol, 0.0)}
                    )

        return {"adjusted": adjusted, "errors": errors, "cash": ledger_cash, "positions": observed_qty}

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
