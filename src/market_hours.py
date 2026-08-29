from __future__ import annotations

from datetime import date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# NYSE/Nasdaq regular trading hours.
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)

# The conservative full extended-hours window (pre-market + after-hours).
# Only consulted when the config-gated opt-out is explicitly on; a real
# broker session may support a narrower window, but staying inside this one
# never asks the connector for a time the market cannot actually be in.
EXTENDED_OPEN = time(4, 0)
EXTENDED_CLOSE = time(20, 0)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The date of the n-th `weekday` (Monday=0) in `month`, 1-indexed."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The date of the last `weekday` (Monday=0) in `month`."""
    next_month_first = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last_day = next_month_first - timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


def _easter(year: int) -> date:
    """Easter Sunday via the Anonymous Gregorian algorithm. Good Friday is
    two days earlier -- the only floating NYSE holiday not fixed to a
    weekday-of-month rule."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(holiday: date) -> date:
    """NYSE observes a Saturday holiday the preceding Friday, and a Sunday
    holiday the following Monday. This can push a Jan-1 New Year's Day
    holiday into December of the prior year -- see is_market_holiday."""
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def us_market_holidays(year: int) -> set[date]:
    """Full-day NYSE/Nasdaq closures for a calendar year, weekend-observed.

    Early-close half-days (e.g. the day after Thanksgiving) are NOT modeled
    here -- they narrow the regular session but do not close the market, and
    Robinhood does not publish them on a fixed rule the way full closures are.
    """
    fixed = [
        date(year, 1, 1),  # New Year's Day
        date(year, 6, 19),  # Juneteenth
        date(year, 7, 4),  # Independence Day
        date(year, 12, 25),  # Christmas
    ]
    floating = [
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _easter(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
    ]
    return {_observed(day) for day in fixed} | set(floating)


def is_market_holiday(day: date) -> bool:
    """Whether `day` is a full NYSE/Nasdaq closure.

    Checks both `day.year` and `day.year + 1`: a Jan-1 New Year's Day that
    falls on a Saturday is observed on Dec 31 of the PRIOR year, so a Dec-31
    date is only found by also computing the following year's holiday set.
    """
    return day in (us_market_holidays(day.year) | us_market_holidays(day.year + 1))


class _FixedRuleEasternTime(tzinfo):
    """US Eastern Time from the post-2007 DST rule (2nd Sunday in March to
    1st Sunday in November), used ONLY as a fallback when the platform has
    no IANA tz database for zoneinfo to load -- e.g. a bare Windows Python
    with no `tzdata` package installed and no network to fetch one. A real
    deployment runs on Linux with system tzdata and gets zoneinfo's
    authoritative data instead; see `_load_eastern` below. The 2 a.m.
    transition instant itself is not modeled precisely -- immaterial for a
    trading-hours gate that only ever cares about which side of 09:30/16:00
    a timestamp falls on.
    """

    _STANDARD_OFFSET = timedelta(hours=-5)
    _DAYLIGHT_OFFSET = timedelta(hours=-4)

    @staticmethod
    def _is_daylight(dt: datetime | None) -> bool:
        if dt is None:
            return False
        year = dt.year
        start = datetime.combine(_nth_weekday(year, 3, 6, 2), time(2, 0))
        end = datetime.combine(_nth_weekday(year, 11, 6, 1), time(2, 0))
        naive = dt.replace(tzinfo=None)
        return start <= naive < end

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return self._DAYLIGHT_OFFSET if self._is_daylight(dt) else self._STANDARD_OFFSET

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=1) if self._is_daylight(dt) else timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "EDT" if self._is_daylight(dt) else "EST"


def _load_eastern() -> tzinfo:
    try:
        return ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        return _FixedRuleEasternTime()


EASTERN = _load_eastern()


def _as_eastern(now: datetime) -> datetime:
    """Normalize to US/Eastern. A naive datetime is treated as already being
    Eastern wall-clock time (the common case for a test or a clock that was
    already localized); an aware one is converted."""
    if now.tzinfo is None:
        return now.replace(tzinfo=EASTERN)
    return now.astimezone(EASTERN)


def blocked_reason(now: datetime, allow_extended_hours: bool = False) -> str | None:
    """None when equity orders may proceed at `now`; otherwise a short,
    human-readable reason for the audit log.

    Regular trading hours (09:30-16:00 ET, Mon-Fri, non-holiday) are always
    allowed. Extended hours (04:00-20:00 ET) are allowed ONLY when
    `allow_extended_hours` is explicitly True -- the config-gated opt-out
    that defaults OFF. Weekends and market holidays are blocked regardless.
    """
    eastern_now = _as_eastern(now)
    if eastern_now.weekday() >= 5:
        return f"market closed: {eastern_now:%Y-%m-%d} is a weekend"
    if is_market_holiday(eastern_now.date()):
        return f"market closed: {eastern_now:%Y-%m-%d} is a US market holiday"

    current_time = eastern_now.time()
    if REGULAR_OPEN <= current_time < REGULAR_CLOSE:
        return None
    if allow_extended_hours and EXTENDED_OPEN <= current_time < EXTENDED_CLOSE:
        return None
    if allow_extended_hours:
        return f"outside extended trading hours ({eastern_now:%H:%M} ET)"
    return f"outside regular trading hours ({eastern_now:%H:%M} ET)"
