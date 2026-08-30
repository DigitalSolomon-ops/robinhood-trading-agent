"""Top-level Options Scout orchestration for the CLI.

ANALYSIS ONLY. Builds a read-only MassiveClient, runs the analyzer over the
configured universe, and hands the ranked plays to the email composer. No order
path is reachable from here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..equity_intelligence.massive_client import MassiveClient
from .analyzer import scout_plays
from .config import load_scout_config
from .email import EmailResult, send_or_preview


def run_options_scout_email(
    *,
    dry_run: bool,
    top_n: int | None = None,
    out_path: str | None = None,
    client: Any = None,
    config: dict[str, Any] | None = None,
    print_fn: Any = print,
) -> EmailResult:
    """Analyze the universe and send (or preview) the ranked options email."""
    config = config if config is not None else load_scout_config()
    if top_n is not None:
        config = {**config, "top_n": int(top_n)}

    client = client or MassiveClient()
    now = datetime.now(UTC)
    today = now.date()

    plays = scout_plays(client, config, today=today, now=now)
    return send_or_preview(
        plays, config, dry_run=dry_run, out_path=out_path, today=today, print_fn=print_fn
    )
