"""Options Scout -- a daily, ANALYSIS-ONLY options screener.

Reads public market data (Massive / Polygon.io) and emails a ranked list of
CANDIDATE options plays with backtested base rates and an unmissable
not-financial-advice disclaimer. It NEVER trades: no order is placed, reviewed,
or cancelled anywhere in this package, and it does not touch any trading gate,
the crypto lane, or the equities order path.
"""

from .analyzer import Play, analyze_symbol, rank_plays, scout_plays
from .backtest import HitRate, backtest_setup
from .config import (
    load_scout_config,
    resolve_from_addr,
    resolve_gmail_app_password,
    resolve_to_addr,
)
from .email import EmailResult, compose_email, render_email_html, send_or_preview
from .runner import run_options_scout_email

__all__ = [
    "EmailResult",
    "HitRate",
    "Play",
    "analyze_symbol",
    "backtest_setup",
    "compose_email",
    "load_scout_config",
    "rank_plays",
    "render_email_html",
    "resolve_from_addr",
    "resolve_gmail_app_password",
    "resolve_to_addr",
    "run_options_scout_email",
    "scout_plays",
    "send_or_preview",
]
