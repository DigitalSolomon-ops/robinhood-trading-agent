"""Market-wide breadth from Massive grouped-daily bars, as a RISK-OFF BRAKE.

What this module is
-------------------
One reading per cycle, about the market rather than about a symbol: of the
stocks that moved in the last completed session, what share advanced? That
share is turned into a regime -- risk_on, neutral, or risk_off -- and the
regime into a POSITION-SIZE MULTIPLIER, or into a refusal to open anything new
at all. Every threshold comes from `equities.market_regime:` in
config/trading_rules.yaml; it lives in the rules file rather than strategy.yaml
because it is a risk cap, not a signal-generation input.

What this module is NOT
-----------------------
It is not an execution path and it cannot become one:

* nothing here imports OrderManager, RiskManager, the kill switch, or any
  broker, and nothing here can name a connector order tool. A regime reading
  only changes HOW LARGE an entry the existing gates are asked about, or
  whether they are asked at all; the gates then run afterwards, unchanged and
  in the same order.
* it is one-directional. `size_multiplier` is clamped to [0.0, 1.0] (a config
  asking for more is clamped, with a note saying so), so a breadth reading can
  only ever make a position SMALLER than the configured cap, never larger, and
  the cap itself is still enforced downstream by RiskManager.
* EXITS ARE NEVER SCALED OR BLOCKED. Shrinking a sell would leave part of a
  position stranded behind a data vendor, which is the opposite of a risk
  control, so the caller applies this to entries only.
* it is not a substitute for a risk gate. It runs BEFORE RiskManager,
  OrderManager, the kill switch and the human confirm-flag, and removes nothing
  from any of them.

Grouped-daily bars are end-of-day data for a session that has already closed.
That makes breadth a decision input, never an execution-timing or pricing
input -- the Robinhood connector stays the sole source of execution-time price.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol, Sequence

SOURCE = "massive_breadth"

# Every threshold below is a DEFAULT that `equities.market_regime:` in
# config/trading_rules.yaml overrides key by key.
DEFAULTS: dict[str, Any] = {
    # Off unless a config turns it on: the lane's read-only-by-default posture
    # extends to reaching out to a data vendor at all.
    "enabled": False,
    # Fail-closed opt-in. When true, a cycle whose breadth could not be read
    # opens nothing new at all rather than sizing on the rules alone.
    "require_breadth": False,
    # How many calendar days back to walk looking for the most recent COMPLETED
    # session. Covers a long weekend plus a market holiday.
    "lookback_days": 6,
    # Below this many counted rows the reading is not market-wide and is
    # discarded -- a partial grouped-daily response is not breadth.
    "min_symbols": 500,
    # Sub-dollar names are excluded from the count; their moves are noise at
    # this scale and they swamp an advance/decline ratio.
    "min_close_price": 1.0,
    # Share of (advancers + decliners) that advanced.
    "risk_on_advance_ratio": 0.55,
    "risk_off_advance_ratio": 0.40,
    # block_entries | scale_down. Spelled with an underscore deliberately: a
    # short all-alpha value under `equities:` in trading_rules.yaml is
    # indistinguishable from a ticker to tests/test_order_symbol_guard.py's
    # config vocabulary scan (see the same note in news_sentiment.py).
    "on_risk_off": "block_entries",
    # Multipliers may only REDUCE. A value above 1.0 is clamped to 1.0.
    "risk_on_size_multiplier": 1.0,
    "neutral_size_multiplier": 0.75,
    "risk_off_size_multiplier": 0.4,
    # A scaled entry smaller than this is not worth placing; it is refused with
    # a rationale rather than sent as dust.
    "min_size_usd": 5.0,
}

# Regimes.
RISK_ON = "risk_on"
NEUTRAL = "neutral"
RISK_OFF = "risk_off"
UNKNOWN = "unknown"

# Verdicts. Same vocabulary shape as the other pre-trade filters so an audit row
# reads the same whichever one produced it.
ALLOW = "allow"
SCALE_DOWN = "scale_down"
BLOCK_ENTRIES = "block_entries"
DISABLED = "disabled"

# The config spellings for "open nothing new".
_BLOCK_MODES = {"block_entries", "block"}


def market_regime_config(trading_rules: dict[str, Any]) -> dict[str, Any]:
    """DEFAULTS with `equities.market_regime:` from config/trading_rules.yaml
    merged over it, one level deep (so a config can override a single threshold
    without restating the whole section)."""
    override = (trading_rules.get("equities") or {}).get("market_regime") or {}
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


@dataclass(frozen=True)
class RegimeSnapshot:
    """Advance/decline breadth for one completed session.

    `error` carries a human-readable reason the read failed; when it is set the
    counts are all zero and the verdict degrades to a pass-through (or to
    blocked entries under require_breadth).
    """

    source: str = SOURCE
    session: str = ""
    counted: int = 0
    advancers: int = 0
    decliners: int = 0
    unchanged: int = 0
    error: str | None = None

    @property
    def moved(self) -> int:
        """Names that actually moved -- the advance-ratio denominator. Flat
        closes are excluded rather than counted as half a decline."""
        return self.advancers + self.decliners

    @property
    def advance_ratio(self) -> float:
        return (self.advancers / self.moved) if self.moved else 0.0

    def counts(self) -> tuple[tuple[str, int], ...]:
        """Audit-friendly (name, count) pairs. Frozen so it can ride on a
        decision's details payload."""
        return (
            ("counted", self.counted),
            ("advancers", self.advancers),
            ("decliners", self.decliners),
            ("unchanged", self.unchanged),
        )

    def citation(self) -> str:
        """The breadth reading itself, rendered for the decision rationale."""
        if self.error:
            return f"{self.source} unavailable ({self.error})"
        if not self.moved:
            return f"{self.source} counted no moving names in session {self.session or 'unknown'}"
        return (
            f"{self.source} session {self.session}: {self.advancers} advancing / "
            f"{self.decliners} declining of {self.counted} counted "
            f"({self.advance_ratio * 100:.0f}% advancing)"
        )


