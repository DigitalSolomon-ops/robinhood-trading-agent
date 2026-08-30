from __future__ import annotations

import uuid
from typing import Any

from .live_broker import LiveBroker
from .logger import SQLiteLogger
from .paper_broker import PaperBroker
from .risk_manager import RiskManager
from .strategy_engine import TradeSignal


class OrderManager:
    def __init__(
        self,
        rules: dict[str, Any],
        risk_manager: RiskManager,
        logger: SQLiteLogger,
        paper_broker: PaperBroker,
        live_broker: LiveBroker | None = None,
    ) -> None:
        self.rules = rules
        self.risk_manager = risk_manager
        self.logger = logger
        self.paper_broker = paper_broker
        self.live_broker = live_broker

    def build_limit_order(self, signal: TradeSignal, limit_price: float, portfolio=None, amount_usd: float | None = None) -> dict[str, Any]:
        amount = float(amount_usd if amount_usd is not None else self.rules.get("risk", {}).get("max_trade_amount_usd", 25))
        quantity = amount / limit_price
        if signal.side == "sell" and portfolio is not None:
            held_quantity = portfolio.quantity_for(signal.symbol)
            if held_quantity > 0:
                quantity = min(quantity, held_quantity)
                amount = quantity * limit_price
        return {
            "client_order_id": str(uuid.uuid4()),
            "symbol": signal.symbol,
            "side": signal.side,
            "order_type": "limit",
            "limit_price": round(limit_price, 8),
            "quantity": round(quantity, 8),
            "notional": round(amount, 2),
            "time_in_force": self.rules.get("orders", {}).get("time_in_force", "gtc"),
            "reason": signal.reason,
            "strategy_signal": signal.strategy_signal,
            "stop_loss_percent": signal.stop_loss_percent,
            "take_profit_percent": signal.take_profit_percent,
        }

    def process_signal(
        self,
        signal: TradeSignal,
        limit_price: float,
        mode: str,
        portfolio,
        daily_summary: dict[str, Any],
        has_api_credentials: bool,
        amount_usd: float | None = None,
    ) -> dict[str, Any] | None:
        # `amount_usd` is an optional REDUCED per-trade cap a caller may impose
        # (the equities lane's market-regime brake does this in a risk-off
        # market). Left None the configured risk.max_trade_amount_usd applies
        # exactly as before, and either way RiskManager below still checks the
        # resulting notional against that same configured cap -- a caller can
        # only ever shrink an order this way, never enlarge one past the cap.
        order = self.build_limit_order(signal, limit_price, portfolio, amount_usd=amount_usd)
        decision = self.risk_manager.evaluate(
            signal=signal,
            mode=mode,
            notional=float(order["notional"]),
            portfolio=portfolio,
            daily_summary=daily_summary,
            has_api_credentials=has_api_credentials,
            submit_live_order=mode == "live",
            order_quantity=float(order["quantity"]),
            current_price=limit_price,
            last_order_timestamp=(self.logger.get_last_order(signal.symbol) or {}).get("timestamp"),
        )
        if not decision.allowed:
            reason = "; ".join(decision.reasons)
            self.logger.log_risk_block(signal.symbol, signal.side, reason, order)
            action = "dry_run_blocked" if mode == "live-dry-run" else "blocked"
            self.logger.log_decision(signal.symbol, action, reason, order)
            return None

        if mode == "paper":
            result = self.paper_broker.place_order(order)
            self.logger.log_order(result)
            self.logger.increment_trade_count()
            self.logger.log_decision(signal.symbol, "paper_order_filled", signal.reason, result)
            return result

        if mode == "live-dry-run":
            if not self.live_broker:
                result = {**order, "submitted": False, "status": "dry_run_order_preview"}
            else:
                # Pass the mode through so a broker that happens to be armed
                # cannot turn a preview run into a real order when this path is
                # reached directly (bypassing the broker's forced_preview).
                result = self.live_broker.place_limit_order(order, mode="live-dry-run")
            self.logger.log_order(result)
            self.logger.log_decision(signal.symbol, "dry_run_order_preview", signal.reason, result)
            return result

        if mode == "live":
            if not self.live_broker:
                raise RuntimeError("Live broker is required for live mode")
            result = self.live_broker.place_limit_order(order, mode="live")
            self.logger.log_order(result)
            self.logger.increment_trade_count()
            self.logger.log_decision(signal.symbol, "live_order_submitted", signal.reason, result)
            return result

        raise ValueError(f"Unsupported mode: {mode}")
