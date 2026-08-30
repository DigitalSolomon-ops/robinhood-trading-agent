"""The proving/backtest price series must be REAL Massive history, and must
say so.

The Massive client itself is mocked here -- no test reaches the network -- but
what is asserted is the thing the audit finding was about: the bars come from
the client's real historical endpoint over a real date window, the series
records where it came from, and there is no path by which a missing series
quietly becomes a generated one.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.equity_intelligence.massive_client import Bar
from src.equity_intelligence.massive_history import (
    MAX_LOOKBACK_DAYS,
    QUOTE_SOURCE,
    MassiveHistoryFeed,
    MassiveHistoryUnavailable,
    load_backtest_series,
)

SYMBOL = "AAPL"
OTHER = "MSFT"

# 2026-08-30 is the day this lane's proving work was done; pinned so the
# window arithmetic is asserted against a fixed date, not "today".
TODAY = date(2026, 8, 30)
DAY_MS = 86_400_000


def bars(closes: list[float], start_ms: int = 1_700_000_000_000) -> list[Bar]:
    return [
        Bar(
            timestamp_ms=start_ms + index * DAY_MS,
            open=close - 0.5,
            high=close + 0.5,
            low=close - 1.0,
            close=close,
            volume=1_000_000.0,
        )
        for index, close in enumerate(closes)
    ]


class FakeMassiveClient:
    """Records every historical-bar call instead of reaching the real API."""

    def __init__(self, series: dict[str, list[Bar]]) -> None:
        self.series = series
        self.calls: list[dict] = []

    def get_daily_bars(self, ticker: str, from_date: str, to_date: str, adjusted: bool = True) -> list[Bar]:
        self.calls.append({"ticker": ticker, "from_date": from_date, "to_date": to_date, "adjusted": adjusted})
        return list(self.series.get(ticker, []))


# --- the bars are real, and they come from the historical endpoint ----------


def test_the_feed_reads_daily_bars_from_the_client_over_the_lookback_window() -> None:
    client = FakeMassiveClient({SYMBOL: bars([100.0, 101.0, 102.0])})

    feed = MassiveHistoryFeed(client, [SYMBOL], lookback_days=730, today=TODAY)
    feed.load()

    assert client.calls == [
        {
            "ticker": SYMBOL,
            "from_date": (TODAY - timedelta(days=730)).isoformat(),
            "to_date": TODAY.isoformat(),
            "adjusted": True,
        }
    ]


def test_the_lookback_is_capped_at_two_years() -> None:
    client = FakeMassiveClient({SYMBOL: bars([100.0])})

    feed = MassiveHistoryFeed(client, [SYMBOL], lookback_days=5000, today=TODAY)

    assert feed.lookback_days == MAX_LOOKBACK_DAYS
    assert feed.from_date == TODAY - timedelta(days=MAX_LOOKBACK_DAYS)


def test_the_bars_are_loaded_once_and_reused() -> None:
    client = FakeMassiveClient({SYMBOL: bars([100.0, 101.0])})
    feed = MassiveHistoryFeed(client, [SYMBOL], today=TODAY)

    feed.load()
    feed.load()
    feed.get_prices([SYMBOL])

    assert len(client.calls) == 1, "a proving run must not re-pull its history on every cycle"


# --- replay ----------------------------------------------------------------


def test_each_call_advances_one_real_bar() -> None:
    client = FakeMassiveClient({SYMBOL: bars([100.0, 101.0, 102.0])})
    feed = MassiveHistoryFeed(client, [SYMBOL], today=TODAY)

    walked = [feed.get_prices([SYMBOL])[SYMBOL] for _ in range(3)]

    assert walked == [100.0, 101.0, 102.0]


def test_an_exhausted_series_stops_rather_than_wrapping_or_inventing_a_bar() -> None:
    """The failure mode this module exists to remove: a feed that keeps
    producing prices after the real data runs out."""
    client = FakeMassiveClient({SYMBOL: bars([100.0, 101.0])})
    feed = MassiveHistoryFeed(client, [SYMBOL], today=TODAY)

    feed.get_prices([SYMBOL])
    feed.get_prices([SYMBOL])
    after = feed.get_prices([SYMBOL])

    assert after == {}, "a spent series must not wrap around to its first bar"
    assert feed.exhausted is True
    assert feed.remaining(SYMBOL) == 0


def test_an_exhausted_series_writes_a_rationale_when_a_logger_is_given() -> None:
    class RecordingLogger:
        def __init__(self) -> None:
            self.rows: list[tuple] = []

        def log_decision(self, symbol, action, reason, details=None):
            self.rows.append((symbol, action, reason, details))

    client = FakeMassiveClient({SYMBOL: bars([100.0])})
    feed = MassiveHistoryFeed(client, [SYMBOL], today=TODAY)
    logger = RecordingLogger()

    feed.get_prices([SYMBOL], logger=logger)
    feed.get_prices([SYMBOL], logger=logger)

    assert [row[1] for row in logger.rows] == ["equity_history_exhausted"]
    assert logger.rows[0][3]["quote_source"] == QUOTE_SOURCE


def test_symbols_are_matched_case_insensitively() -> None:
    client = FakeMassiveClient({SYMBOL: bars([100.0])})
    feed = MassiveHistoryFeed(client, [SYMBOL.lower()], today=TODAY)

    assert feed.get_prices([SYMBOL.lower()]) == {SYMBOL: 100.0}


# --- fail closed: no silent fallback ---------------------------------------


def test_a_symbol_with_no_real_bars_raises_instead_of_degrading() -> None:
    client = FakeMassiveClient({SYMBOL: bars([100.0]), OTHER: []})
    feed = MassiveHistoryFeed(client, [SYMBOL, OTHER], today=TODAY)

    with pytest.raises(MassiveHistoryUnavailable) as raised:
        feed.load()

    assert OTHER in str(raised.value)
    assert "generated series" in str(raised.value)


def test_a_client_that_cannot_authenticate_propagates_rather_than_degrading() -> None:
    class BrokenClient:
        def get_daily_bars(self, *args, **kwargs):
            raise RuntimeError("MASSIVE_API_KEY is not set")

    feed = MassiveHistoryFeed(BrokenClient(), [SYMBOL], today=TODAY)

    with pytest.raises(RuntimeError):
        feed.load()


def test_zero_priced_rows_are_dropped_not_replayed() -> None:
    client = FakeMassiveClient({SYMBOL: bars([100.0, 0.0, 102.0])})
    feed = MassiveHistoryFeed(client, [SYMBOL], today=TODAY)

    assert feed.closes(SYMBOL) == [100.0, 102.0]


def test_a_lookback_of_no_days_is_refused() -> None:
    with pytest.raises(ValueError):
        MassiveHistoryFeed(FakeMassiveClient({}), [SYMBOL], lookback_days=0, today=TODAY)


# --- provenance: the source names itself ------------------------------------


def test_the_provenance_names_the_source_the_vendor_and_the_window() -> None:
    client = FakeMassiveClient({SYMBOL: bars([100.0, 101.0, 102.0])})
    feed = MassiveHistoryFeed(client, [SYMBOL], lookback_days=730, today=TODAY)

    provenance = feed.provenance()

    assert provenance["quote_source"] == QUOTE_SOURCE == "massive"
    assert "Massive" in provenance["vendor"]
    assert provenance["from_date"] == (TODAY - timedelta(days=730)).isoformat()
    assert provenance["to_date"] == TODAY.isoformat()
    assert provenance["total_bars"] == 3
    assert provenance["series"][0]["symbol"] == SYMBOL
    assert provenance["series"][0]["bars"] == 3
    assert provenance["series"][0]["first_day"] < provenance["series"][0]["last_day"]
    # It says, in the record itself, that these prices never fill an order.
    assert "connector" in provenance["execution_price_note"]


def test_the_description_is_human_readable_and_cites_the_window() -> None:
    client = FakeMassiveClient({SYMBOL: bars([100.0, 101.0])})
    feed = MassiveHistoryFeed(client, [SYMBOL], today=TODAY)

    description = feed.describe()

    assert "Massive" in description
    assert SYMBOL in description
    assert "2 bars" in description


# --- the backtest view ------------------------------------------------------


def test_the_backtest_view_returns_the_whole_real_series_with_provenance() -> None:
    client = FakeMassiveClient({SYMBOL: bars([100.0, 101.0]), OTHER: bars([50.0, 51.0, 52.0])})

    series, provenance = load_backtest_series(client, [SYMBOL, OTHER], lookback_days=365, today=TODAY)

    assert series == {SYMBOL: [100.0, 101.0], OTHER: [50.0, 51.0, 52.0]}
    assert provenance["quote_source"] == QUOTE_SOURCE
    assert provenance["lookback_days"] == 365
