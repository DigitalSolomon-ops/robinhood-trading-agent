"""Real Massive historical daily bars as the proving-run / backtest price series.

WHY THIS EXISTS
    The equities lane's paper proving runs used to be priced off a locally
    generated price series -- a deterministic drift that produced fills and a
    clean reconcile, and proved nothing about the lane against a real market.
    The audit named it for what it was: a synthetic feed sold as real quotes.
    This module replaces that feed with the genuine article -- up to two years
    of REAL adjusted daily bars pulled from Massive (formerly Polygon.io)
    through massive_client -- and, just as importantly, makes the price series
    say out loud where it came from so the readiness gate can refuse anything
    else.

WHAT IT IS NOT
    It is not an execution price source. A real order still prices off the
    Robinhood OAuth connector's own quote at order time; nothing here ever
    reaches the order path. Massive is end-of-day/delayed data -- correct for
    deciding and for proving, wrong for filling.

HOW IT FEEDS A PROVING RUN
    `MassiveHistoryFeed` loads one bar series per symbol once, then replays it
    one bar per `get_prices()` call, which is exactly the cadence
    run_equity_cycle reads quotes at. A bounded loop of N iterations therefore
    walks the first N real trading days of the window. When a symbol's series
    runs out the feed stops offering it a price, and the cycle logs its
    ordinary "no quote available" rationale -- it never wraps around, and it
    never invents a bar to keep the loop fed.

FAIL-CLOSED
    A symbol that comes back with no bars raises. There is deliberately no
    fallback to a generated series: silently substituting one is the exact
    defect this module exists to remove.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Iterable, Sequence

from .massive_client import Bar, MassiveClient

# The name a price series records itself under. The readiness gate asserts a
# proving run carries exactly this, so a run priced any other way cannot count.
QUOTE_SOURCE = "massive"

# Two years of daily bars -- Massive's free-tier history window, and enough
# trading days (~500) that a bounded proving loop never exhausts it.
MAX_LOOKBACK_DAYS = 730

# The vendor endpoint the bars come from, recorded in the provenance so the
# evidence names a real API path rather than the word "real".
BARS_ENDPOINT = "/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}"


class MassiveHistoryUnavailable(RuntimeError):
    """Raised when the real bar series a proving run needs could not be read.

    Raised rather than degraded on purpose: a proving run with no real history
    must stop, not quietly fall back to a generated series.
    """


def _iso_day(timestamp_ms: int) -> str:
    if not timestamp_ms:
        return ""
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).date().isoformat()


@dataclass(frozen=True)
class SeriesProvenance:
    """Where one symbol's replayed series actually came from."""

    symbol: str
    bars: int
    first_day: str
    last_day: str

    def as_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "bars": self.bars, "first_day": self.first_day, "last_day": self.last_day}


