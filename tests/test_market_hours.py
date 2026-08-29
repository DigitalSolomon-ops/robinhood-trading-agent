"""market_hours is the standalone RTH/holiday guard the equities broker
consults before an order is allowed to reach the connector. Tested in
isolation here; tests/test_robinhood_equity_broker.py proves the broker
actually wires it in and refuses to submit outside these hours.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from src import market_hours as mh

# A known, unambiguous non-holiday weekday: 2026-08-31 is a Monday.
A_MONDAY = date(2026, 8, 31)
A_SATURDAY = date(2026, 8, 29)


def test_a_weekday_inside_regular_hours_is_allowed() -> None:
    assert mh.blocked_reason(datetime.combine(A_MONDAY, mh.REGULAR_OPEN)) is None
    assert mh.blocked_reason(datetime(2026, 8, 31, 12, 0)) is None


def test_the_close_boundary_is_exclusive() -> None:
    # 15:59 is inside RTH, 16:00 (close) is not.
    assert mh.blocked_reason(datetime(2026, 8, 31, 15, 59)) is None
    assert mh.blocked_reason(datetime(2026, 8, 31, 16, 0)) is not None


def test_before_the_open_is_blocked_by_default() -> None:
    reason = mh.blocked_reason(datetime(2026, 8, 31, 8, 0))
    assert reason is not None
    assert "outside regular trading hours" in reason


def test_after_the_close_is_blocked_by_default() -> None:
    reason = mh.blocked_reason(datetime(2026, 8, 31, 17, 0))
    assert reason is not None
    assert "outside regular trading hours" in reason


def test_a_weekend_is_blocked_regardless_of_time_of_day() -> None:
    reason = mh.blocked_reason(datetime.combine(A_SATURDAY, mh.REGULAR_OPEN))
    assert reason is not None
    assert "weekend" in reason


# --- the extended-hours opt-out, defaults OFF ---------------------------------


def test_extended_hours_is_blocked_unless_explicitly_opted_in() -> None:
    pre_market = datetime(2026, 8, 31, 8, 0)
    assert mh.blocked_reason(pre_market) is not None
    assert mh.blocked_reason(pre_market, allow_extended_hours=False) is not None
    assert mh.blocked_reason(pre_market, allow_extended_hours=True) is None


def test_extended_hours_opt_in_still_has_a_floor_and_ceiling() -> None:
    the_middle_of_the_night = datetime(2026, 8, 31, 2, 0)
    reason = mh.blocked_reason(the_middle_of_the_night, allow_extended_hours=True)
    assert reason is not None
    assert "extended trading hours" in reason

    after_extended_close = datetime(2026, 8, 31, 21, 0)
    reason = mh.blocked_reason(after_extended_close, allow_extended_hours=True)
    assert reason is not None


def test_a_weekend_is_blocked_even_with_extended_hours_on() -> None:
    reason = mh.blocked_reason(datetime.combine(A_SATURDAY, mh.REGULAR_OPEN), allow_extended_hours=True)
    assert reason is not None
    assert "weekend" in reason


# --- market holidays, computed rather than hardcoded --------------------------


def test_a_computed_market_holiday_falls_on_a_weekday() -> None:
    # Observed-holiday shifting always moves a weekend date onto the adjacent
    # weekday, so every entry here must be Mon-Fri.
    for year in (2026, 2027, 2028):
        for holiday in mh.us_market_holidays(year):
            assert holiday.weekday() < 5, f"{holiday} is not a weekday"


def test_a_market_holiday_blocks_orders_even_during_normal_market_hours() -> None:
    holiday = sorted(mh.us_market_holidays(2026))[0]
    reason = mh.blocked_reason(datetime.combine(holiday, mh.REGULAR_OPEN))
    assert reason is not None
    assert "holiday" in reason


def test_a_market_holiday_blocks_orders_even_with_extended_hours_on() -> None:
    holiday = sorted(mh.us_market_holidays(2026))[0]
    reason = mh.blocked_reason(datetime.combine(holiday, mh.REGULAR_OPEN), allow_extended_hours=True)
    assert reason is not None
    assert "holiday" in reason


def test_good_friday_is_computed_from_easter_not_hardcoded() -> None:
    # Easter 2026 is April 5 (Sunday); Good Friday is two days earlier.
    assert date(2026, 4, 3) in mh.us_market_holidays(2026)


def test_a_new_years_day_on_saturday_is_observed_the_prior_friday() -> None:
    """Regression guard for the cross-year edge case: when Jan 1 falls on a
    Saturday, NYSE observes New Year's Day on Dec 31 of the PRIOR year. That
    date only shows up by also consulting the following year's holiday set
    (is_market_holiday does this) -- a naive same-year-only lookup would
    silently miss it and let an order through on a real closure."""
    saturday_new_years_year = next(year for year in range(2020, 2050) if date(year, 1, 1).weekday() == 5)
    observed_date = date(saturday_new_years_year - 1, 12, 31)

    assert mh.is_market_holiday(observed_date) is True
    assert mh.blocked_reason(datetime.combine(observed_date, mh.REGULAR_OPEN)) is not None


# --- market-closed cases never crash -------------------------------------------


def test_market_closed_states_return_a_reason_string_never_raise() -> None:
    """The acceptance bar is "handled cleanly": every closed state below must
    return a plain string, not raise."""
    holiday = sorted(mh.us_market_holidays(2027))[0]
    cases = [
        datetime.combine(A_SATURDAY, mh.REGULAR_OPEN),
        datetime(2026, 8, 31, 3, 0),
        datetime(2026, 8, 31, 23, 0),
        datetime.combine(holiday, mh.REGULAR_OPEN),
    ]
    for case in cases:
        reason = mh.blocked_reason(case)
        assert isinstance(reason, str)
        assert reason


def test_an_aware_datetime_is_converted_to_eastern_before_the_check() -> None:
    # 14:00 UTC on 2026-08-31 is 10:00 ET (EDT, UTC-4) -- inside RTH.
    utc_time = datetime(2026, 8, 31, 14, 0, tzinfo=timezone.utc)
    assert mh.blocked_reason(utc_time) is None

    # 12:00 UTC on the same day is 08:00 ET -- before the open.
    early_utc_time = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    reason = mh.blocked_reason(early_utc_time)
    assert reason is not None
    assert "outside regular trading hours" in reason


def test_fallback_eastern_tzinfo_never_crashes_on_a_naive_or_none_dt() -> None:
    """_load_eastern's fallback only engages when zoneinfo has no tz data
    (e.g. no `tzdata` package on a bare Windows Python); exercise its
    utcoffset/dst/tzname directly since those are what datetime.astimezone
    calls under the hood."""
    fallback = mh._FixedRuleEasternTime()
    assert fallback.utcoffset(None) is not None
    assert fallback.dst(None) is not None
    assert fallback.tzname(None) in {"EST", "EDT"}