@dataclass(frozen=True)
class RegimeVerdict:
    """What the breadth reading does (or declines to do) to new entries."""

    regime: str
    action: str
    size_multiplier: float
    notes: tuple[str, ...]
    snapshot: RegimeSnapshot | None

    @property
    def enabled(self) -> bool:
        return self.action != DISABLED

    @property
    def blocks_entries(self) -> bool:
        return self.action == BLOCK_ENTRIES

    def rationale(self) -> str:
        """One readable clause naming the breadth, the regime it implies, and
        what that was taken to mean for sizing."""
        citation = self.snapshot.citation() if self.snapshot is not None else "no breadth data"
        interpretation = "; ".join(self.notes) if self.notes else "no interpretation applied"
        return f"market_regime[{self.regime}: {citation}] -> {interpretation}"

    def scaled_amount(self, base_amount: float) -> float:
        """The per-trade cap after the regime multiplier.

        Reduce-only, enforced a second time here: the result is floored against
        the incoming amount, so even a multiplier that somehow escaped the
        config clamp cannot size an entry ABOVE the configured cap.
        """
        base = max(float(base_amount), 0.0)
        return min(max(base * self.size_multiplier, 0.0), base)


class RegimeProvider(Protocol):
    """Anything that can hand the runtime one market-wide breadth reading.

    A Protocol on purpose: tests inject a stub, and the real implementation
    (MassiveBreadthProvider) is read-only by construction. It takes no symbol --
    the regime is a property of the market, not of a ticker.
    """

    def snapshot(self) -> RegimeSnapshot: ...


