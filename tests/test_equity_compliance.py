"""Unit tests for the equities lane's compliance guards, in isolation from the
broker/connector plumbing:

1. PatternDayTraderGuard blocks the 4th day trade in a trailing 5-business-day
   window while account equity is under FINRA's $25k threshold, and gets out
   of the way once equity clears it.
2. SettlementGuard blocks a buy that would spend still-unsettled sale
   proceeds (the cash-account good-faith rule, and this account's only
   defense against an effective margin trade -- it has no margin buying power
   to fall back on).
3. assert_long_only refuses a sell that would exceed the held quantity.

Broker-level wiring (that these guards actually fire during a real order,
and are re-checked at the moment of submission) is covered in
tests/test_robinhood_equity_broker.py.
"""

from __future__ import annotations

from datetime import date, datetime

from src.equity_compliance import (
    PDT_EQUITY_THRESHOLD_USD,
    PDT_WINDOW_BUSINESS_DAYS,
    PatternDayTraderGuard,
    SettlementGuard,
    _business_days_back,
    assert_long_only,
)

SYMBOL = "SYMBOL"

# A Monday, so "the last 5 business days" is unambiguous and does not itself
# straddle a weekend at the edges of the window being tested.
A_MONDAY = datetime(2026, 8, 31, 10, 0)


def filled(symbol: str, side: str, day: str, notional: float = 100.0, status: str = "submitted") -> dict:
    return {"symbol": symbol, "side": side, "status": status, "notional": notional, "timestamp": f"{day}T10:00:00+00:00"}


# --- PatternDayTraderGuard ----------------------------------------------------


def test_no_history_never_blocks() -> None:
    guard = PatternDayTraderGuard([])

    decision = guard.evaluate(SYMBOL, "sell", account_equity=1000.0, as_of=A_MONDAY)

    assert decision.allowed


def test_a_sell_with_no_same_day_buy_is_not_a_day_trade() -> None:
    # Bought last week, selling today: a normal sale, not a day trade.
    history = [filled(SYMBOL, "buy", "2026-08-24")]
    guard = PatternDayTraderGuard(history)

    decision = guard.evaluate(SYMBOL, "sell", account_equity=1000.0, as_of=A_MONDAY)

    assert decision.allowed


def test_three_prior_day_trades_allow_a_fourth_when_equity_is_at_or_above_threshold() -> None:
    history = [
        filled(SYMBOL, "buy", "2026-08-25"), filled(SYMBOL, "sell", "2026-08-25"),
        filled(SYMBOL, "buy", "2026-08-26"), filled(SYMBOL, "sell", "2026-08-26"),
        filled(SYMBOL, "buy", "2026-08-27"), filled(SYMBOL, "sell", "2026-08-27"),
        filled(SYMBOL, "buy", "2026-08-31"),  # today's opening leg
    ]
    guard = PatternDayTraderGuard(history)

    decision = guard.evaluate(SYMBOL, "sell", account_equity=PDT_EQUITY_THRESHOLD_USD, as_of=A_MONDAY)

    assert decision.allowed


def test_the_fourth_day_trade_in_the_window_is_blocked_under_25k() -> None:
    # Three day trades already this trailing week, then a same-day buy today
    # -- selling now would be the 4th day trade while equity is under $25k.
    history = [
        filled(SYMBOL, "buy", "2026-08-25"), filled(SYMBOL, "sell", "2026-08-25"),
        filled(SYMBOL, "buy", "2026-08-26"), filled(SYMBOL, "sell", "2026-08-26"),
        filled(SYMBOL, "buy", "2026-08-27"), filled(SYMBOL, "sell", "2026-08-27"),
        filled(SYMBOL, "buy", "2026-08-31"),  # today's opening leg
    ]
    guard = PatternDayTraderGuard(history)

    decision = guard.evaluate(SYMBOL, "sell", account_equity=24_999.99, as_of=A_MONDAY)

    assert not decision.allowed
    assert "pattern-day-trader guard" in decision.reason
    assert "day trade #4" in decision.reason


