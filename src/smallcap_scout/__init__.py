"""Small-Cap Scout -- a daily, ANALYSIS-ONLY momentum screener.

Reads public end-of-day market data (Massive / Polygon.io) and emails a ranked
shares WATCHLIST in the Ross Cameron / Warrior-Trading "5 Pillars" momentum
style (big move, high relative volume, price range, low float, news catalyst).
It NEVER trades: no order is placed, reviewed, or cancelled anywhere in this
package, and it does not touch any trading gate, the crypto lane, or the
equities order path.
"""

from .config import (
    load_scout_config,
    resolve_from_addr,
    resolve_gmail_app_password,
    resolve_to_addr,
)
from .email import (
    DISCLAIMER,
    EOD_NOTICE,
    EmailResult,
    compose_email,
    render_email_html,
    send_or_preview,
)
from .levels import Levels, compute_levels
from .runner import run_smallcap_scout_email
from .scanner import ScoutPick, scan

__all__ = [
    "DISCLAIMER",
    "EOD_NOTICE",
    "EmailResult",
    "Levels",
    "ScoutPick",
    "compose_email",
    "compute_levels",
    "load_scout_config",
    "render_email_html",
    "resolve_from_addr",
    "resolve_gmail_app_password",
    "resolve_to_addr",
    "run_smallcap_scout_email",
    "scan",
    "send_or_preview",
]
