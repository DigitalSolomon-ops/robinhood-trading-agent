"""Scanner: the 5 pillars, the RVOL baseline, and the composite ranking.

All network is mocked -- no MassiveClient, no HTTP, no SMTP. The fake market is
built from grouped-daily Bars so the same code path the real client feeds runs
here unchanged.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from src.equity_intelligence.massive_client import Bar, NewsInsight, NewsItem, TickerDetails
from src.smallcap_scout.scanner import (
    PILLAR_CATALYST,
    PILLAR_LOW_FLOAT,
    ScoutPick,
    scan,
)

SCAN_DAY = date(2026, 8, 28)  # a Friday
NOW = datetime(2026, 8, 28, 16, 0, tzinfo=UTC)


def _bar(ticker: str, close: float, volume: float) -> Bar:
    return Bar(
        timestamp_ms=0,
        open=close * 0.98,
        high=close * 1.03,
        low=close * 0.95,
        close=close,
        volume=volume,
        vwap=close,
        transactions=1000,
        ticker=ticker,
    )


def _business_days_desc(end: date, count: int) -> list[date]:
    days: list[date] = []
    day = end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day = day - timedelta(days=1)
    return days


# ticker -> (scan_close, scan_vol, baseline_close, baseline_vol, sessions_present)
# sessions_present limits how many sessions (from the scan day back) the name
# appears in; None = every session in the window.
MARKET = {
    "WINR": (6.00, 600_000, 5.00, 100_000, None),      # all pillars; rvol = 6.0
    "WEAK": (5.55, 501_000, 5.00, 100_000, None),      # +11%, rvol ~5.01
    "UNKWN": (6.00, 600_000, 5.00, 100_000, None),     # qualifies; float unknown
    "BIGFLOT": (6.00, 600_000, 5.00, 100_000, None),   # qualifies 1-3, float too big
    "PRICEY": (55.0, 600_000, 45.0, 100_000, None),    # fails price range
    "LOWVOL": (6.00, 200_000, 5.00, 100_000, None),    # rvol 2.0, fails pillar 2
    "FLAT": (5.10, 600_000, 5.00, 100_000, None),      # +2%, fails pillar 1
    "THIN": (6.00, 600_000, 5.00, 100_000, 4),         # only 3 baseline days
}


def _build_sessions() -> dict[str, dict[str, Bar]]:
    days = _business_days_desc(SCAN_DAY, 21)  # scan day + 20 baseline sessions
    sessions: dict[str, dict[str, Bar]] = {d.isoformat(): {} for d in days}
    for ticker, (sc, sv, bc, bv, present) in MARKET.items():
        for idx, day in enumerate(days):
            if present is not None and idx >= present:
                break
            close, vol = (sc, sv) if idx == 0 else (bc, bv)
            sessions[day.isoformat()][ticker] = _bar(ticker, close, vol)
    return sessions


DETAILS = {
    "WINR": TickerDetails("WINR", "Winner Inc", 10_000_000, "share_class_shares_outstanding", 60_000_000, "XNAS"),
    "WEAK": TickerDetails("WEAK", "Weak Inc", 18_000_000, "share_class_shares_outstanding", 99_900_000, "XNAS"),
    "BIGFLOT": TickerDetails("BIGFLOT", "Big Float Co", 500_000_000, "share_class_shares_outstanding", 3_000_000_000, "XNYS"),
    # UNKWN deliberately absent -> get_ticker_details raises -> float unknown.
}

NEWS = {
    "WINR": [
        NewsItem(
            id="n1",
            title="Winner Inc lands a major contract",
            published_utc="2026-08-28T14:00:00Z",
            article_url="https://example.com/winr",
            tickers=("WINR",),
            insights=(NewsInsight(ticker="WINR", sentiment="positive", sentiment_reasoning="new revenue"),),
        )
    ],
}


class FakeClient:
    def __init__(self) -> None:
        self.sessions = _build_sessions()
        self.detail_calls: list[str] = []

    def get_grouped_daily(self, date_str: str, **_kw) -> list[Bar]:
        return list(self.sessions.get(date_str, {}).values())

    def get_ticker_details(self, ticker: str) -> TickerDetails:
        self.detail_calls.append(ticker)
        if ticker in DETAILS:
            return DETAILS[ticker]
        raise RuntimeError(f"no details for {ticker}")

    def get_ticker_news(self, ticker: str, limit: int = 20, **_kw) -> list[NewsItem]:
        return NEWS.get(ticker, [])


def _config(**overrides) -> dict:
    cfg = {
        "min_gap_pct": 10.0,
        "price_min": 1.0,
        "price_max": 20.0,
        "min_rvol": 5.0,
        "rvol_baseline_days": 20,
        "rvol_min_baseline_days": 5,
        "max_float": 20_000_000,
        "float_required": True,
        "shortlist_max": 40,
        "top_n": 10,
        "news": {"enabled": True, "recency_hours": 72, "max_articles": 20,
                 "negative_labels": ["negative"], "positive_labels": ["positive"]},
        "levels": {"horizon_days": 5, "atr_window": 14, "atr_target_mult": 2.0,
                   "stop_atr_mult": 1.5, "support_lookback": 10},
        "rank_weights": {"gap": 0.35, "rvol": 0.30, "float": 0.20, "news": 0.15},
    }
    cfg.update(overrides)
    return cfg


def _run(**overrides) -> list[ScoutPick]:
    return scan(FakeClient(), _config(**overrides), today=SCAN_DAY, now=NOW)


def _by_symbol(picks: list[ScoutPick]) -> dict[str, ScoutPick]:
    return {p.symbol: p for p in picks}


# --- pillar filters ----------------------------------------------------------


def test_only_names_clearing_pillars_1_to_4_are_returned():
    picks = _by_symbol(_run())
    assert set(picks) == {"WINR", "WEAK", "UNKWN"}
    # each rejected for a specific reason:
    assert "PRICEY" not in picks   # pillar 3 (price range)
    assert "LOWVOL" not in picks   # pillar 2 (rvol)
    assert "FLAT" not in picks     # pillar 1 (gap)
    assert "BIGFLOT" not in picks  # pillar 4 (float too big, and it IS known)
    assert "THIN" not in picks     # too few baseline days for an honest rvol


def test_big_float_is_dropped_only_because_its_float_is_known_and_too_big():
    # With float_required off, BIGFLOT survives (it clears pillars 1-3).
    picks = _by_symbol(_run(float_required=False))
    assert "BIGFLOT" in picks
    assert PILLAR_LOW_FLOAT not in picks["BIGFLOT"].pillars


def test_unknown_float_is_kept_and_flagged_not_dropped():
    picks = _by_symbol(_run())
    assert "UNKWN" in picks
    assert picks["UNKWN"].float_known is False
    assert picks["UNKWN"].float_shares is None
    assert PILLAR_LOW_FLOAT not in picks["UNKWN"].pillars


# --- the RVOL baseline -------------------------------------------------------


def test_rvol_baseline_excludes_the_scan_day():
    """WINR's scan volume is 600k on a 100k baseline -> rvol 6.0. If the scan
    day were wrongly folded into the 20-day baseline the mean would rise to
    ~124k and rvol would fall to ~4.85, below the 5.0 floor, and WINR would not
    qualify. Its presence AND its rvol of 6.0 pin the baseline to prior sessions."""
    picks = _by_symbol(_run())
    assert "WINR" in picks
    assert picks["WINR"].rvol == 6.0
    assert picks["WINR"].baseline_days == 20


def test_a_name_with_too_little_baseline_history_is_dropped():
    picks = _by_symbol(_run())
    assert "THIN" not in picks  # only 3 baseline sessions < rvol_min_baseline_days


# --- ranking -----------------------------------------------------------------


def test_stronger_momentum_with_a_catalyst_outranks_a_weaker_name():
    picks = _run()
    assert picks[0].symbol == "WINR"  # bigger move, more rvol, low float, +news
    order = [p.symbol for p in picks]
    assert order.index("WINR") < order.index("WEAK")


def test_catalyst_pillar_only_when_positive_news_exists():
    picks = _by_symbol(_run())
    assert PILLAR_CATALYST in picks["WINR"].pillars
    assert picks["WINR"].catalyst == "Winner Inc lands a major contract"
    assert PILLAR_CATALYST not in picks["WEAK"].pillars
    assert picks["WEAK"].catalyst is None


def test_levels_are_a_valid_long_setup():
    winr = _by_symbol(_run())["WINR"]
    assert winr.levels is not None
    assert winr.levels.stop < winr.levels.entry < winr.levels.target


def test_float_and_news_calls_are_made_only_for_the_shortlist():
    """The rejected-by-pillars-1-3 names must never cost a per-ticker call."""
    client = FakeClient()
    scan(client, _config(), today=SCAN_DAY, now=NOW)
    # Only names that cleared pillars 1-3 get a details call.
    assert set(client.detail_calls) <= {"WINR", "WEAK", "UNKWN", "BIGFLOT"}
    assert "PRICEY" not in client.detail_calls
    assert "LOWVOL" not in client.detail_calls
    assert "FLAT" not in client.detail_calls


def test_no_completed_sessions_returns_empty():
    class Empty:
        def get_grouped_daily(self, *_a, **_k):
            return []

    assert scan(Empty(), _config(), today=SCAN_DAY, now=NOW) == []
