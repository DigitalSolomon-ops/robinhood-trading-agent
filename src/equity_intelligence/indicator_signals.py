"""Massive EOD technical indicators as SIGNAL INPUTS for the equities lane.

What this module is
-------------------
It reads SMA/EMA/RSI/MACD from the read-only Massive client and turns them
into a MODULATION of a rules-based signal that StrategyEngine has already
produced: keep it, downweight its confidence, or skip the entry entirely.
Every threshold comes from `equity_indicators:` in config/strategy.yaml --
nothing here is hardcoded except the defaults that section overrides.

What this module is NOT
-----------------------
It is not an execution path and it cannot become one:

* nothing here imports OrderManager, RiskManager, the kill switch, or any
  broker, and nothing here has a reference to a connector order tool. An
  indicator can only change WHAT SIGNAL is handed to the existing gates; the
  gates themselves are untouched and still run afterwards, in order.
* modulation is one-directional. `apply_modulation` may move an actionable
  side to "hold" and may lower confidence, but it can never invent a side --
  a rules "hold" stays a hold, a "sell" stays a sell. So no indicator reading
  can conjure an order that the rules did not already ask for, and the human
  confirm-flag / dry_run gates in RobinhoodEquityBroker are downstream of all
  of this regardless.
* EXITS ARE NEVER MODULATED. Indicators gate entries only. Downweighting a
  sell would trap a position behind a data feed, which is the opposite of a
  risk control, so a "sell" signal is passed through unchanged and merely
  ANNOTATED with the indicator values for the audit rationale.

The data is EOD/delayed by construction (Massive daily bars). It is a
signal-generation input, never an execution-timing or pricing input -- the
Robinhood connector stays the sole source of execution-time price.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Protocol

SOURCE = "massive"

# Every threshold below is a DEFAULT that config/strategy.yaml's
# `equity_indicators:` section overrides key by key.
DEFAULTS: dict[str, Any] = {
    # Off unless a config turns it on: the lane's read-only-by-default posture
    # extends to reaching out to a second data vendor at all.
    "enabled": False,
    # When true, a buy whose indicator data could not be read is SKIPPED rather
    # than passed through unmodulated. Fail-closed opt-in.
    "require_indicators": False,
    "max_confidence": 0.95,
    # A modulated confidence below this floor downgrades the entry to a hold.
    "min_confidence_to_act": 0.35,
    "trend": {
        "series": "sma",  # sma | ema
        "fast_window": 50,
        "slow_window": 200,
        "on_downtrend": "skip",  # skip | downweight
        "downtrend_confidence_multiplier": 0.5,
        "uptrend_confidence_multiplier": 1.1,
    },
    "rsi": {
        "window": 14,
        "oversold": 30.0,
        "overbought": 70.0,
        "strongly_overbought": 80.0,
        "on_overbought": "downweight",  # skip | downweight
        "on_strongly_overbought": "skip",  # skip | downweight
        "overbought_confidence_multiplier": 0.5,
        "oversold_confidence_multiplier": 1.15,
    },
    "macd": {
        "short_window": 12,
        "long_window": 26,
        "signal_window": 9,
        "on_bearish_cross": "downweight",  # skip | downweight
        "bearish_confidence_multiplier": 0.6,
        "bullish_confidence_multiplier": 1.1,
    },
}

# Modulation outcomes.
ALLOW = "allow"
DOWNWEIGHT = "downweight"
SKIP = "skip"
NOT_APPLICABLE = "not_applicable"  # signal is not an entry -- annotate only
UNAVAILABLE = "unavailable"  # no indicator data; rules signal passed through


def indicator_config(strategy_config: dict[str, Any]) -> dict[str, Any]:
    """DEFAULTS with `equity_indicators:` from config/strategy.yaml merged over
    it, one level deep (so a config can override a single RSI band without
    restating the whole section)."""
    override = strategy_config.get("equity_indicators") or {}
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
    return "n/a" if value is None else f"{value:.2f}"


@dataclass(frozen=True)
class IndicatorSnapshot:
    """The latest EOD indicator readings for one symbol.

    `error` carries a human-readable reason the read failed; when it is set,
    the numeric fields are all None and the modulation degrades to
    UNAVAILABLE (or SKIP under require_indicators).
    """

    symbol: str
    source: str = SOURCE
    trend_fast: float | None = None
    trend_slow: float | None = None
    trend_series: str = "sma"
    trend_fast_window: int = 50
    trend_slow_window: int = 200
    rsi: float | None = None
    rsi_window: int = 14
    macd: float | None = None
    macd_signal: float | None = None
    macd_histogram: float | None = None
    as_of_ms: int | None = None
    error: str | None = None

    @property
    def trend_fast_label(self) -> str:
        return f"{self.trend_series.upper()}{self.trend_fast_window}"

    @property
    def trend_slow_label(self) -> str:
        return f"{self.trend_series.upper()}{self.trend_slow_window}"

    def values(self) -> tuple[tuple[str, float], ...]:
        """Every indicator value actually read, as an audit-friendly tuple of
        (name, value) pairs. Frozen/hashable so it can ride on a TradeSignal."""
        pairs: list[tuple[str, float]] = []
        if self.trend_fast is not None:
            pairs.append((self.trend_fast_label, self.trend_fast))
        if self.trend_slow is not None:
            pairs.append((self.trend_slow_label, self.trend_slow))
        if self.rsi is not None:
            pairs.append((f"RSI{self.rsi_window}", self.rsi))
        if self.macd is not None:
            pairs.append(("MACD", self.macd))
        if self.macd_signal is not None:
            pairs.append(("MACD_signal", self.macd_signal))
        if self.macd_histogram is not None:
            pairs.append(("MACD_hist", self.macd_histogram))
        return tuple(pairs)

    def citation(self) -> str:
        """The values themselves, rendered for the decision rationale."""
        if self.error:
            return f"{self.source} indicators unavailable ({self.error})"
        pairs = self.values()
        if not pairs:
            return f"{self.source} indicators returned no values"
        return f"{self.source} " + ", ".join(f"{name}={_fmt(value)}" for name, value in pairs)


@dataclass(frozen=True)
class IndicatorModulation:
    """How the indicators change (or do not change) a rules-based signal."""

    action: str
    confidence_multiplier: float
    notes: tuple[str, ...]
    snapshot: IndicatorSnapshot | None

    @property
    def skipped(self) -> bool:
        return self.action == SKIP

    def rationale(self) -> str:
        """One readable clause naming every indicator value used and what it
        was taken to mean."""
        citation = self.snapshot.citation() if self.snapshot is not None else "no indicator data"
        interpretation = "; ".join(self.notes) if self.notes else "no interpretation applied"
        return f"indicators[{citation}] -> {interpretation}"


class IndicatorProvider(Protocol):
    """Anything that can hand the strategy an EOD snapshot for a symbol.

    A Protocol on purpose: tests inject a stub, and the real implementation
    (MassiveIndicatorProvider) is read-only by construction.
    """

    def snapshot(self, symbol: str) -> IndicatorSnapshot: ...


class MassiveIndicatorProvider:
    """Reads the configured SMA/EMA + RSI + MACD windows from MassiveClient.

    Read-only: the only client methods it can reach are get_sma/get_ema/
    get_rsi/get_macd. Any failure (missing API key, rate limit, HTTP error) is
    caught and returned as a snapshot with `error` set -- an indicator feed
    going down must degrade the signal, never crash the lane and never place
    anything.
    """

    def __init__(self, client: Any, config: dict[str, Any]) -> None:
        self.client = client
        self.config = config
        self._cache: dict[str, IndicatorSnapshot] = {}

    @staticmethod
    def _latest(points: list[Any]) -> Any | None:
        # The client requests order="desc", so the newest point is first.
        return points[0] if points else None

    def snapshot(self, symbol: str) -> IndicatorSnapshot:
        cached = self._cache.get(symbol)
        if cached is not None:
            return cached
        trend = self.config["trend"]
        rsi_config = self.config["rsi"]
        macd_config = self.config["macd"]
        series = str(trend.get("series", "sma")).lower()
        fast_window = int(trend["fast_window"])
        slow_window = int(trend["slow_window"])
        rsi_window = int(rsi_config["window"])
        base = IndicatorSnapshot(
            symbol=symbol,
            trend_series=series,
            trend_fast_window=fast_window,
            trend_slow_window=slow_window,
            rsi_window=rsi_window,
        )
        try:
            read = self.client.get_ema if series == "ema" else self.client.get_sma
            fast = self._latest(read(symbol, window=fast_window, order="desc"))
            slow = self._latest(read(symbol, window=slow_window, order="desc"))
            rsi = self._latest(self.client.get_rsi(symbol, window=rsi_window, order="desc"))
            macd = self._latest(
                self.client.get_macd(
                    symbol,
                    short_window=int(macd_config["short_window"]),
                    long_window=int(macd_config["long_window"]),
                    signal_window=int(macd_config["signal_window"]),
                    order="desc",
                )
            )
        except Exception as exc:  # noqa: BLE001 -- a data outage degrades, never crashes
            snapshot = replace(base, error=f"{type(exc).__name__}: {exc}")
            self._cache[symbol] = snapshot
            return snapshot

        stamps = [point.timestamp_ms for point in (fast, slow, rsi, macd) if point is not None]
        snapshot = replace(
            base,
            trend_fast=fast.value if fast is not None else None,
            trend_slow=slow.value if slow is not None else None,
            rsi=rsi.value if rsi is not None else None,
            macd=macd.value if macd is not None else None,
            macd_signal=macd.signal if macd is not None else None,
            macd_histogram=macd.histogram if macd is not None else None,
            as_of_ms=max(stamps) if stamps else None,
        )
        self._cache[symbol] = snapshot
        return snapshot


def build_indicator_provider(strategy_config: dict[str, Any], client_factory: Any = None) -> IndicatorProvider | None:
    """A provider when `equity_indicators.enabled` is true, else None.

    Disabled is the default, so a lane that has not opted in never reaches a
    second data vendor at all and the strategy behaves exactly as before.
    """
    config = indicator_config(strategy_config)
    if not config.get("enabled", False):
        return None
    if client_factory is None:
        from .massive_client import MassiveClient

        client_factory = MassiveClient
    return MassiveIndicatorProvider(client_factory(), config)


def modulated_confidence(base_confidence: float, modulation: IndicatorModulation, config: dict[str, Any]) -> float:
    """The rules confidence after the indicator multiplier, clamped to
    [0, max_confidence]. Confidence is informational -- no risk gate reads it
    -- so this can only ever change what the rationale says, not what the
    gates decide."""
    ceiling = float(config.get("max_confidence", 0.95))
    return min(max(base_confidence * modulation.confidence_multiplier, 0.0), ceiling)


def _component_verdict(mode: str, multiplier: float) -> tuple[str, float]:
    """A component that fired either skips the entry or downweights it."""
    if str(mode).lower() == "skip":
        return SKIP, multiplier
    return DOWNWEIGHT, multiplier


def evaluate_indicators(
    snapshot: IndicatorSnapshot | None,
    base_side: str,
    base_confidence: float,
    config: dict[str, Any],
) -> IndicatorModulation:
    """Turn a snapshot into a modulation of an already-decided rules signal.

    Pure: no I/O, no config mutation, no order path. Returns SKIP or a
    confidence multiplier, and always returns the notes that justify it.
    """
    if base_side != "buy":
        # Exits and holds are annotated, never modulated -- see module docstring.
        notes = ("indicators modulate entries only; this non-entry signal is passed through unchanged",)
        return IndicatorModulation(NOT_APPLICABLE, 1.0, notes, snapshot)

    if snapshot is None or snapshot.error:
        detail = snapshot.error if snapshot is not None else "no indicator provider configured"
        if config.get("require_indicators", False):
            note = f"indicator data required but unavailable ({detail}); entry skipped"
            return IndicatorModulation(SKIP, 1.0, (note,), snapshot)
        note = f"indicator data unavailable ({detail}); rules-based signal passed through unmodulated"
        return IndicatorModulation(UNAVAILABLE, 1.0, (note,), snapshot)

    trend = config["trend"]
    rsi_config = config["rsi"]
    macd_config = config["macd"]

    notes: list[str] = []
    multiplier = 1.0
    verdicts: list[str] = []

    def record(verdict: str, factor: float) -> None:
        nonlocal multiplier
        verdicts.append(verdict)
        multiplier *= float(factor)

    # --- trend filter (fast vs slow moving average) -------------------------
    fast_label, slow_label = snapshot.trend_fast_label, snapshot.trend_slow_label
    if snapshot.trend_fast is None or snapshot.trend_slow is None:
        notes.append(f"trend filter incomplete ({fast_label}={_fmt(snapshot.trend_fast)}, {slow_label}={_fmt(snapshot.trend_slow)})")
        if config.get("require_indicators", False):
            record(SKIP, 1.0)
    elif snapshot.trend_fast > snapshot.trend_slow:
        factor = float(trend["uptrend_confidence_multiplier"])
        notes.append(f"{fast_label}={_fmt(snapshot.trend_fast)} above {slow_label}={_fmt(snapshot.trend_slow)} -> uptrend (x{factor:.2f})")
        record(ALLOW, factor)
    elif snapshot.trend_fast < snapshot.trend_slow:
        verdict, factor = _component_verdict(trend["on_downtrend"], float(trend["downtrend_confidence_multiplier"]))
        outcome = "entry skipped" if verdict == SKIP else f"downweighted (x{factor:.2f})"
        notes.append(f"{fast_label}={_fmt(snapshot.trend_fast)} below {slow_label}={_fmt(snapshot.trend_slow)} -> downtrend, {outcome}")
        record(verdict, 1.0 if verdict == SKIP else factor)
    else:
        notes.append(f"{fast_label} equals {slow_label} at {_fmt(snapshot.trend_fast)} -> flat trend, no adjustment")

    # --- RSI bands ----------------------------------------------------------
    rsi_label = f"RSI{snapshot.rsi_window}"
    if snapshot.rsi is None:
        notes.append(f"{rsi_label} unavailable")
        if config.get("require_indicators", False):
            record(SKIP, 1.0)
    else:
        strongly_overbought = float(rsi_config["strongly_overbought"])
        overbought = float(rsi_config["overbought"])
        oversold = float(rsi_config["oversold"])
        if snapshot.rsi >= strongly_overbought:
            verdict, factor = _component_verdict(
                rsi_config["on_strongly_overbought"], float(rsi_config["overbought_confidence_multiplier"])
            )
            outcome = "entry skipped" if verdict == SKIP else f"downweighted (x{factor:.2f})"
            notes.append(f"{rsi_label}={_fmt(snapshot.rsi)} at/above strongly-overbought {strongly_overbought:.0f} -> {outcome}")
            record(verdict, 1.0 if verdict == SKIP else factor)
        elif snapshot.rsi >= overbought:
            verdict, factor = _component_verdict(
                rsi_config["on_overbought"], float(rsi_config["overbought_confidence_multiplier"])
            )
            outcome = "entry skipped" if verdict == SKIP else f"downweighted (x{factor:.2f})"
            notes.append(f"{rsi_label}={_fmt(snapshot.rsi)} at/above overbought {overbought:.0f} -> {outcome}")
            record(verdict, 1.0 if verdict == SKIP else factor)
        elif snapshot.rsi <= oversold:
            factor = float(rsi_config["oversold_confidence_multiplier"])
            notes.append(f"{rsi_label}={_fmt(snapshot.rsi)} at/below oversold {oversold:.0f} -> favourable entry (x{factor:.2f})")
            record(ALLOW, factor)
        else:
            notes.append(f"{rsi_label}={_fmt(snapshot.rsi)} inside {oversold:.0f}-{overbought:.0f} band -> neutral")

    # --- MACD cross ---------------------------------------------------------
    if snapshot.macd is None or snapshot.macd_signal is None:
        notes.append("MACD unavailable")
        if config.get("require_indicators", False):
            record(SKIP, 1.0)
    elif snapshot.macd > snapshot.macd_signal:
        factor = float(macd_config["bullish_confidence_multiplier"])
        notes.append(
            f"MACD={_fmt(snapshot.macd)} above signal={_fmt(snapshot.macd_signal)} "
            f"(hist {_fmt(snapshot.macd_histogram)}) -> bullish cross (x{factor:.2f})"
        )
        record(ALLOW, factor)
    elif snapshot.macd < snapshot.macd_signal:
        verdict, factor = _component_verdict(
            macd_config["on_bearish_cross"], float(macd_config["bearish_confidence_multiplier"])
        )
        outcome = "entry skipped" if verdict == SKIP else f"downweighted (x{factor:.2f})"
        notes.append(
            f"MACD={_fmt(snapshot.macd)} below signal={_fmt(snapshot.macd_signal)} "
            f"(hist {_fmt(snapshot.macd_histogram)}) -> bearish cross, {outcome}"
        )
        record(verdict, 1.0 if verdict == SKIP else factor)
    else:
        notes.append(f"MACD equals signal at {_fmt(snapshot.macd)} -> no cross, no adjustment")

    if SKIP in verdicts:
        return IndicatorModulation(SKIP, multiplier, tuple(notes), snapshot)

    max_confidence = float(config.get("max_confidence", 0.95))
    modulated = min(max(base_confidence * multiplier, 0.0), max_confidence)
    floor = float(config.get("min_confidence_to_act", 0.0) or 0.0)
    if floor > 0 and modulated < floor:
        notes.append(
            f"modulated confidence {modulated:.2f} below min_confidence_to_act {floor:.2f} -> entry skipped"
        )
        return IndicatorModulation(SKIP, multiplier, tuple(notes), snapshot)

    action = DOWNWEIGHT if multiplier < 1.0 else ALLOW
    return IndicatorModulation(action, multiplier, tuple(notes), snapshot)
