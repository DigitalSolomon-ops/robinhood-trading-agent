"""Sector Scout persisted state: run tables (for the change log), the
self-collected ATM IV history (for IV rank), and the open six-month calls
(for settlement).

Storage follows the entry-alerts store pattern: GCS when a bucket is
configured and reachable (lazy import, every failure degrades to local), the
repo-local data directory otherwise. Every read/write is exception-guarded --
state is a bonus, the email is the product -- EXCEPT that a run table that
cannot be persisted is reported in the email's method section, because the
next day's change log depends on it.

ANALYSIS ONLY -- no order path.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any

RUNS_SUBDIR = "runs"
CALLS_FILE = "open_calls.json"
IV_FILE = "iv_history.json"
BREADTH_SUBDIR = "breadth"


def resolve_bucket(config: dict[str, Any]) -> str | None:
    """SECTOR_SCOUT_BUCKET env, else the entry-alerts bucket config (shared
    infrastructure), else None (local files)."""
    env = os.getenv("SECTOR_SCOUT_BUCKET")
    if env:
        return env
    try:
        from ..entry_alerts.config import load_alerts_config, resolve_bucket as _rb

        return _rb(load_alerts_config())
    except Exception:
        return None


def _gcs_bucket(bucket_name: str | None):
    if not bucket_name:
        return None
    try:
        from google.cloud import storage  # lazy: lean local runs need no GCP dep

        return storage.Client().bucket(bucket_name)
    except Exception:
        return None


class StateStore:
    """One store instance per run. GCS layered over the local directory; the
    local copy is always written so a bucket outage never loses the run."""

    def __init__(self, local_dir: Path, bucket_name: str | None, gcs_prefix: str) -> None:
        self.local = local_dir
        self.local.mkdir(parents=True, exist_ok=True)
        (self.local / RUNS_SUBDIR).mkdir(exist_ok=True)
        (self.local / BREADTH_SUBDIR).mkdir(exist_ok=True)
        self.prefix = gcs_prefix.strip("/")
        self._bucket = _gcs_bucket(bucket_name)

    @property
    def breadth_dir(self) -> Path:
        return self.local / BREADTH_SUBDIR

    # --- generic json blob ----------------------------------------------------

    def _write(self, rel: str, payload: Any) -> str:
        text = json.dumps(payload, indent=1, default=str)
        path = self.local / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(text, encoding="utf-8")
            wrote = "local"
        except OSError:
            wrote = "failed"
        if self._bucket is not None:
            try:
                self._bucket.blob(f"{self.prefix}/{rel.replace(os.sep, '/')}").upload_from_string(
                    text, content_type="application/json"
                )
                wrote = "gcs+local" if wrote == "local" else "gcs"
            except Exception:
                pass
        return wrote

    def _read(self, rel: str) -> Any | None:
        """Read the FRESHER of the GCS blob and the local file, by timestamp.

        The local copy is always written; a GCS upload can silently fail. If
        GCS were preferred unconditionally, a stale cloud blob would resurrect
        over newer local state and the next write would clobber real data
        (review finding, 2026-09-04). Comparing blob.updated to the local
        mtime makes the outage self-healing: local wins until the next
        successful upload."""
        gcs_payload = gcs_ts = None
        if self._bucket is not None:
            try:
                blob = self._bucket.blob(f"{self.prefix}/{rel.replace(os.sep, '/')}")
                if blob.exists():
                    blob.reload()
                    gcs_payload = json.loads(blob.download_as_text())
                    updated = getattr(blob, "updated", None)
                    gcs_ts = updated.timestamp() if updated is not None else 0.0
            except Exception:
                gcs_payload = gcs_ts = None
        local_payload = local_ts = None
        path = self.local / rel
        if path.exists():
            try:
                local_payload = json.loads(path.read_text(encoding="utf-8"))
                local_ts = path.stat().st_mtime
            except (OSError, ValueError):
                local_payload = local_ts = None
        if gcs_payload is not None and local_payload is not None:
            return gcs_payload if (gcs_ts or 0.0) >= (local_ts or 0.0) else local_payload
        return gcs_payload if gcs_payload is not None else local_payload

    # --- run tables (change-log source) ----------------------------------------

    def save_run_table(self, run_date: date, table: dict[str, Any]) -> str:
        return self._write(f"{RUNS_SUBDIR}/{run_date.isoformat()}.json", table)

    def load_run_table(self, run_date: date) -> dict[str, Any] | None:
        return self._read(f"{RUNS_SUBDIR}/{run_date.isoformat()}.json")

    def load_previous_run_table(self, before: date, lookback_days: int = 14) -> dict[str, Any] | None:
        """The most recent stored run strictly before `before`. Checks GCS
        then local per day, newest first."""
        from datetime import timedelta

        for back in range(1, lookback_days + 1):
            day = before - timedelta(days=back)
            table = self._read(f"{RUNS_SUBDIR}/{day.isoformat()}.json")
            if table is not None:
                return table
        return None

    # --- IV history (self-collected; the plan has no historical IV) ------------

    def load_iv_history(self) -> dict[str, dict[str, float]]:
        raw = self._read(IV_FILE)
        return raw if isinstance(raw, dict) else {}

    def record_iv(self, run_date: date, atm_iv_by_fund: dict[str, float]) -> None:
        history = self.load_iv_history()
        day = run_date.isoformat()
        for fund, iv in atm_iv_by_fund.items():
            if iv is None:
                continue
            history.setdefault(fund, {})[day] = round(float(iv), 5)
        # Trim beyond ~2 windows so the file never grows unbounded.
        for fund, series in history.items():
            if len(series) > 550:
                keep = sorted(series)[-550:]
                history[fund] = {k: series[k] for k in keep}
        self._write(IV_FILE, history)

    # --- open six-month calls (settlement state) --------------------------------

    def load_open_calls(self) -> list[dict[str, Any]]:
        raw = self._read(CALLS_FILE)
        return raw if isinstance(raw, list) else []

    def save_open_calls(self, calls: list[dict[str, Any]]) -> str:
        return self._write(CALLS_FILE, calls)
