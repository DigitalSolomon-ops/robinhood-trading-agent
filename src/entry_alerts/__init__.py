"""Entry-Hit Alerter -- a NOTIFICATION-ONLY poller for the daily scout plays.

Polls a delayed underlying price per symbol during US market hours and sends a
ONE-TIME alert email when a recommended play's ENTRY level is reached. It NEVER
trades: no order is placed, reviewed, or cancelled anywhere in this package, and
it does not touch any trading gate, the crypto lane, or the equities order path.
"""

from .alerter import AlertCycleResult, entry_hit, run_cycle
from .config import (
    load_alerts_config,
    resolve_bucket,
    resolve_enabled_sources,
    resolve_from_addr,
    resolve_to_addr,
    resolve_tolerance,
)
from .email import EmailResult, Hit, compose_email, render_email_html, send_or_preview
from .quotes import fetch_prices
from .store import (
    DayState,
    PlayRecord,
    load_day,
    make_play_id,
    mark_fired,
    record_from_options_play,
    record_from_smallcap_pick,
    save_plays,
)

__all__ = [
    "AlertCycleResult",
    "DayState",
    "EmailResult",
    "Hit",
    "PlayRecord",
    "compose_email",
    "entry_hit",
    "fetch_prices",
    "load_alerts_config",
    "load_day",
    "make_play_id",
    "mark_fired",
    "record_from_options_play",
    "record_from_smallcap_pick",
    "render_email_html",
    "resolve_bucket",
    "resolve_enabled_sources",
    "resolve_from_addr",
    "resolve_to_addr",
    "resolve_tolerance",
    "run_cycle",
    "save_plays",
    "send_or_preview",
]
