"""Massive ticker news + per-ticker sentiment as a PRE-TRADE RISK FILTER.

What this module is
-------------------
A veto, and only a veto. Before an ENTRY, it reads recent news for the symbol
from the read-only Massive client, counts the per-ticker sentiment labels
published inside a configured recency window, and returns one of: leave the
entry alone, downweight it, or skip it. Every threshold and the window itself
come from `equities.news_sentiment:` in config/trading_rules.yaml -- this is a
RISK setting, so it lives beside the other risk caps rather than in
strategy.yaml with the signal-generation inputs.

What this module is NOT
-----------------------
It is not an execution path and it cannot become one:

* nothing here imports OrderManager, RiskManager, the kill switch, or any
  broker, and nothing here can name a connector order tool. A sentiment
  reading only changes WHICH SIGNAL is handed to the existing gates; those
  gates then run afterwards, unchanged and in the same order.
* the filter is one-directional twice over. `evaluate_news_sentiment` can
  never return a confidence multiplier above 1.0 (a config that asks for one
  is clamped, with a note saying so), and `apply_sentiment_filter` in
  StrategyEngine can never set `side` to "buy" or "sell". So no headline --
  however glowing -- can create an order the rules did not already ask for,
  raise a confidence, or shorten the path to a fill.
* EXITS ARE NEVER FILTERED. Bad news must not trap an open position behind a
  news vendor's uptime, so a "sell" is passed through unchanged and merely
  ANNOTATED with the sentiment for the audit rationale.
* it is not a substitute for a risk gate. It runs BEFORE RiskManager,
  OrderManager, the kill switch and the human confirm-flag, and removes
  nothing from any of them -- a name this filter allows still has to clear
  every one of them.

News is published data with an unpredictable lag. It is a decision input,
never an execution-timing or pricing input -- the Robinhood connector stays
the sole source of execution-time price.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Protocol

SOURCE = "massive_news"

# Every threshold below is a DEFAULT that `equities.news_sentiment:` in
# config/trading_rules.yaml overrides key by key.
DEFAULTS: dict[str, Any] = {
    # Off unless a config turns it on: the lane's read-only-by-default posture
    # extends to reaching out to a news vendor at all.
    "enabled": False,
    # When true, a buy whose news could not be read is SKIPPED rather than
    # passed through unfiltered. Fail-closed opt-in.
    "require_news": False,
    # The recency window. Anything published longer ago than this is not
    # "recent sentiment" and is ignored entirely.
    "recency_hours": 48,
    # How many articles to ask the vendor for, per symbol, per read.
    "max_articles": 20,
    # Below this many RATED articles inside the window there is not enough
    # coverage to act on; the filter says so and stands down.
    "min_rated_articles": 2,
    # Which vendor labels count as which. Massive/Polygon emits
    # positive|neutral|negative today; a relabelling is a config change.
    "negative_labels": ["negative"],
    "positive_labels": ["positive"],
    # Share of rated articles that are negative.
    "strongly_negative_ratio": 0.6,
    "negative_ratio": 0.34,
    "on_strongly_negative": "skip_entry",  # skip_entry | downweight
    "on_negative": "downweight",  # skip_entry | downweight
    # Multipliers may only REDUCE. A value above 1.0 is clamped to 1.0.
    "strongly_negative_confidence_multiplier": 0.4,
    "negative_confidence_multiplier": 0.6,
    # A filtered confidence below this floor downgrades the entry to a hold.
    "min_confidence_to_act": 0.35,
}

# Filter outcomes. Deliberately the same vocabulary as indicator_signals so an
# audit row reads the same whichever pre-trade filter produced it.
ALLOW = "allow"
DOWNWEIGHT = "downweight"
SKIP = "skip"
NOT_APPLICABLE = "not_applicable"  # signal is not an entry -- annotate only
UNAVAILABLE = "unavailable"  # no news data; rules signal passed through


def news_sentiment_config(trading_rules: dict[str, Any]) -> dict[str, Any]:
    """DEFAULTS with `equities.news_sentiment:` from config/trading_rules.yaml
    merged over it, one level deep (so a config can override a single
    threshold without restating the whole section)."""
    override = (trading_rules.get("equities") or {}).get("news_sentiment") or {}
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


def _labels(config: dict[str, Any], key: str) -> set[str]:
    raw = config.get(key) or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(label).strip().lower() for label in raw if str(label).strip()}


def _parse_published(value: str | None) -> datetime | None:
    """Massive publishes RFC-3339 with a trailing Zulu marker, which
    fromisoformat has parsed natively since Python 3.11. An unparseable
    timestamp is treated as unknown rather than as 'now' -- guessing recency
    in a recency filter is exactly the wrong failure."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class SentimentHeadline:
    """One rated article, kept so the rationale can quote it verbatim."""

    title: str
    sentiment: str
    published_utc: str | None = None
    article_url: str | None = None
    reasoning: str | None = None
    age_hours: float | None = None

    def citation(self) -> str:
        stamp = self.published_utc or "unknown time"
        age = f", {self.age_hours:.1f}h ago" if self.age_hours is not None else ""
        return f'"{self.title}" [{self.sentiment}, {stamp}{age}]'