def summarize_breadth(rows: Sequence[Any], config: dict[str, Any], session: str) -> RegimeSnapshot:
    """Count advancers and decliners across one session's grouped-daily rows.

    Pure: no i/o and no config mutation. A row is counted only when it has a
    positive open AND close and closes at or above `min_close_price`; a name
    that closed exactly where it opened is counted as unchanged and kept out of
    the advance ratio entirely.
    """
    floor_price = float(config.get("min_close_price", 0) or 0)
    counted = advancers = decliners = unchanged = 0
    for row in rows:
        try:
            opening = float(getattr(row, "open", 0) or 0)
            closing = float(getattr(row, "close", 0) or 0)
        except (TypeError, ValueError):
            continue
        if opening <= 0 or closing <= 0 or closing < floor_price:
            continue
        counted += 1
        if closing > opening:
            advancers += 1
        elif closing < opening:
            decliners += 1
        else:
            unchanged += 1
    return RegimeSnapshot(
        session=session,
        counted=counted,
        advancers=advancers,
        decliners=decliners,
        unchanged=unchanged,
    )


class MassiveBreadthProvider:
    """Reads one completed session's grouped-daily bars from MassiveClient.

    Read-only: the only client method it can reach is get_grouped_daily. It
    walks BACKWARDS from yesterday (today's session may still be open, and an
    incomplete session is not breadth) until a day returns a market-wide row
    count, skipping weekends and any day the vendor returns nothing for -- which
    is how market holidays are handled without shipping a holiday calendar.

    Any failure (missing api key, rate limit, http error) is caught and returned
    as a snapshot with `error` set: a data feed going down must degrade the
    sizing decision, never crash the lane and never place anything. The reading
    is cached for the life of the provider, since a completed session cannot
    change again.
    """

    def __init__(self, client: Any, config: dict[str, Any], today: date | None = None) -> None:
        self.client = client
        self.config = config
        self.today = today or datetime.now(UTC).date()
        self._cached: RegimeSnapshot | None = None

    def snapshot(self) -> RegimeSnapshot:
        if self._cached is not None:
            return self._cached
        lookback = max(int(self.config.get("lookback_days", 6) or 1), 1)
        minimum = max(int(self.config.get("min_symbols", 0) or 0), 1)
        tried: list[str] = []
        for offset in range(1, lookback + 1):
            session = self.today - timedelta(days=offset)
            if session.weekday() >= 5:
                continue
            tried.append(session.isoformat())
            try:
                rows = self.client.get_grouped_daily(session.isoformat())
            except Exception as exc:  # noqa: BLE001 -- a data outage degrades, never crashes
                self._cached = RegimeSnapshot(error=f"{type(exc).__name__}: {exc}")
                return self._cached
            candidate = summarize_breadth(list(rows or []), self.config, session.isoformat())
            if candidate.counted >= minimum:
                self._cached = candidate
                return self._cached
        self._cached = RegimeSnapshot(
            error=(
                f"no completed session with at least {minimum} counted names in the last "
                f"{lookback} day(s); tried {', '.join(tried) or 'no weekday'}"
            )
        )
        return self._cached


def build_regime_provider(trading_rules: dict[str, Any], client_factory: Any = None) -> RegimeProvider | None:
    """A provider when `equities.market_regime.enabled` is true, else None.

    Disabled is the default, so a lane that has not opted in never reaches a
    data vendor at all and sizing behaves exactly as before.
    """
    config = market_regime_config(trading_rules)
    if not config.get("enabled", False):
        return None
    if client_factory is None:
        from .massive_client import MassiveClient

        client_factory = MassiveClient
    return MassiveBreadthProvider(client_factory(), config)


def _reducing_multiplier(value: Any, notes: list[str]) -> float:
    """A multiplier this brake is allowed to return: [0.0, 1.0].

    The clamp is the enforcement point for "may only reduce or block". A config
    that asks for 1.5 gets 1.0 and a note saying it was clamped, so a
    misconfiguration cannot quietly turn a risk brake into a size booster.
    """
    try:
        factor = float(value)
    except (TypeError, ValueError):
        notes.append(f"non-numeric size multiplier {value!r} ignored (treated as 1.00)")
        return 1.0
    if factor > 1.0:
        notes.append(
            f"configured size multiplier {factor:.2f} clamped to 1.00 -- "
            "the market-regime brake may only reduce a position, never enlarge it"
        )
        return 1.0
    return max(factor, 0.0)


