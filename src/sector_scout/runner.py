"""Top-level Sector Scout orchestration for the CLI.

ANALYSIS ONLY. Builds the read-only data client, runs the analyzer, settles
open calls, computes the change log, renders both artifacts from the one
table, persists state, and hands off to the email composer. No order path is
reachable from here.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from .analyzer import build_run_table
from .changelog import compute_changelog
from .config import (
    load_sector_config,
    resolve_min_interval,
    resolve_state_dir,
)
from .data import SectorDataClient
from .email import EmailResult, build_subject, send_or_preview
from .report import render_docx_bytes, render_html
from .settlement import append_new_calls, evaluate_open_calls
from .state import StateStore, resolve_bucket


def run_sector_scout_email(
    *,
    dry_run: bool,
    top_n: int | None = None,
    out_path: str | None = None,
    client: Any = None,
    config: dict[str, Any] | None = None,
    print_fn: Any = print,
) -> EmailResult:
    """Analyze the universe and send (or preview) the Sector Scout report."""
    config = config if config is not None else load_sector_config()
    if top_n is not None:
        config = {**config, "selection": {**(config.get("selection") or {}), "max_plays": int(top_n)}}

    client = client or SectorDataClient(min_interval=resolve_min_interval(config))
    now = datetime.now(UTC)
    today = now.date()

    store = StateStore(
        resolve_state_dir(config),
        resolve_bucket(config),
        (config.get("state") or {}).get("gcs_prefix", "sector-scout"),
    )

    # 1. Settle yesterday's open calls first, so the email reports the record.
    settlement = evaluate_open_calls(store, client, today)

    # 2. Build the computed table (the single source of truth).
    table = build_run_table(client, config, store, today=today, now=now)

    # 3. Change log vs the previous stored run.
    prior = store.load_previous_run_table(today)
    changelog = compute_changelog(
        table, prior,
        reprice_threshold_pct=float(
            (config.get("changelog") or {}).get("reprice_threshold_pct", 10.0)
        ),
    )
    changelog_dict = {**asdict(changelog), "is_first_run": changelog.is_first_run, "quiet": changelog.quiet}

    # 4. Record this run's structures as open calls (idempotent per day).
    added = append_new_calls(store, table.get("plays") or [], today)
    if added:
        settlement.notes.append(f"{added} new call(s) recorded for settlement")

    # 5. Persist the run table for tomorrow's change log.
    wrote = store.save_run_table(today, table)
    if wrote == "failed":
        table.setdefault("notes", []).append(
            "run table could not be persisted; tomorrow's change log will miss this run"
        )

    # 6. Render both artifacts from the ONE table.
    settlement_line = settlement.line() + (
        "; " + "; ".join(settlement.notes) if settlement.notes else ""
    )
    html_body = render_html(table, changelog_dict, settlement_line)
    docx = render_docx_bytes(table, changelog_dict, settlement_line)

    return send_or_preview(
        subject=build_subject(table, today),
        html_body=html_body,
        docx_bytes=docx,
        config=config,
        today=today,
        dry_run=dry_run,
        out_path=out_path,
        print_fn=print_fn,
    )