@dataclass(frozen=True)
class SentimentSnapshot:
    """Recent per-ticker sentiment for one symbol, inside the config window.

    `error` carries a human-readable reason the read failed; when it is set the
    counts are all zero and the filter degrades to UNAVAILABLE (or SKIP under
    require_news).
    """

    symbol: str
    source: str = SOURCE
    window_hours: float = 48.0
    articles_in_window: int = 0
    rated: int = 0
    negative: int = 0
    positive: int = 0
    neutral: int = 0
    # Negative headlines inside the window, most recent first.
    negative_headlines: tuple[SentimentHeadline, ...] = ()
    latest_headline: SentimentHeadline | None = None
    error: str | None = None

    @property
    def negative_ratio(self) -> float:
        return (self.negative / self.rated) if self.rated else 0.0

    @property
    def worst_headline(self) -> SentimentHeadline | None:
        """The most recent negative headline -- the one a human would want
        quoted back at them when the entry is refused."""
        return self.negative_headlines[0] if self.negative_headlines else None

    def counts(self) -> tuple[tuple[str, int], ...]:
        """Audit-friendly (name, count) pairs. Frozen so it can ride on a
        TradeSignal."""
        return (
            ("articles_in_window", self.articles_in_window),
            ("rated", self.rated),
            ("negative", self.negative),
            ("neutral", self.neutral),
            ("positive", self.positive),
        )

    def citation(self) -> str:
        """The sentiment reading and the headline behind it, rendered for the
        decision rationale."""
        if self.error:
            return f"{self.source} unavailable ({self.error})"
        if not self.rated:
            return (
                f"{self.source} found no rated articles for {self.symbol} "
                f"in the last {self.window_hours:g}h"
            )
        head = self.worst_headline or self.latest_headline
        summary = (
            f"{self.source} {self.negative}/{self.rated} rated articles negative "
            f"({self.negative_ratio * 100:.0f}%) for {self.symbol} in the last {self.window_hours:g}h"
        )
        if head is None:
            return summary
        label = "most recent negative" if self.worst_headline is not None else "most recent"
        return f"{summary}; {label}: {head.citation()}"


@dataclass(frozen=True)
class SentimentFilterResult:
    """What the news filter did (or declined to do) to a rules signal."""

    action: str
    confidence_multiplier: float
    notes: tuple[str, ...]
    snapshot: SentimentSnapshot | None

    @property
    def skipped(self) -> bool:
        return self.action == SKIP

    def rationale(self) -> str:
        """One readable clause naming the sentiment, the headline behind it,
        and what it was taken to mean."""
        citation = self.snapshot.citation() if self.snapshot is not None else "no news data"
        interpretation = "; ".join(self.notes) if self.notes else "no interpretation applied"
        return f"news_sentiment[{citation}] -> {interpretation}"


class SentimentProvider(Protocol):
    """Anything that can hand the strategy a recent-sentiment snapshot.

    A Protocol on purpose: tests inject a stub, and the real implementation
    (MassiveNewsSentimentProvider) is read-only by construction.
    """

    def snapshot(self, symbol: str) -> SentimentSnapshot: ...


