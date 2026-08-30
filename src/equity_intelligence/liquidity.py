"""Massive daily volume/price as the LIQUIDITY + STILL-TRADING half of the
equities lane's tradability gate.

What this module is
-------------------
A per-symbol screen answering two questions the connector's own quote cannot:
is this name still actually trading (are there recent daily bars at all, and how
recent is the latest one), and is it liquid enough that a small long-only order
is not the whole tape. It reads real daily bars through the read-only Massive
client and returns a verdict plus the numbers behind it, so the skip rationale
quotes an average dollar volume rather than asserting "illiquid".

Every threshold comes from `equities.liquidity:` in config/trading_rules.yaml.
It lives in the rules file rather than strategy.yaml because it is a risk cap,
not a signal-generation input.

Where it is used
----------------
src/equity_symbols.py's `validate_equity_symbols` -- the tradability gate that
the audit found was dead code. That function already read the connector quote
for a halted/delisted/suspended state; this module is the second half of the
same gate, and `src/equity_runtime.py:run_equity_cycle` now runs the whole
thing on every cycle, skipping any name that fails either half with a logged
rationale.

What this module is NOT
-----------------------
It is not an execution path and it cannot become one:

* nothing here imports OrderManager, RiskManager, the kill switch, or any
  broker, and nothing here can name a connector order tool. It only ever
  REMOVES a symbol from the list the existing gates are then asked about; the
  gates run afterwards, unchanged and in the same order.
* it is one-directional. The only outcomes are "leave this symbol alone" and
  "skip this symbol", so it can never add a name to the universe, enlarge a
  position, or shorten the path to a fill. A symbol it passes still has to
  clear every risk gate, the kill switch and the human confirm-flag.
* it is not a substitute for a risk gate, and it never blocks an EXIT -- the
  gate filters which symbols are EVALUATED; a position already open is closed
  by the strategy's own sell path regardless of what this screen says.

Daily bars are end-of-day data. Liquidity is a decision input, never an
execution-timing or pricing input -- the Robinhood connector stays the sole
source of execution-time price.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from statistics import fmean
from typing import Any, Protocol, Sequence

SOURCE = "massive_liquidity"

# Every threshold below is a DEFAULT that `equities.liquidity:` in
# config/trading_rules.yaml overrides key by key.
DEFAULTS: dict[str, Any] = {
    # Off unless a config turns it on: the lane's read-only-by-default posture
    # extends to reaching out to a data vendor at all.
    "enabled": False,
    # Fail-closed opt-in. When true, a symbol whose bars could not be read is
    # skipped rather than passed through on the connector quote alone.
    "require_liquidity": False,
    # The window of daily bars to average over.
    "lookback_days": 45,
    # Fewer sessions than this inside the window is not "actively trading".
    "min_bars": 10,
    # The latest bar being older than this many calendar days is how a halted,
    # suspended or delisted name shows up in end-of-day data.
    "max_stale_days": 5,
    # Sub-$3 names are out of scope for this long-only lane.
    "min_close_price": 3.0,
    # Average shares and average notional traded per session in the window.
    "min_average_volume": 300000.0,
    "min_average_dollar_volume": 5000000.0,
}


def liquidity_config(trading_rules: dict[str, Any]) -> dict[str, Any]:
    """DEFAULTS with `equities.liquidity:` from config/trading_rules.yaml merged
    over it, one level deep (so a config can override a single threshold without
    restating the whole section)."""
    override = (trading_rules.get("equities") or {}).get("liquidity") or {}
    merged: dict[str, Any] = {}
    for key, value in DEFAULTS.items():
        if isinstance(value, dict):
            merged[key] = {**value, **(override.get(key) or {})}
        else:
            merged[key] = override.get(key, value)
    for key, value in override.items():
        if key not in merged:
            merged[key] = value
    return merged


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:,.0f}"


def _iso_day(timestamp_ms: int) -> str:
    if not timestamp_ms:
        return ""
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).date().isoformat()


@dataclass(frozen=True)
class LiquiditySnapshot:
    """What the daily bars say about one symbol's recent trading.

    `error` carries a human-readable reason the read failed; when it is set the
    numeric fields are all None and the verdict degrades to a pass-through (or
    to a skip under require_liquidity).
    """

    symbol: str
    source: str = SOURCE
    bars: int = 0
    first_day: str = ""
    last_day: str = ""
    stale_days: int | None = None
    last_close: float | None = None
    average_volume: float | None = None
    average_dollar_volume: float | None = None
    error: str | None = None

    def values(self) -> tuple[tuple[str, float], ...]:
        """Every reading actually taken, as audit-friendly (name, value) pairs."""
        pairs: list[tuple[str, float]] = [("bars", float(self.bars))]
        if self.last_close is not None:
            pairs.append(("last_close", self.last_close))
        if self.average_volume is not None:
            pairs.append(("average_volume", self.average_volume))
        if self.average_dollar_volume is not None:
            pairs.append(("average_dollar_volume", self.average_dollar_volume))
        if self.stale_days is not None:
            pairs.append(("stale_days", float(self.stale_days)))
        return tuple(pairs)

    def citation(self) -> str:
        """The numbers themselves, rendered for the decision rationale."""
        if self.error:
            return f"{self.source} unavailable for {self.symbol} ({self.error})"
        if not self.bars:
            return f"{self.source} returned no daily bars for {self.symbol}"
        stale = "unknown" if self.stale_days is None else f"{self.stale_days}d"
        close = "n/a" if self.last_close is None else f"{self.last_close:,.2f}"
        return (
            f"{self.source} {self.symbol}: {self.bars} session(s) "
            f"{self.first_day}..{self.last_day} (last bar {stale} old), "
            f"last close {close}, average volume {_fmt(self.average_volume)}, "
            f"average dollar volume {_fmt(self.average_dollar_volume)}"
        )


@dataclass(frozen=True)
class LiquidityVerdict:
    """Whether this symbol may be evaluated at all this cycle, and why."""

    symbol: str
    tradable: bool
    notes: tuple[str, ...]
    snapshot: LiquiditySnapshot | None

    def rationale(self) -> str:
        """One readable clause naming every value used and what it was taken to
        mean."""
        citation = self.snapshot.citation() if self.snapshot is not None else "no liquidity data"
        interpretation = "; ".join(self.notes) if self.notes else "no interpretation applied"
        return f"liquidity[{citation}] -> {interpretation}"


class LiquidityProvider(Protocol):
    """Anything that can hand the tradability gate a liquidity snapshot.

    A Protocol on purpose: tests inject a stub, and the real implementation
    (MassiveLiquidityProvider) is read-only by construction.
    """

    def snapshot(self, symbol: str) -> LiquiditySnapshot: ...


def summarize_liquidity(symbol: str, bars: Sequence[Any], today: date | None = None) -> LiquiditySnapshot:
    """Average the real daily bars for one symbol.

    Pure: no i/o. Bars with a non-positive close are dropped -- they are not
    sessions this name traded in -- and `stale_days` is measured from the most
    recent surviving bar, which is how an end-of-day feed reveals a name that
    stopped trading.
    """
    today = today or datetime.now(UTC).date()
    usable = []
    for bar in bars:
        try:
            close = float(getattr(bar, "close", 0) or 0)
            volume = float(getattr(bar, "volume", 0) or 0)
            stamp = int(getattr(bar, "timestamp_ms", 0) or 0)
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        usable.append((stamp, close, max(volume, 0.0)))
    if not usable:
        return LiquiditySnapshot(symbol=symbol, bars=0)
    usable.sort(key=lambda row: row[0])
    first_day = _iso_day(usable[0][0])
    last_day = _iso_day(usable[-1][0])
    stale_days: int | None = None
    if last_day:
        stale_days = max((today - date.fromisoformat(last_day)).days, 0)
    return LiquiditySnapshot(
        symbol=symbol,
        bars=len(usable),
        first_day=first_day,
        last_day=last_day,
        stale_days=stale_days,
        last_close=usable[-1][1],
        average_volume=fmean(row[2] for row in usable),
        average_dollar_volume=fmean(row[1] * row[2] for row in usable),
    )


class MassiveLiquidityProvider:
    """Reads the configured window of daily bars per symbol from MassiveClient.

    Read-only: the only client method it can reach is get_daily_bars. Any
    failure (missing api key, rate limit, http error) is caught and returned as
    a snapshot with `error` set -- a data feed going down must degrade the
    screen, never crash the lane and never place anything. Readings are cached
    per symbol for the life of the provider, since end-of-day bars for a
    completed session cannot change again.
    """

    def __init__(self, client: Any, config: dict[str, Any], today: date | None = None) -> None:
        self.client = client
        self.config = config
        self.today = today or datetime.now(UTC).date()
        self._cache: dict[str, LiquiditySnapshot] = {}

    def snapshot(self, symbol: str) -> LiquiditySnapshot:
        symbol = str(symbol).upper()
        cached = self._cache.get(symbol)
        if cached is not None:
            return cached
        lookback = max(int(self.config.get("lookback_days", 45) or 1), 1)
        from_date = (self.today - timedelta(days=lookback)).isoformat()
        try:
            bars = self.client.get_daily_bars(symbol, from_date, self.today.isoformat())
        except Exception as exc:  # noqa: BLE001 -- a data outage degrades, never crashes
            snapshot = LiquiditySnapshot(symbol=symbol, error=f"{type(exc).__name__}: {exc}")
            self._cache[symbol] = snapshot
            return snapshot
        snapshot = summarize_liquidity(symbol, list(bars or []), today=self.today)
        self._cache[symbol] = snapshot
        return snapshot


def build_liquidity_provider(trading_rules: dict[str, Any], client_factory: Any = None) -> LiquidityProvider | None:
    """A provider when `equities.liquidity.enabled` is true, else None.

    Disabled is the default, so a lane that has not opted in never reaches a
    data vendor at all and the tradability gate is the connector-quote check
    alone, exactly as before.
    """
    config = liquidity_config(trading_rules)
    if not config.get("enabled", False):
        return None
    if client_factory is None:
        from .massive_client import MassiveClient

        client_factory = MassiveClient
    return MassiveLiquidityProvider(client_factory(), config)


def evaluate_liquidity(
    snapshot: LiquiditySnapshot | None, config: dict[str, Any]
) -> LiquidityVerdict:
    """Turn a liquidity snapshot into a keep-or-skip verdict on one symbol.

    Pure: no i/o, no config mutation, no order path. The only two outcomes are
    "tradable" and "skipped", and it always returns the notes that justify the
    one it picked.
    """
    symbol = snapshot.symbol if snapshot is not None else ""
    if not config.get("enabled", False):
        note = "liquidity screen is disabled in config; connector quote state is the only tradability check"
        return LiquidityVerdict(symbol, True, (note,), snapshot)

    if snapshot is None or snapshot.error:
        detail = snapshot.error if snapshot is not None else "no liquidity provider configured"
        if config.get("require_liquidity", False):
            note = f"liquidity data required but unavailable ({detail}); symbol skipped"
            return LiquidityVerdict(symbol, False, (note,), snapshot)
        note = f"liquidity data unavailable ({detail}); symbol left to the connector quote check alone"
        return LiquidityVerdict(symbol, True, (note,), snapshot)

    failures: list[str] = []

    min_bars = int(config.get("min_bars", 0) or 0)
    if snapshot.bars < min_bars:
        failures.append(
            f"only {snapshot.bars} session(s) of daily bars, below min_bars {min_bars} "
            f"-> not actively trading"
        )

    max_stale = int(config.get("max_stale_days", 0) or 0)
    if max_stale > 0 and (snapshot.stale_days is None or snapshot.stale_days > max_stale):
        stale = "unknown" if snapshot.stale_days is None else f"{snapshot.stale_days}d"
        failures.append(
            f"latest daily bar is {stale} old ({snapshot.last_day or 'no dated bar'}), "
            f"beyond max_stale_days {max_stale} -> halted, suspended or delisted"
        )

    floor_price = float(config.get("min_close_price", 0) or 0)
    if floor_price > 0 and (snapshot.last_close is None or snapshot.last_close < floor_price):
        close = "n/a" if snapshot.last_close is None else f"{snapshot.last_close:,.2f}"
        failures.append(f"last close {close} below min_close_price {floor_price:,.2f}")

    min_volume = float(config.get("min_average_volume", 0) or 0)
    if min_volume > 0 and (snapshot.average_volume is None or snapshot.average_volume < min_volume):
        failures.append(
            f"average volume {_fmt(snapshot.average_volume)} below min_average_volume {_fmt(min_volume)}"
        )

    min_dollars = float(config.get("min_average_dollar_volume", 0) or 0)
    if min_dollars > 0 and (
        snapshot.average_dollar_volume is None or snapshot.average_dollar_volume < min_dollars
    ):
        failures.append(
            f"average dollar volume {_fmt(snapshot.average_dollar_volume)} below "
            f"min_average_dollar_volume {_fmt(min_dollars)}"
        )

    if failures:
        return LiquidityVerdict(symbol, False, tuple(failures + ["symbol skipped this cycle"]), snapshot)

    note = (
        f"{snapshot.bars} recent session(s), last bar "
        f"{'unknown' if snapshot.stale_days is None else str(snapshot.stale_days) + 'd'} old, "
        f"average dollar volume {_fmt(snapshot.average_dollar_volume)} -> actively trading and liquid"
    )
    return LiquidityVerdict(symbol, True, (note,), snapshot)
