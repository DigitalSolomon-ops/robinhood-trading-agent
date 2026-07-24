from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .kill_switch import KillSwitch
from .portfolio import Portfolio
from .strategy_engine import TradeSignal


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reasons: list[str]


class RiskManager:
    def __init__(self, rules: dict[str, Any], kill_switch: KillSwitch) -> None:
        self.rules = rules
        self.kill_switch = kill_switch

    def evaluate(
        self,
        signal: TradeSignal,
        mode: str,
        notional: float,
        portfolio: Portfolio,
        daily_summary: dict[str, Any],
        has_api_credentials: bool,
        submit_live_order: bool = False,
        order_quantity: float | None = None,
        current_price: float | None = None,
        last_order_timestamp: str | None = None,
    ) -> RiskDecision:
        reasons: list[str] = []
        trading = self.rules.get("trading", {})
        risk = self.rules.get("risk", {})
        orders = self.rules.get("orders", {})

        reasons.extend(self.kill_switch.halt_reasons())
        if not trading.get("enabled", False):
            reasons.append("config trading.enabled=false")
        if mode not in {"paper", "live-dry-run", "live"}:
            reasons.append(f"unsupported trading mode: {mode}")
        if submit_live_order and mode != "live":
            reasons.append("live submission requires TRADING_MODE=live")
        if submit_live_order and trading.get("mode") != "live":
            reasons.append("live submission requires config trading.mode=live")
        if signal.side not in {"buy", "sell"}:
            reasons.append(f"not an actionable signal: {signal.side}")
        if signal.symbol not in set(trading.get("allowed_symbols", [])):
            reasons.append(f"symbol not allowlisted: {signal.symbol}")
        if notional > float(risk.get("max_trade_amount_usd", 0)):
            reasons.append("trade exceeds max trade amount")
        if float(daily_summary.get("realized_pnl", 0)) <= -float(risk.get("max_daily_loss_usd", 0)):
            reasons.append("max daily loss limit is hit")
        if int(daily_summary.get("trade_count", 0)) >= int(risk.get("max_trades_per_day", 0)):
            reasons.append("max daily trade count is hit")
        cooldown_seconds = int(risk.get("min_order_cooldown_seconds", 0) or 0)
        if cooldown_seconds > 0 and last_order_timestamp:
            try:
                last_order_time = datetime.fromisoformat(last_order_timestamp.replace("Z", "+00:00"))
                if last_order_time.tzinfo is None:
                    last_order_time = last_order_time.replace(tzinfo=UTC)
                elapsed = (datetime.now(UTC) - last_order_time.astimezone(UTC)).total_seconds()
                if elapsed < cooldown_seconds:
                    reasons.append("order cooldown is active")
            except ValueError:
                reasons.append("last order timestamp is invalid")
        if portfolio.open_position_count >= int(risk.get("max_open_positions", 0)) and signal.side == "buy":
            reasons.append("max open positions is hit")
        if signal.side == "buy" and portfolio.quantity_for(signal.symbol) > 0 and not risk.get("allow_position_scaling", False):
            reasons.append("position scaling disabled")
        allocation_limit = float(risk.get("max_symbol_allocation_percent", 0) or 0)
        if signal.side == "buy" and allocation_limit > 0 and current_price and order_quantity:
            total_equity = portfolio.cash_usd
            for position in portfolio.positions.values():
                total_equity += max(float(position.quantity), 0.0) * current_price
            if total_equity > 0:
                symbol_quantity_after = max(portfolio.quantity_for(signal.symbol), 0.0) + float(order_quantity)
                symbol_allocation_percent = ((symbol_quantity_after * current_price) / total_equity) * 100
                if symbol_allocation_percent > allocation_limit:
                    reasons.append("symbol allocation limit exceeded")
        if signal.side == "sell":
            held_quantity = portfolio.quantity_for(signal.symbol)
            requested_quantity = float(order_quantity or 0)
            if held_quantity <= 0:
                reasons.append("no open position to sell")
            elif requested_quantity and requested_quantity > held_quantity + 1e-12:
                reasons.append("sell would create short position")
        if signal.side == "buy" and orders.get("require_stop_loss", True) and not signal.stop_loss_percent:
            reasons.append("stop-loss is missing")
        if signal.side == "buy" and orders.get("require_take_profit", True) and not signal.take_profit_percent:
            reasons.append("take-profit is missing")
        if risk.get("require_cash_available", True) and signal.side == "buy" and not portfolio.has_cash_for(notional):
            reasons.append("order would exceed available cash")
        if risk.get("allow_shorting", risk.get("allow_shorts", False)) and signal.side == "sell" and portfolio.quantity_for(signal.symbol) <= 0:
            reasons.append("shorting is not allowed")
        if not risk.get("allow_margin", False) and not risk.get("require_cash_available", True):
            reasons.append("margin is not allowed")
        if not has_api_credentials:
            reasons.append("API credentials are missing")
        return RiskDecision(allowed=not reasons, reasons=reasons)
