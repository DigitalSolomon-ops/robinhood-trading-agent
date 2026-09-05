"""Sector Scout data layer over Massive (formerly Polygon.io). READ-ONLY.

Extends the shared read-only MassiveClient (never modified -- subclassed) with
what this lane needs beyond it:

  * pagination: the stocks entitlement paginates aggregates (next_url observed
    live on 2026-09-04); get_aggs_paged follows it so a 2-year daily pull is
    complete rather than silently truncated.
  * effective-history discovery: the plan caps stock history (~2 years,
    verified live: first available bar sits exactly 2y back). The layer
    reports the ACTUAL window so every percentile is labeled honestly.
  * fundamentals (/vX/reference/financials) and short interest
    (/stocks/v1/short-interest) -- both verified 200 on the current plan.
    The five-year ratio endpoint is NOT on the plan (verified 403); the
    valuation overlay is cross-sectional and the report's method section
    says so.
  * the grouped-daily BREADTH CACHE: one call returns the whole market for a
    day; per-day extracts for the universe's constituent tickers are persisted
    so constituent 200-day/52-week reads accumulate across daily runs instead
    of costing hundreds of calls per run. Backfill is bounded per run.

Throttling: stock-side calls are spaced (default 13s -- the observed ~5/min
stock entitlement); option snapshots ride the paid Options plan unthrottled.

ANALYSIS ONLY -- nothing here can place, review, or cancel an order.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from ..equity_intelligence.massive_client import Bar, MassiveClient, _bar_from_row


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def bars_to_daily_arrays(bars: list[Bar]) -> tuple[list[float], list[float], list[float], list[date]]:
    """(closes, highs, lows, dates) ascending. Skips non-positive closes."""
    rows = sorted(
        (
            (datetime.fromtimestamp(b.timestamp_ms / 1000, UTC).date(), b.close, b.high, b.low)
            for b in bars
            if b.close > 0
        ),
        key=lambda r: r[0],
    )
    return (
        [r[1] for r in rows],
        [r[2] for r in rows],
        [r[3] for r in rows],
        [r[0] for r in rows],
    )


def resample_last_per_period(closes: list[float], dates: list[date], *, weekly: bool) -> list[float]:
    """Last close per ISO week (weekly=True) or per calendar month, ascending.
    Computed locally from daily bars -- no extra API calls, and the same series
    feeds both the live read and any historical replay."""
    out: list[float] = []
    last_key: Any = None
    for close, d in zip(closes, dates):
        key = (d.isocalendar().year, d.isocalendar().week) if weekly else month_key(d)
        if key != last_key:
            out.append(close)
            last_key = key
        else:
            out[-1] = close
    return out


def monthly_end_indices(dates: list[date]) -> list[int]:
    """The DAILY index of each month's last bar, aligned 1:1 with the monthly
    closes from resample_last_per_period(weekly=False). This is what lets the
    base-rate replay truncate the daily series exactly at a month-end instead
    of assuming 21 bars per month -- the assumption that leaked future bars
    into historical classifications (found by review, 2026-09-04)."""
    out: list[int] = []
    last_key: str | None = None
    for i, d in enumerate(dates):
        key = month_key(d)
        if key != last_key:
            out.append(i)
            last_key = key
        else:
            out[-1] = i
    return out


@dataclass(frozen=True)
class FundHistory:
    """One fund's aligned price history, resampled locally."""

    symbol: str
    closes_daily: list[float]
    highs_daily: list[float]
    lows_daily: list[float]
    dates_daily: list[date]
    closes_weekly: list[float]
    closes_monthly: list[float]
    monthly_indices: list[int]  # daily index of each monthly close, 1:1 aligned
    first_date: date
    last_date: date

    @property
    def window_label(self) -> str:
        years = max((self.last_date - self.first_date).days / 365.25, 0.0)
        return f"{years:.1f}y"