def summarize_news(
    symbol: str,
    items: list[Any],
    config: dict[str, Any],
    now: datetime | None = None,
) -> SentimentSnapshot:
    """Count per-ticker sentiment labels across the news items published inside
    the config recency window.

    Pure: no I/O and no config mutation. An article with no per-ticker insight
    for THIS symbol is counted as in-window but unrated -- a piece that merely
    mentions the ticker is not a sentiment reading about it.
    """
    now = now or datetime.now(UTC)
    window_hours = float(config.get("recency_hours", 48) or 0)
    cutoff = now - timedelta(hours=window_hours)
    negative_labels = _labels(config, "negative_labels")
    positive_labels = _labels(config, "positive_labels")

    rated: list[SentimentHeadline] = []
    in_window = 0
    negative = positive = neutral = 0

    for item in items:
        published = _parse_published(getattr(item, "published_utc", None))
        if published is None or published < cutoff:
            # Undated or older than the window: not "recent sentiment".
            continue
        in_window += 1
        sentiment = None
        reasoning = None
        for insight in getattr(item, "insights", ()) or ():
            if str(getattr(insight, "ticker", "")).upper() == symbol.upper():
                sentiment = getattr(insight, "sentiment", None)
                reasoning = getattr(insight, "sentiment_reasoning", None)
                break
        if sentiment is None:
            continue
        label = str(sentiment).strip().lower()
        if not label:
            continue
        headline = SentimentHeadline(
            title=str(getattr(item, "title", "") or "(untitled)"),
            sentiment=label,
            published_utc=getattr(item, "published_utc", None),
            article_url=getattr(item, "article_url", None),
            reasoning=reasoning,
            age_hours=max((now - published).total_seconds() / 3600.0, 0.0),
        )
        rated.append(headline)
        if label in negative_labels:
            negative += 1
        elif label in positive_labels:
            positive += 1
        else:
            neutral += 1

    rated.sort(key=lambda head: head.age_hours if head.age_hours is not None else float("inf"))
    negatives = tuple(head for head in rated if head.sentiment in negative_labels)
    return SentimentSnapshot(
        symbol=symbol,
        window_hours=window_hours,
        articles_in_window=in_window,
        rated=len(rated),
        negative=negative,
        positive=positive,
        neutral=neutral,
        negative_headlines=negatives,
        latest_headline=rated[0] if rated else None,
    )