def test_a_third_day_trade_in_the_window_is_still_allowed_under_25k() -> None:
    history = [
        filled(SYMBOL, "buy", "2026-08-25"), filled(SYMBOL, "sell", "2026-08-25"),
        filled(SYMBOL, "buy", "2026-08-26"), filled(SYMBOL, "sell", "2026-08-26"),
        filled(SYMBOL, "buy", "2026-08-31"),  # today's opening leg
    ]
    guard = PatternDayTraderGuard(history)

    decision = guard.evaluate(SYMBOL, "sell", account_equity=1_000.0, as_of=A_MONDAY)

    assert decision.allowed


def test_day_trades_outside_the_five_business_day_window_do_not_count() -> None:
    # Three day trades, but over two weeks ago -- outside the trailing window.
    history = [
        filled(SYMBOL, "buy", "2026-08-10"), filled(SYMBOL, "sell", "2026-08-10"),
        filled(SYMBOL, "buy", "2026-08-11"), filled(SYMBOL, "sell", "2026-08-11"),
        filled(SYMBOL, "buy", "2026-08-12"), filled(SYMBOL, "sell", "2026-08-12"),
        filled(SYMBOL, "buy", "2026-08-31"),
    ]
    guard = PatternDayTraderGuard(history)

    decision = guard.evaluate(SYMBOL, "sell", account_equity=1_000.0, as_of=A_MONDAY)

    assert decision.allowed


def test_day_trades_on_a_different_symbol_do_not_count_toward_this_ones_limit() -> None:
    history = [
        filled("OTHER", "buy", "2026-08-25"), filled("OTHER", "sell", "2026-08-25"),
        filled("OTHER", "buy", "2026-08-26"), filled("OTHER", "sell", "2026-08-26"),
        filled("OTHER", "buy", "2026-08-27"), filled("OTHER", "sell", "2026-08-27"),
        filled(SYMBOL, "buy", "2026-08-31"),
    ]
    guard = PatternDayTraderGuard(history)

    decision = guard.evaluate(SYMBOL, "sell", account_equity=1_000.0, as_of=A_MONDAY)

    assert decision.allowed


def test_a_buy_is_never_blocked_by_the_pdt_guard_itself() -> None:
    # Opening a position is never the trade PDT counts -- only the closing leg is.
    history = [
        filled(SYMBOL, "buy", "2026-08-25"), filled(SYMBOL, "sell", "2026-08-25"),
        filled(SYMBOL, "buy", "2026-08-26"), filled(SYMBOL, "sell", "2026-08-26"),
        filled(SYMBOL, "buy", "2026-08-27"), filled(SYMBOL, "sell", "2026-08-27"),
    ]
    guard = PatternDayTraderGuard(history)

    decision = guard.evaluate(SYMBOL, "buy", account_equity=1_000.0, as_of=A_MONDAY)

    assert decision.allowed


# --- SettlementGuard / good-faith --------------------------------------------


def test_a_sell_is_never_blocked_by_the_settlement_guard() -> None:
    guard = SettlementGuard([])

    decision = guard.evaluate("sell", notional=1_000_000.0, total_cash=0.0, as_of=A_MONDAY)

    assert decision.allowed


def test_a_buy_within_settled_cash_is_allowed() -> None:
    guard = SettlementGuard([])

    decision = guard.evaluate("buy", notional=100.0, total_cash=500.0, as_of=A_MONDAY)

    assert decision.allowed


def test_a_buy_that_would_spend_unsettled_sale_proceeds_is_blocked() -> None:
    # Sold earlier today; under T+1 those proceeds settle tomorrow at the
    # earliest, so spending them on a buy the same day is a good-faith
    # violation -- this account has no margin to cover the difference.
    history = [filled(SYMBOL, "sell", "2026-08-31", notional=500.0)]  # today
    guard = SettlementGuard(history)

    decision = guard.evaluate("buy", notional=500.0, total_cash=500.0, as_of=A_MONDAY)

    assert not decision.allowed
    assert "good-faith guard" in decision.reason
    assert "no margin" in decision.reason