class SectorDataClient(MassiveClient):
    """MassiveClient + the sector lane's extra read-only endpoints.

    Throttling is split by entitlement: STOCK-side calls ride this client's
    min_interval (~13s for the ~5/min stocks limit), while OPTIONS-side calls
    (contract reference + per-contract snapshots, the paid Options plan)
    delegate to an unthrottled twin -- spacing them would add half an hour of
    dead air to a run for no reason."""

    @property
    def _options_twin(self) -> MassiveClient:
        twin = getattr(self, "_options_client", None)
        if twin is None:
            twin = MassiveClient(api_key=self.api_key, base_url=self.base_url,
                                 transport=self._transport, sleep=self._sleep,
                                 max_retries=self._max_retries, clock=self._clock,
                                 min_interval=0.0)
            self._options_client = twin
        return twin

    def get_option_contracts(self, *args, **kwargs):  # options plan: unthrottled
        return self._options_twin.get_option_contracts(*args, **kwargs)

    def get_option_snapshot(self, *args, **kwargs):  # options plan: unthrottled
        return self._options_twin.get_option_snapshot(*args, **kwargs)

    def get_aggs_paged(
        self,
        ticker: str,
        multiplier: int,
        timespan: str,
        from_date: str,
        to_date: str,
        *,
        adjusted: bool = True,
        limit: int = 5000,
        max_pages: int = 6,
    ) -> list[Bar]:
        """Aggregates with next_url pagination followed (the base client stops
        at one page, which silently truncates on this entitlement)."""
        path = f"/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from_date}/{to_date}"
        params: dict[str, Any] = {"adjusted": str(adjusted).lower(), "sort": "asc", "limit": limit}
        bars: list[Bar] = []
        for _ in range(max_pages):
            payload = self._get(path, params)
            rows = payload.get("results") or []
            bars.extend(_bar_from_row(r) for r in rows if isinstance(r, dict))
            next_url = payload.get("next_url")
            if not next_url:
                break
            # next_url is absolute; keep only the path + cursor and re-sign
            # with our own auth header. urllib handles URL-decoding so an
            # encoded cursor is not double-encoded on the way back out.
            try:
                from urllib.parse import parse_qsl, urlsplit

                parts = urlsplit(next_url)
                path = parts.path
                params = dict(parse_qsl(parts.query))
            except Exception:
                break
        return bars

    def get_fund_history(self, symbol: str, years: int, today: date) -> FundHistory | None:
        """Daily bars back as far as the entitlement allows (requesting `years`),
        with weekly/monthly closes resampled locally."""
        frm = (today - timedelta(days=int(years * 365.25))).isoformat()
        try:
            bars = self.get_aggs_paged(symbol, 1, "day", frm, today.isoformat())
        except Exception:
            return None
        closes, highs, lows, dates = bars_to_daily_arrays(bars)
        if len(closes) < 60:
            return None
        return FundHistory(
            symbol=symbol,
            closes_daily=closes,
            highs_daily=highs,
            lows_daily=lows,
            dates_daily=dates,
            closes_weekly=resample_last_per_period(closes, dates, weekly=True),
            closes_monthly=resample_last_per_period(closes, dates, weekly=False),
            monthly_indices=monthly_end_indices(dates),
            first_date=dates[0],
            last_date=dates[-1],
        )

    # --- fundamentals (verified 200 on the current plan) ----------------------

    def get_financial_snapshot(self, ticker: str) -> dict[str, Any] | None:
        """Latest reported statements row for one ticker: EPS (diluted), book
        value per share when derivable, and the raw period metadata. Reference
        data only -- no price."""
        try:
            payload = self._get(
                "/vX/reference/financials",
                {"ticker": ticker, "limit": 2, "sort": "period_of_report_date"},
            )
        except Exception:
            return None
        rows = [r for r in (payload.get("results") or []) if isinstance(r, dict)]
        if not rows:
            return None
        latest = rows[0]
        fin = latest.get("financials") or {}
        income = fin.get("income_statement") or {}
        balance = fin.get("balance_sheet") or {}

        def _v(section: dict[str, Any], key: str) -> float | None:
            node = section.get(key)
            if isinstance(node, dict):
                try:
                    return float(node.get("value"))
                except (TypeError, ValueError):
                    return None
            return None

        return {
            "ticker": ticker,
            "fiscal_period": latest.get("fiscal_period"),
            "fiscal_year": latest.get("fiscal_year"),
            "end_date": latest.get("end_date"),
            "eps_diluted": _v(income, "diluted_earnings_per_share")
            or _v(income, "basic_earnings_per_share"),
            "equity": _v(balance, "equity")
            or _v(balance, "equity_attributable_to_parent"),
            "net_income": _v(income, "net_income_loss"),
        }

    def get_short_interest(self, ticker: str, readings: int = 3) -> list[dict[str, Any]]:
        """Most recent short-interest readings (context only, never a scoring
        input). Verified fields: short_interest, avg_daily_volume,
        days_to_cover, settlement_date."""
        try:
            payload = self._get(
                "/stocks/v1/short-interest",
                {"ticker": ticker, "limit": max(1, int(readings)), "sort": "settlement_date.desc"},
            )
        except Exception:
            return []
        return [r for r in (payload.get("results") or []) if isinstance(r, dict)]