class MassiveHistoryFeed:
    """A replayable price series built from real Massive daily bars.

    Shaped to be droppable into run_equity_cycle wherever the connector quote
    read would otherwise go: it exposes `quote_source_name` and
    `get_prices(symbols, logger=...)`, and nothing else about it is special.
    """

    quote_source_name = QUOTE_SOURCE

    def __init__(
        self,
        client: MassiveClient,
        symbols: Sequence[str],
        lookback_days: int = MAX_LOOKBACK_DAYS,
        today: date | None = None,
        adjusted: bool = True,
    ) -> None:
        if lookback_days < 1:
            raise ValueError("lookback_days must be at least 1 day of history")
        self.client = client
        self.symbols = [str(symbol).upper() for symbol in symbols]
        self.lookback_days = min(int(lookback_days), MAX_LOOKBACK_DAYS)
        self.adjusted = adjusted
        self.to_date = today or datetime.now(UTC).date()
        self.from_date = self.to_date - timedelta(days=self.lookback_days)
        self._bars: dict[str, tuple[Bar, ...]] = {}
        self._cursor: dict[str, int] = {}

    # --- loading -----------------------------------------------------------

    def load(self) -> dict[str, tuple[Bar, ...]]:
        """Read every symbol's real daily bars once, and keep them.

        Raises MassiveHistoryUnavailable if any symbol comes back empty --
        a proving run priced on a partial universe is not the run it claims.
        """
        if self._bars:
            return self._bars
        loaded: dict[str, tuple[Bar, ...]] = {}
        empty: list[str] = []
        for symbol in self.symbols:
            bars = tuple(
                bar
                for bar in self.client.get_daily_bars(
                    symbol,
                    self.from_date.isoformat(),
                    self.to_date.isoformat(),
                    adjusted=self.adjusted,
                )
                if bar.close > 0
            )
            if not bars:
                empty.append(symbol)
                continue
            loaded[symbol] = bars
        if empty:
            raise MassiveHistoryUnavailable(
                "no real daily bars returned for "
                + ", ".join(empty)
                + f" between {self.from_date.isoformat()} and {self.to_date.isoformat()}; "
                "a proving run will not substitute a generated series"
            )
        self._bars = loaded
        self._cursor = dict.fromkeys(loaded, 0)
        return self._bars

    # --- backtest view ------------------------------------------------------

    def bars(self, symbol: str) -> tuple[Bar, ...]:
        return self.load().get(str(symbol).upper(), ())

    def closes(self, symbol: str) -> list[float]:
        """The symbol's real adjusted closes, oldest first -- the series a
        backtest walks."""
        return [bar.close for bar in self.bars(symbol)]

    def backtest_series(self) -> dict[str, list[float]]:
        """Every symbol's real close series, for a backtest that wants the
        whole window at once rather than one bar per cycle."""
        return {symbol: [bar.close for bar in bars] for symbol, bars in self.load().items()}

    # --- replay view --------------------------------------------------------

    def remaining(self, symbol: str) -> int:
        symbol = str(symbol).upper()
        return max(0, len(self._bars.get(symbol, ())) - self._cursor.get(symbol, 0))

    @property
    def exhausted(self) -> bool:
        self.load()
        return all(self.remaining(symbol) == 0 for symbol in self._bars)

    def get_prices(self, symbols: Iterable[str], logger: Any | None = None) -> dict[str, float]:
        """The next real close for each requested symbol, one bar per call.

        Signature-compatible with EquityMarketDataService.get_latest_prices so
        run_equity_cycle can read from either without knowing which it holds.
        A symbol whose series is spent is simply absent from the result, and
        the caller's ordinary missing-quote rationale covers it.
        """
        self.load()
        prices: dict[str, float] = {}
        for raw in symbols:
            symbol = str(raw).upper()
            series = self._bars.get(symbol, ())
            index = self._cursor.get(symbol, 0)
            if index >= len(series):
                if logger is not None:
                    logger.log_decision(
                        symbol,
                        "equity_history_exhausted",
                        f"{symbol} has no unreplayed {QUOTE_SOURCE} daily bar left in the "
                        f"{self.from_date.isoformat()}..{self.to_date.isoformat()} window; "
                        f"the series is not wrapped or extended",
                        {"quote_source": QUOTE_SOURCE, "bars": len(series)},
                    )
                continue
            self._cursor[symbol] = index + 1
            prices[symbol] = series[index].close
        return prices

    # --- provenance ---------------------------------------------------------

    def series_provenance(self) -> list[SeriesProvenance]:
        return [
            SeriesProvenance(
                symbol=symbol,
                bars=len(bars),
                first_day=_iso_day(bars[0].timestamp_ms),
                last_day=_iso_day(bars[-1].timestamp_ms),
            )
            for symbol, bars in sorted(self.load().items())
        ]

    def provenance(self) -> dict[str, Any]:
        """The audit record of where these prices came from.

        Written into the proving run's audit trail so the readiness evidence
        reads a claim with a vendor, an endpoint and a date range behind it,
        not just the word "real".
        """
        series = self.series_provenance()
        return {
            "quote_source": QUOTE_SOURCE,
            "vendor": "Massive (formerly Polygon.io)",
            "endpoint": BARS_ENDPOINT,
            "adjusted": self.adjusted,
            "lookback_days": self.lookback_days,
            "from_date": self.from_date.isoformat(),
            "to_date": self.to_date.isoformat(),
            "symbols": [entry.symbol for entry in series],
            "series": [entry.as_dict() for entry in series],
            "total_bars": sum(entry.bars for entry in series),
            "execution_price_note": (
                "decision/proving prices only; a real order prices off the Robinhood connector quote"
            ),
        }

    def describe(self) -> str:
        """A one-line, human-readable rationale for the audit log."""
        series = self.series_provenance()
        spans = ", ".join(f"{entry.symbol} {entry.bars} bars {entry.first_day}..{entry.last_day}" for entry in series)
        return (
            f"priced from real Massive adjusted daily bars ({self.lookback_days}d window "
            f"{self.from_date.isoformat()}..{self.to_date.isoformat()}): {spans or 'no series loaded'}"
        )


def load_backtest_series(
    client: MassiveClient,
    symbols: Sequence[str],
    lookback_days: int = MAX_LOOKBACK_DAYS,
    today: date | None = None,
) -> tuple[dict[str, list[float]], dict[str, Any]]:
    """Real close series per symbol, plus the provenance that names their source.

    The backtest counterpart to the replay feed: same bars, handed over whole.
    """
    feed = MassiveHistoryFeed(client, symbols, lookback_days=lookback_days, today=today)
    return feed.backtest_series(), feed.provenance()
