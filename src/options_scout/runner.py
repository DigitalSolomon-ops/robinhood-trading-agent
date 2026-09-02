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
    _persist_for_alerts(plays, today.isoformat())
    return send_or_preview(
        plays, config, dry_run=dry_run, out_path=out_path, today=today, print_fn=print_fn,
        extra_recipients=_report_recipients(),
        enrichment=_build_enrichment(plays, today, config),
    )


def _build_enrichment(plays: list[Any], today: Any, config: dict[str, Any]) -> dict[str, Any]:
    """Best-effort Finnhub event context (earnings-in-horizon + latest headline)
    for the play symbols. Any failure or a missing API key yields {}, so the
    email renders exactly as before -- enrichment is a bonus, the email is the
    product."""
    try:
        from ..scout_enrichment.enrichment import enrich_symbols

        symbols = [str(getattr(play, "symbol", "")) for play in plays]
        horizon = int((config.get("enrichment") or {}).get("earnings_horizon_days", 14))
        return enrich_symbols(symbols, today=today, earnings_horizon_days=horizon)
    except Exception:
        return {}


def _report_recipients() -> list[str]:
    """The operator-managed report distribution list, read best-effort from the
    same GCS/local store the scout persists plays to. Additive: any failure to
    read yields [] so the email still goes to the default operator address."""
    try:
        from ..entry_alerts.config import load_alerts_config, resolve_bucket
        from ..entry_alerts.store import load_recipients

        return load_recipients(bucket=resolve_bucket(load_alerts_config()))
    except Exception:
        return []


def _persist_for_alerts(plays: list[Any], day: str) -> None:
    """Additive, best-effort: write the day's ranked plays to the entry-alert
    store so the notification-only alerter can watch their entry levels. A
    failure here MUST NOT break the scout email, so everything is swallowed --
    persistence is a bonus, the email is the product."""
    try:
        from ..entry_alerts.config import load_alerts_config, resolve_bucket
        from ..entry_alerts.store import record_from_options_play, save_plays

        records = [
            record
            for record in (
                record_from_options_play(play, day, rank=i + 1) for i, play in enumerate(plays)
            )
            if record is not None
        ]
        if records:
            save_plays(records, day=day, bucket=resolve_bucket(load_alerts_config()))
    except Exception:
        pass
