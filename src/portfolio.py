from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Position:
    symbol: str
    quantity: float
    average_price: float = 0.0
    pnl: float = 0.0


@dataclass
class Portfolio:
    cash_usd: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)

    @property
    def open_position_count(self) -> int:
        return sum(1 for position in self.positions.values() if abs(position.quantity) > 0)

    def has_cash_for(self, notional: float) -> bool:
        return self.cash_usd >= notional

    def quantity_for(self, symbol: str) -> float:
        position = self.positions.get(symbol)
        return float(position.quantity) if position else 0.0

    @classmethod
    def from_robinhood(cls, account_payload: Any, holdings_payload: Any) -> "Portfolio":
        cash = 0.0
        accounts = account_payload.get("results", account_payload if isinstance(account_payload, list) else []) if isinstance(account_payload, dict) else []
        if accounts:
            first = accounts[0]
            cash = float(first.get("buying_power") or first.get("cash_available_for_trading") or 0)

        positions: dict[str, Position] = {}
        holdings = holdings_payload.get("results", holdings_payload if isinstance(holdings_payload, list) else []) if isinstance(holdings_payload, dict) else []
        for holding in holdings:
            asset = holding.get("asset_code")
            quantity = float(holding.get("quantity_available_for_trading") or holding.get("total_quantity") or 0)
            if asset and quantity:
                positions[f"{asset}-USD"] = Position(symbol=f"{asset}-USD", quantity=quantity)
        return cls(cash_usd=cash, positions=positions)

    @classmethod
    def from_alpaca(cls, account_payload: Any, positions_payload: Any) -> "Portfolio":
        """Build a Portfolio from Alpaca's /v2/account and /v2/positions.

        Uses cash rather than buying_power so margin is never counted as
        available capital - the risk rules forbid margin on every lane.
        """
        cash = 0.0
        if isinstance(account_payload, dict):
            cash = float(account_payload.get("cash") or 0)

        positions: dict[str, Position] = {}
        rows = positions_payload if isinstance(positions_payload, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = row.get("symbol")
            quantity = float(row.get("qty") or 0)
            if symbol and quantity:
                positions[symbol] = Position(
                    symbol=symbol,
                    quantity=quantity,
                    average_price=float(row.get("avg_entry_price") or 0),
                    pnl=float(row.get("unrealized_pl") or 0),
                )
        return cls(cash_usd=cash, positions=positions)
