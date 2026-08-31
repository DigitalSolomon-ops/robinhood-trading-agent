"""The entry-hit poll cycle: detect, alert once, persist the fired set.

NOTIFICATION ONLY. One cycle reads today's active plays, fetches a delayed
current price per symbol, and emails a ONE-TIME alert for each play whose ENTRY
level has been reached. It never places, reviews, or cancels an order and never
touches a trading gate, the crypto lane, or the equities order path.

Idempotency: a hit play's id is written to the day's fired set the moment its
alert goes out, and an already-fired play is skipped on every later cycle -- so
re-running the same cycle sends no duplicate. A dry-run persists NOTHING (it
composes and prints only), so it can be run freely without consuming a play's
one alert.

Entry-hit direction logic (the key judgment call):
  * A BULLISH play (call / long) is entered on strength -- price rising TO or
    THROUGH the entry from below. Hit when current >= entry * (1 - tolerance).
  * A BEARISH play (put) is entered on weakness -- price falling TO or THROUGH
    the entry from above. Hit when current <= entry * (1 + tolerance).
The tolerance is a small fractional band (e.g. 0.1%) so a near-touch of the
level counts, and the >=/<= naturally covers a price that has already blown
through the level in the play's direction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..equity_intelligence.massive_client import MassiveClient
from ..market_hours import blocked_reason
from .config import (
    load_alerts_config,
    resolve_bucket,
    resolve_enabled_sources,
    resolve_tolerance,
)
from .email import EmailResult, Hit, send_or_preview
from .quotes import fetch_prices
from .store import PlayRecord, load_day, mark_fired


def entry_hit(record: PlayRecord, price: float, tolerance: float) -> bool:
    """True when `price` has reached the play's entry level in the play's
    direction, within a fractional `tolerance` band. Direction-aware:
    bullish plays trigger from below, bearish plays from above."""
    if price <= 0:
        return False
    if record.bullish:
        return price >= record.entry * (1.0 - tolerance)
    return price <= record.entry * (1.0 + tolerance)


@dataclass
class AlertCycleResult:
    day: str
    skipped: bool = False
    reason: str = ""
    hits: list[Hit] = field(default_factory=list)
    sent: bool = False
    dry_run: bool = False
    email: EmailResult | None = None
    detail: str = ""


def run_cycle(
    *,
    dry_run: bool,
    client: Any = None,
    config: dict[str, Any] | None = None,
    now: datetime | None = None,
    bucket: str | None = None,
    force: bool = False,
    allow_extended_hours: bool = False,
    print_fn: Any = print,
    smtp_factory: Any = None,
) -> AlertCycleResult:
    """Run ONE poll cycle and return what happened.

    `force=True` bypasses the market-hours guard (for a manual/dry run outside
    RTH). `bucket` overrides the resolved GCS bucket (tests pass a temp-backed
    None to use the local-file store)."""
    config = config if config is not None else load_alerts_config()
    now = now or datetime.now(UTC)
    day = now.astimezone(UTC).date().isoformat() if now.tzinfo else now.date().isoformat()

    # (a) market-hours guard -- skip outside RTH unless forced.
    reason = blocked_reason(now, allow_extended_hours)
    if reason is not None and not force:
        return AlertCycleResult(day=day, skipped=True, reason=reason,
                                dry_run=dry_run, detail=f"skipped: {reason}")

    if bucket is None:
        bucket = resolve_bucket(config)
    tolerance = resolve_tolerance(config)
    enabled = resolve_enabled_sources(config)
    client = client or MassiveClient()

    # (b) load today's plays + fired set.
    state = load_day(day, bucket=bucket)
    active = [
        rec for rec in state.plays.values()
        if rec.source in enabled and rec.id not in state.fired
    ]

    # (c) fetch current prices (latest delayed minute bar) for symbols in play.
    prices = fetch_prices(client, [rec.symbol for rec in active], on_date=day)

    # (d) collect the plays whose entry is newly hit this cycle.
    newly: list[Hit] = []
    for rec in active:
        price = prices.get(rec.symbol)
        if price is None:
            continue
        if entry_hit(rec, price, tolerance):
            newly.append(Hit(play=rec, current_price=price))

    # Persist the fired set BEFORE sending is not required, but a real (non-dry)
    # cycle marks fired so a duplicate is impossible even if the send is retried.
    # A dry-run persists nothing -- it must not consume a play's one alert.
    if newly and not dry_run:
        mark_fired({h.play.id for h in newly}, day=day, bucket=bucket)

    # (e) send ONE email summarizing the newly-hit plays. On a real cycle with
    # nothing new we send nothing (an alert inbox, not a heartbeat) unless
    # poll.send_on_empty is set; a dry-run always composes+prints so the operator
    # can see the output.
    send_on_empty = bool((config.get("poll") or {}).get("send_on_empty", False))
    result = AlertCycleResult(day=day, hits=newly, dry_run=dry_run)

    if dry_run or newly or send_on_empty:
        email_result = send_or_preview(
            newly, config, dry_run=dry_run, today=now.date(),
            print_fn=print_fn, smtp_factory=smtp_factory,
        )
        result.email = email_result
        result.sent = email_result.sent
        result.detail = email_result.detail
    else:
        result.detail = "nothing new; no alert sent"

    return result
