"""Top-level Small-Cap Scout orchestration for the CLI.

ANALYSIS ONLY. Builds a read-only MassiveClient, runs the market-wide scan, and
hands the ranked watchlist to the email composer. No order path is reachable
from here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..equity_intelligence.massive_client import MassiveClient
from .config import load_scout_config
from .email import EmailResult, send_or_preview
from .scanner import scan


def run_smallcap_scout_email(
    *,
    dry_run: bool,
    top_n: int | None = None,
    out_path: str | None = None,
    client: Any = None,
    config: dict[str, Any] | None = None,
    print_fn: Any = print,
) -> EmailResult:
    """Scan the market and send (or preview) the ranked small-cap watchlist."""
    config = config if config is not None else load_scout_config()
    if top_n is not None:
        config = {**config, "top_n": int(top_n)}

    client = client or MassiveClient()
    now = datetime.now(UTC)
    today = now.date()

    picks = scan(client, config, today=today, now=now)
    return send_or_preview(
        picks, config, dry_run=dry_run, out_path=out_path, today=today, print_fn=print_fn
    )