def evaluate_market_regime(snapshot: RegimeSnapshot | None, config: dict[str, Any]) -> RegimeVerdict:
    """Turn a breadth snapshot into a sizing verdict on NEW entries.

    Pure: no i/o, no config mutation, no order path. Returns either a refusal to
    open anything new, or a size multiplier in [0.0, 1.0] -- and always returns
    the notes that justify it.
    """
    if not config.get("enabled", False):
        note = "market-regime breadth is disabled in config; position sizing is unchanged"
        return RegimeVerdict(UNKNOWN, DISABLED, 1.0, (note,), snapshot)

    minimum = max(int(config.get("min_symbols", 0) or 0), 1)
    unavailable: str | None = None
    if snapshot is None:
        unavailable = "no breadth provider configured"
    elif snapshot.error:
        unavailable = snapshot.error
    elif snapshot.counted < minimum:
        unavailable = (
            f"only {snapshot.counted} name(s) counted in session {snapshot.session or 'unknown'}, "
            f"below min_symbols {minimum} -- not a market-wide reading"
        )
    elif not snapshot.moved:
        unavailable = f"no name advanced or declined in session {snapshot.session or 'unknown'}"

    if unavailable is not None:
        if config.get("require_breadth", False):
            note = f"market breadth required but unavailable ({unavailable}); new entries blocked"
            return RegimeVerdict(UNKNOWN, BLOCK_ENTRIES, 0.0, (note,), snapshot)
        note = f"market breadth unavailable ({unavailable}); position sizing left at the configured cap"
        return RegimeVerdict(UNKNOWN, ALLOW, 1.0, (note,), snapshot)

    assert snapshot is not None  # narrowed by the unavailable branch above
    notes: list[str] = []
    ratio = snapshot.advance_ratio
    risk_on_ratio = float(config.get("risk_on_advance_ratio", 0.55))
    risk_off_ratio = float(config.get("risk_off_advance_ratio", 0.40))

    if ratio <= risk_off_ratio:
        multiplier = _reducing_multiplier(config.get("risk_off_size_multiplier", 0.4), notes)
        if str(config.get("on_risk_off", "block_entries")).strip().lower() in _BLOCK_MODES:
            notes.append(
                f"advancing share {ratio:.2f} at/below risk_off_advance_ratio {risk_off_ratio:.2f} "
                f"-> risk_off, no new entries opened this cycle"
            )
            return RegimeVerdict(RISK_OFF, BLOCK_ENTRIES, 0.0, tuple(notes), snapshot)
        notes.append(
            f"advancing share {ratio:.2f} at/below risk_off_advance_ratio {risk_off_ratio:.2f} "
            f"-> risk_off, entry size scaled to x{multiplier:.2f} of the configured cap"
        )
        return RegimeVerdict(RISK_OFF, SCALE_DOWN, multiplier, tuple(notes), snapshot)

    if ratio >= risk_on_ratio:
        multiplier = _reducing_multiplier(config.get("risk_on_size_multiplier", 1.0), notes)
        notes.append(
            f"advancing share {ratio:.2f} at/above risk_on_advance_ratio {risk_on_ratio:.2f} "
            f"-> risk_on, entry size at x{multiplier:.2f} of the configured cap"
        )
        action = SCALE_DOWN if multiplier < 1.0 else ALLOW
        return RegimeVerdict(RISK_ON, action, multiplier, tuple(notes), snapshot)

    multiplier = _reducing_multiplier(config.get("neutral_size_multiplier", 0.75), notes)
    notes.append(
        f"advancing share {ratio:.2f} between risk_off_advance_ratio {risk_off_ratio:.2f} and "
        f"risk_on_advance_ratio {risk_on_ratio:.2f} -> neutral, entry size scaled to "
        f"x{multiplier:.2f} of the configured cap"
    )
    action = SCALE_DOWN if multiplier < 1.0 else ALLOW
    return RegimeVerdict(NEUTRAL, action, multiplier, tuple(notes), snapshot)