def test_a_buy_fitting_inside_the_settled_remainder_is_allowed() -> None:
    # $500 on hand, $400 of it unsettled from a sale made earlier today --
    # $100 is settled cash from before today and free to spend.
    history = [filled(SYMBOL, "sell", "2026-08-31", notional=400.0)]  # today
    guard = SettlementGuard(history)

    decision = guard.evaluate("buy", notional=100.0, total_cash=500.0, as_of=A_MONDAY)

    assert decision.allowed


def test_proceeds_settle_after_one_business_day() -> None:
    # Sold on Friday 2026-08-28; by Monday 2026-08-31 (T+1 business day) those
    # proceeds have settled and are free to spend.
    history = [filled(SYMBOL, "sell", "2026-08-27", notional=500.0)]  # Thursday
    guard = SettlementGuard(history)

    decision = guard.evaluate("buy", notional=500.0, total_cash=500.0, as_of=A_MONDAY)

    assert decision.allowed


def test_unfilled_or_refused_sells_do_not_count_as_unsettled_proceeds() -> None:
    history = [filled(SYMBOL, "sell", "2026-08-28", notional=500.0, status="dry_run_order_preview")]
    guard = SettlementGuard(history)

    decision = guard.evaluate("buy", notional=500.0, total_cash=500.0, as_of=A_MONDAY)

    assert decision.allowed


# --- assert_long_only ---------------------------------------------------------


def test_selling_no_more_than_held_is_allowed() -> None:
    decision = assert_long_only(SYMBOL, "sell", order_quantity=2.0, held_quantity=2.0)

    assert decision.allowed


def test_selling_more_than_held_is_a_short_and_is_refused() -> None:
    decision = assert_long_only(SYMBOL, "sell", order_quantity=3.0, held_quantity=2.0)

    assert not decision.allowed
    assert "short" in decision.reason


def test_a_sell_with_no_position_at_all_is_refused() -> None:
    decision = assert_long_only(SYMBOL, "sell", order_quantity=1.0, held_quantity=0.0)

    assert not decision.allowed


def test_a_buy_is_never_touched_by_the_long_only_guard() -> None:
    decision = assert_long_only(SYMBOL, "buy", order_quantity=1_000_000.0, held_quantity=0.0)

    assert decision.allowed


# --- the trailing PDT window spans REAL trading days, not calendar weekdays ---

# Christmas 2026-12-25 is a Friday full-market closure. Anchored on Wed
# 2026-12-30, the trailing five TRADING days reach back to Wed 2026-12-23 --
# NOT to Thu 2026-12-24, where a naive calendar-weekday count (that treated the
# holiday as a session) would have stopped one day short.
_ANCHOR = date(2026, 12, 30)
_CHRISTMAS = date(2026, 12, 25)
_FIFTH_TRADING_DAY = date(2026, 12, 23)


def test_business_days_back_skips_a_market_holiday_in_the_window() -> None:
    window = _business_days_back(_ANCHOR, PDT_WINDOW_BUSINESS_DAYS)

    assert len(window) == PDT_WINDOW_BUSINESS_DAYS
    # The holiday is a calendar weekday but NOT a trading day, so it is skipped.
    assert _CHRISTMAS not in window
    # Skipping it makes the window reach one genuine session further back --
    # the far edge a calendar-weekday count would have missed.
    assert _FIFTH_TRADING_DAY in window


def test_pdt_window_counts_a_day_trade_on_the_far_side_of_a_holiday() -> None:
    # One completed day trade sitting on the 5th real trading day back. A
    # calendar-weekday window (the bug) stops at 2026-12-24 and never counts
    # it; the holiday-aware window includes it, so the PDT tally is correct.
    history = [
        {"symbol": SYMBOL, "side": "buy", "status": "filled", "timestamp": f"{_FIFTH_TRADING_DAY}T10:00:00"},
        {"symbol": SYMBOL, "side": "sell", "status": "filled", "timestamp": f"{_FIFTH_TRADING_DAY}T14:00:00"},
    ]
    guard = PatternDayTraderGuard(history)

    assert guard.count_day_trades_in_window(SYMBOL, datetime(2026, 12, 30, 10, 0)) == 1