class MassiveNewsSentimentProvider:
    """Reads recent ticker news + per-ticker sentiment from MassiveClient.

    Read-only: the only client method it can reach is get_ticker_news. Any
    failure (missing API key, rate limit, HTTP error) is caught and returned as
    a snapshot with `error` set -- a news feed going down must degrade the
    signal, never crash the lane and never place anything.
    """

    def __init__(
        self,
        client: Any,
        config: dict[str, Any],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.client = client
        self.config = config
        self._now = now
        self._cache: dict[str, SentimentSnapshot] = {}

    def _cutoff(self, now: datetime) -> str:
        """The window start, floored to the hour. Flooring keeps the request
        (and so the client's response cache) stable across the cycles of one
        session instead of minting a fresh cache key every minute."""
        hours = float(self.config.get("recency_hours", 48) or 0)
        start = (now - timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
        return start.strftime("%Y-%m-%dT%H:%M:%SZ")

    def snapshot(self, symbol: str) -> SentimentSnapshot:
        cached = self._cache.get(symbol)
        if cached is not None:
            return cached
        now = self._now()
        window_hours = float(self.config.get("recency_hours", 48) or 0)
        try:
            items = self.client.get_ticker_news(
                symbol,
                limit=int(self.config.get("max_articles", 20)),
                order="desc",
                sort="published_utc",
                **{"published_utc.gte": self._cutoff(now)},
            )
        except Exception as exc:  # noqa: BLE001 -- a news outage degrades, never crashes
            snapshot = SentimentSnapshot(
                symbol=symbol, window_hours=window_hours, error=f"{type(exc).__name__}: {exc}"
            )
            self._cache[symbol] = snapshot
            return snapshot
        # The vendor filter is a request hint, not a guarantee; the window is
        # re-applied locally so a server that ignores it cannot widen it.
        snapshot = summarize_news(symbol, list(items or []), self.config, now=now)
        self._cache[symbol] = snapshot
        return snapshot


def build_sentiment_provider(
    trading_rules: dict[str, Any], client_factory: Any = None
) -> SentimentProvider | None:
    """A provider when `equities.news_sentiment.enabled` is true, else None.

    Disabled is the default, so a lane that has not opted in never reaches a
    news vendor at all and the strategy behaves exactly as before.
    """
    config = news_sentiment_config(trading_rules)
    if not config.get("enabled", False):
        return None
    if client_factory is None:
        from .massive_client import MassiveClient

        client_factory = MassiveClient
    return MassiveNewsSentimentProvider(client_factory(), config)


def _reducing_multiplier(value: Any, notes: list[str]) -> float:
    """A multiplier this filter is allowed to return: [0.0, 1.0].

    The clamp is the enforcement point for "may only block or reduce". A config
    that asks for 1.5 gets 1.0 and a note saying it was clamped, so a
    misconfiguration cannot quietly turn a risk filter into a signal booster.
    """
    try:
        factor = float(value)
    except (TypeError, ValueError):
        notes.append(f"non-numeric confidence multiplier {value!r} ignored (treated as 1.00)")
        return 1.0
    if factor > 1.0:
        notes.append(
            f"configured confidence multiplier {factor:.2f} clamped to 1.00 -- "
            "the news filter may only reduce confidence, never raise it"
        )
        return 1.0
    return max(factor, 0.0)


# The config spelling for "refuse the entry". `equity_indicators:` in
# strategy.yaml spells the same policy "skip"; the bare word is accepted here
# as a synonym, but the SHIPPED config uses `skip_entry` because a 4-letter
# lowercase value under `equities:` in trading_rules.yaml is indistinguishable
# from a ticker to tests/test_order_symbol_guard.py's config vocabulary scan,
# and poisoning that guard's vocabulary is not worth four characters.
_SKIP_MODES = {"skip", "skip_entry"}


def _component_verdict(mode: str, multiplier: float) -> tuple[str, float]:
    """A band that fired either skips the entry or downweights it."""
    if str(mode).strip().lower() in _SKIP_MODES:
        return SKIP, multiplier
    return DOWNWEIGHT, multiplier


def evaluate_news_sentiment(
    snapshot: SentimentSnapshot | None,
    base_side: str,
    base_confidence: float,
    config: dict[str, Any],
) -> SentimentFilterResult:
    """Turn a sentiment snapshot into a block/reduce verdict on an entry.

    Pure: no I/O, no config mutation, no order path. Returns SKIP or a
    confidence multiplier in [0.0, 1.0], and always returns the notes that
    justify it.
    """
    if base_side != "buy":
        # Exits and holds are annotated, never filtered -- see module docstring.
        notes = ("news sentiment filters entries only; this non-entry signal is passed through unchanged",)
        return SentimentFilterResult(NOT_APPLICABLE, 1.0, notes, snapshot)

    if snapshot is None or snapshot.error:
        detail = snapshot.error if snapshot is not None else "no news provider configured"
        if config.get("require_news", False):
            note = f"news sentiment required but unavailable ({detail}); entry skipped"
            return SentimentFilterResult(SKIP, 1.0, (note,), snapshot)
        note = f"news sentiment unavailable ({detail}); rules-based signal passed through unfiltered"
        return SentimentFilterResult(UNAVAILABLE, 1.0, (note,), snapshot)

    minimum = int(config.get("min_rated_articles", 0) or 0)
    if snapshot.rated < minimum:
        note = (
            f"only {snapshot.rated} rated article(s) in the last {snapshot.window_hours:g}h, "
            f"below min_rated_articles {minimum} -- insufficient coverage to filter on"
        )
        if config.get("require_news", False):
            return SentimentFilterResult(SKIP, 1.0, (f"{note}; entry skipped (require_news)",), snapshot)
        return SentimentFilterResult(ALLOW, 1.0, (note,), snapshot)

    notes: list[str] = []
    ratio = snapshot.negative_ratio
    strong_threshold = float(config.get("strongly_negative_ratio", 0.6))
    negative_threshold = float(config.get("negative_ratio", 0.34))
    head = snapshot.worst_headline

    if ratio >= strong_threshold:
        multiplier = _reducing_multiplier(config.get("strongly_negative_confidence_multiplier", 0.4), notes)
        verdict, factor = _component_verdict(config.get("on_strongly_negative", "skip"), multiplier)
        outcome = "entry skipped" if verdict == SKIP else f"downweighted (x{factor:.2f})"
        notes.append(
            f"negative share {ratio:.2f} at/above strongly_negative_ratio {strong_threshold:.2f} "
            f"-> strongly negative, {outcome}"
        )
    elif ratio >= negative_threshold:
        multiplier = _reducing_multiplier(config.get("negative_confidence_multiplier", 0.6), notes)
        verdict, factor = _component_verdict(config.get("on_negative", "downweight"), multiplier)
        outcome = "entry skipped" if verdict == SKIP else f"downweighted (x{factor:.2f})"
        notes.append(
            f"negative share {ratio:.2f} at/above negative_ratio {negative_threshold:.2f} "
            f"-> negative, {outcome}"
        )
    else:
        notes.append(
            f"negative share {ratio:.2f} below negative_ratio {negative_threshold:.2f} "
            f"-> no negative-news veto"
        )
        return SentimentFilterResult(ALLOW, 1.0, tuple(notes), snapshot)

    if head is not None and head.reasoning:
        notes.append(f"vendor reasoning: {head.reasoning}")

    if verdict == SKIP:
        return SentimentFilterResult(SKIP, 1.0, tuple(notes), snapshot)

    filtered = max(base_confidence * factor, 0.0)
    floor = float(config.get("min_confidence_to_act", 0.0) or 0.0)
    if floor > 0 and filtered < floor:
        notes.append(
            f"filtered confidence {filtered:.2f} below min_confidence_to_act {floor:.2f} -> entry skipped"
        )
        return SentimentFilterResult(SKIP, factor, tuple(notes), snapshot)

    action = DOWNWEIGHT if factor < 1.0 else ALLOW
    return SentimentFilterResult(action, factor, tuple(notes), snapshot)
