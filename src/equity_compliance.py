from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from . import market_hours

# FINRA's pattern-day-trader threshold and window. Not a lane risk cap the
# operator tunes in trading_rules.yaml -- a regulatory constant.
PDT_EQUITY_THRESHOLD_USD = 25_000.0
PDT_MAX_DAY_TRADES_IN_WINDOW = 3  # a 4th day trade inside the window is blocked
PDT_WINDOW_BUSINESS_DAYS = 5

# Standard equity settlement cycle (T+1 business day) for a cash account.
SETTLEMENT_BUSINESS_DAYS = 1

# Filled/submitted order history is fetched this far back -- generous enough
# to cover the 5-business-day PDT window plus weekends/holidays either guard
# might straddle.
HISTORY_LOOKBACK_DAYS = 12

_SETTLED_STATUSES = {"submitted", "filled", "accepted"}


def _parse_timestamp(value: str | datetime) -> datetime:
    """Python 3.11+'s fromisoformat accepts a trailing UTC designator
    natively, so no manual suffix rewrite is needed here."""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _add_business_days(start: date, business_days: int) -> date:
    cursor = start
    remaining = business_days
    while remaining > 0:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            remaining -= 1
    return cursor


def _business_days_back(anchor: date, count: int) -> set[date]:
    """The `count` REAL trading days ending at `anchor`, inclusive of `anchor`
    itself when it is a trading day.

    A market holiday is not a trading day even though it is a calendar weekday,
    so it is skipped here: counting it would shorten the trailing PDT window by
    a full session, letting a day trade that happened 5 trading days back fall
    outside the window and go uncounted. Skipping holidays makes the window
    span 5 genuine sessions."""
    days: set[date] = set()
    cursor = anchor
    while len(days) < count:
        if cursor.weekday() < 5 and not market_hours.is_market_holiday(cursor):
            days.add(cursor)
        cursor -= timedelta(days=1)
    return days


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: str | None = None


class PatternDayTraderGuard:
    """FINRA pattern-day-trader guard for the Agentic cash account.

    A "day trade" is a buy and a sell of the same symbol on the same
    calendar day. Robinhood confines the agent to a plain cash account with
    no margin, so there is no margin buying-power check to lean on here --
    this guard is what keeps the lane out of PDT territory instead. It only
    blocks while account equity is under FINRA's $25k threshold; at or above
    it, day trading is unrestricted.
    """

    def __init__(self, order_history: list[dict[str, Any]] | None = None) -> None:
        self.order_history = order_history or []

    def _sides_by_symbol_day(self) -> dict[tuple[str, date], set[str]]:
        sides: dict[tuple[str, date], set[str]] = {}
        for row in self.order_history:
            symbol = row.get("symbol")
            side = str(row.get("side", "")).lower()
            if not symbol or side not in {"buy", "sell"}:
                continue
            if str(row.get("status", "")) not in _SETTLED_STATUSES:
                continue
            day = _parse_timestamp(row["timestamp"]).date()
            sides.setdefault((symbol, day), set()).add(side)
        return sides

    def day_trade_days(self, symbol: str) -> set[date]:
        """Calendar days on which both a buy and a sell of `symbol` occurred."""
        sides = self._sides_by_symbol_day()
        return {day for (sym, day), day_sides in sides.items() if sym == symbol and {"buy", "sell"} <= day_sides}

    def count_day_trades_in_window(self, symbol: str, as_of: datetime) -> int:
        window = _business_days_back(as_of.date(), PDT_WINDOW_BUSINESS_DAYS)
        return sum(1 for day in self.day_trade_days(symbol) if day in window)

    def would_close_a_same_day_position(self, symbol: str, side: str, as_of: datetime) -> bool:
        """True when this sell would pair with a buy already filled today --
        i.e. submitting it completes a day trade."""
        if side != "sell":
            return False
        return "buy" in self._sides_by_symbol_day().get((symbol, as_of.date()), set())

    def evaluate(self, symbol: str, side: str, account_equity: float, as_of: datetime) -> GuardDecision:
        if account_equity >= PDT_EQUITY_THRESHOLD_USD:
            return GuardDecision(True)
        if not self.would_close_a_same_day_position(symbol, side, as_of):
            return GuardDecision(True)
        existing = self.count_day_trades_in_window(symbol, as_of)
        if existing >= PDT_MAX_DAY_TRADES_IN_WINDOW:
            return GuardDecision(
                False,
                f"pattern-day-trader guard: this would be day trade #{existing + 1} on {symbol} within the "
                f"trailing {PDT_WINDOW_BUSINESS_DAYS} business days, and account equity ${account_equity:,.2f} "
                f"is under the ${PDT_EQUITY_THRESHOLD_USD:,.0f} pattern-day-trader threshold",
            )
        return GuardDecision(True)


class SettlementGuard:
    """Good-faith / settlement guard for the Agentic cash account.

    A cash account cannot spend sale proceeds until they settle (T+1
    business day); buying with unsettled proceeds before they settle is a
    good-faith violation ("freeriding") -- the cash-account equivalent of
    what a margin call would catch in a margin account. This guard subtracts
    still-unsettled sale proceeds from cash on hand before deciding whether a
    new buy fits, since Robinhood's own cash figure is not broken out that
    way. Because a cash account has no margin buying power at all, this same
    check is what keeps a buy from ever being financed on margin.
    """

    def __init__(self, order_history: list[dict[str, Any]] | None = None) -> None:
        self.order_history = order_history or []

    def unsettled_proceeds(self, as_of: datetime) -> float:
        total = 0.0
        for row in self.order_history:
            if str(row.get("side", "")).lower() != "sell":
                continue
            if str(row.get("status", "")) not in _SETTLED_STATUSES:
                continue
            sale_day = _parse_timestamp(row["timestamp"]).date()
            settles_on = _add_business_days(sale_day, SETTLEMENT_BUSINESS_DAYS)
            if as_of.date() < settles_on:
                total += float(row.get("notional") or 0)
        return total

    def settled_cash(self, total_cash: float, as_of: datetime) -> float:
        return total_cash - self.unsettled_proceeds(as_of)

    def evaluate(self, side: str, notional: float, total_cash: float, as_of: datetime) -> GuardDecision:
        if side != "buy":
            return GuardDecision(True)
        unsettled = self.unsettled_proceeds(as_of)
        settled = total_cash - unsettled
        if notional > settled + 1e-9:
            return GuardDecision(
                False,
                f"good-faith guard: order notional ${notional:,.2f} exceeds settled cash ${settled:,.2f} "
                f"(${unsettled:,.2f} of ${total_cash:,.2f} on hand is still unsettled from a recent sale) -- "
                "this is a cash account with no margin to cover the difference",
            )
        return GuardDecision(True)


def assert_long_only(symbol: str, side: str, order_quantity: float, held_quantity: float) -> GuardDecision:
    """No shorts in this lane, ever -- independent of trading_rules.yaml's
    allow_shorting flag, which the crypto lane owns separately. A sell that
    would take the position negative is a short and is refused outright."""
    if side == "sell" and order_quantity > held_quantity + 1e-9:
        return GuardDecision(
            False,
            f"long-only guard: selling {order_quantity} {symbol} would exceed the held quantity "
            f"{held_quantity} and open a short position -- shorts are not permitted in this lane",
        )
    return GuardDecision(True)