# --- breadth cache -------------------------------------------------------------


class BreadthCache:
    """Per-day extracts of grouped-daily bars for the universe's tickers.

    One grouped-daily call returns the ENTIRE market for one past day; this
    cache keeps just the universe's constituents (a few hundred tickers) per
    day as gzipped JSON, so constituent 200-day / 52-week computations read
    from disk. Backfill is bounded per run (config) and coverage is reported
    honestly until the window fills.
    """

    def __init__(self, cache_dir: Path, tickers: set[str]) -> None:
        self.dir = cache_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tickers = {t.upper() for t in tickers}

    def _path(self, day: date) -> Path:
        return self.dir / f"{day.isoformat()}.json.gz"

    def has(self, day: date) -> bool:
        return self._path(day).exists()

    def load(self, day: date) -> dict[str, dict[str, float]] | None:
        path = self._path(day)
        if not path.exists():
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            # A truncated gzip raises EOFError; anything unreadable is a dead
            # cache entry. Delete it so the next backfill refetches the day.
            try:
                path.unlink()
            except OSError:
                pass
            return None

    def store(self, day: date, rows: list[Bar]) -> None:
        extract = {
            (b.ticker or "").upper(): {"c": b.close, "h": b.high, "l": b.low}
            for b in rows
            if b.ticker and (b.ticker or "").upper() in self.tickers and b.close > 0
        }
        # An EMPTY extract for an OLD weekday means a holiday (no session) --
        # stored so the backfill never refetches the dead day. Callers must
        # NOT store an empty extract for a recent day (see backfill): "no rows
        # yet" and "holiday" are indistinguishable until the data is final.
        # Atomic write (tmp + replace): a killed run can never leave a
        # truncated file at the canonical path.
        path = self._path(day)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as handle:
            json.dump(extract, handle)
        import os as _os

        _os.replace(tmp, path)

    def missing_days(self, today: date, lookback_days: int) -> list[date]:
        """Weekdays in the lookback window with no cache file, oldest first."""
        out: list[date] = []
        for back in range(1, lookback_days + 1):
            day = today - timedelta(days=back)
            if day.weekday() >= 5:
                continue
            if not self.has(day):
                out.append(day)
        out.reverse()
        return out

    def backfill(self, client: MassiveClient, today: date, lookback_days: int, budget: int) -> int:
        """Fetch up to `budget` missing days (throttled by the client). Returns
        how many days were fetched. Failures stop the loop -- partial coverage
        is reported, never papered over.

        A zero-row response for a RECENT day is not cached: "EOD data not
        posted yet" and "holiday" look identical, and caching the former
        would poison a real trading day forever (review finding, 2026-09-04).
        Older empty weekdays cache as holidays so they are never refetched."""
        fetched = 0
        for day in self.missing_days(today, lookback_days):
            if fetched >= budget:
                break
            try:
                rows = client.get_grouped_daily(day.isoformat())
            except Exception:
                break
            fetched += 1  # the API call happened; it spends the budget
            if not rows and (today - day).days < 3:
                continue  # too recent to declare a holiday; refetch next run
            self.store(day, rows)
        return fetched

    def constituent_series(self, today: date, lookback_days: int) -> dict[str, list[tuple[date, float]]]:
        """Per-ticker (date, close) series ascending from the cached days."""
        series: dict[str, list[tuple[date, float]]] = {}
        for back in range(lookback_days, 0, -1):
            day = today - timedelta(days=back)
            if day.weekday() >= 5:
                continue
            rows = self.load(day)
            if not rows:
                continue
            for ticker, ohlc in rows.items():
                series.setdefault(ticker, []).append((day, float(ohlc.get("c", 0.0))))
        return series

    def coverage(self, today: date, lookback_days: int) -> float:
        """Fraction of lookback weekdays present in the cache, 0..1."""
        weekdays = [
            today - timedelta(days=b)
            for b in range(1, lookback_days + 1)
            if (today - timedelta(days=b)).weekday() < 5
        ]
        if not weekdays:
            return 0.0
        have = sum(1 for d in weekdays if self.has(d))
        return have / len(weekdays)
