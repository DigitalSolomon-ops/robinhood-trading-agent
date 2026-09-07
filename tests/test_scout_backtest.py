"""Scout backtest replay helpers.

ANALYSIS ONLY -- point-in-time replay + settlement wiring; nothing here trades.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from src.scout_backtest.backtest import news_off, settle_play, weekly_dates


def _ts(day: str) -> int:
    y, m, d = (int(x) for x in day.split("-"))
    return int(datetime(y, m, d, tzinfo=UTC).timestamp() * 1000)


@dataclass
class _Bar:
    timestamp_ms: int
    high: float
    low: float


@dataclass
class _HitRate:
    hit_rate: float


@dataclass
class _Play:
    symbol: str
    direction: str
    entry: float
    target: float
    stop: float
    conviction: float
    hit_rate: _HitRate


class _Client:
    def __init__(self, daily):
        self._daily = daily

    def get_daily_bars(self, ticker, from_date, to_date, adjusted=True):
        return [_Bar(_ts(d), h, lo) for (d, h, lo) in self._daily.get(ticker.upper(), [])]

    def get_aggs_range(self, ticker, mult, span, f, t, adjusted=True, sort="asc", limit=5000):
        return []


def test_news_off_disables_news_without_mutating_original():
    config = {"news": {"enabled": True, "max_articles": 20}, "horizon_days": 10}
    off = news_off(config)
    assert off["news"]["enabled"] is False
    assert off["news"]["max_articles"] == 20  # other keys preserved
    assert config["news"]["enabled"] is True  # original untouched


def test_weekly_dates_are_past_weekdays_oldest_first():
    end = date(2026, 9, 2)
    days = weekly_dates(end, months=12)
    assert len(days) >= 40  # ~52 weekly points over a year
    assert days == sorted(days)  # oldest first
    assert all(d < end for d in days)
    assert all(d.weekday() < 5 for d in days)


def test_settle_play_produces_calibration_record():
    play = _Play("NVDA", "call", entry=100, target=120, stop=90, conviction=85.0, hit_rate=_HitRate(0.62))
    client = _Client({"NVDA": [("2026-09-03", 105, 98), ("2026-09-04", 121, 101)]})
    rec = settle_play(client, play, date(2026, 9, 2), horizon_days=10)
    assert rec is not None
    assert rec["source"] == "options"
    assert rec["conviction"] == 85.0
    assert rec["predicted_hit_rate"] == 0.62
    assert rec["outcome"]["verdict"] == "WIN"
    assert rec["date"] == "2026-09-02"


def test_settle_play_none_when_no_forward_bars():
    play = _Play("X", "call", entry=100, target=120, stop=90, conviction=50.0, hit_rate=_HitRate(0.5))
    rec = settle_play(_Client({}), play, date(2026, 9, 2), horizon_days=10)
    assert rec is None  # no bars after the date -> cannot settle
