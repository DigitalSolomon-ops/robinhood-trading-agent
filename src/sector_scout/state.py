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
RH_SNAPSHOT_FILE = "rh_snapshot.json"


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


def _generated_ts(payload: dict[str, Any]) -> float:
    """generated_at as an epoch, 0.0 when absent/unparseable (so a blob with
    no timestamp never blocks a real push)."""
    from datetime import UTC, datetime

    raw = str(payload.get("generated_at") or "")
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    except ValueError:
        return 0.0


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

    # --- the Robinhood snapshot (a scheduled agent session pushes it) -----------

    def materialize_rh_snapshot(self) -> Path | None:
        """Pull the connector-filled Robinhood snapshot (the FRESHER of the
        GCS blob and any local copy, via _read) into the local state dir and
        return its path; None when neither side has one. Staleness is judged
        downstream by RhSnapshot.is_fresh -- an old snapshot still loads and
        the report states its age rather than pretending it is absent."""
        payload = self._read(RH_SNAPSHOT_FILE)
        if not isinstance(payload, dict):
            return None
        path = self.local / RH_SNAPSHOT_FILE
        try:
            path.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
        except OSError:
            return None
        return path

    def push_rh_snapshot(self, source: Path, *, force: bool = False) -> str:
        """Publish a snapshot file to the store (local always, GCS when
        configured). The caller validates the schema FIRST (load_snapshot)
        so a malformed file can never shadow a good blob. Two extra refusals
        here (review findings, 2026-09-07): non-finite numerics never
        persist, and a push OLDER than the stored blob is refused unless
        forced -- a rerun of yesterday's fill must not shadow today's."""
        try:
            payload = json.loads(Path(source).read_text(encoding="utf-8"))
            json.dumps(payload, allow_nan=False)
        except (OSError, ValueError):
            return "refused_unreadable_or_nonfinite"
        if not force:
            existing = self._read(RH_SNAPSHOT_FILE)
            if isinstance(existing, dict) and _generated_ts(existing) > _generated_ts(payload):
                return "refused_older_than_stored"
        return self._write(RH_SNAPSHOT_FILE, payload)

    # --- breadth cache sync (Cloud Run has no persistent disk) ------------------

    def sync_breadth_down(self) -> int:
        """Download breadth-cache day files present in GCS but absent locally.
        Without this, a Cloud Run job starts with an empty cache every
        execution and the breadth factor never converges. No-op (0) when no
        bucket is configured. Failures download what they can -- coverage is
        reported honestly either way."""
        if self._bucket is None:
            return 0
        fetched = 0
        try:
            prefix = f"{self.prefix}/{BREADTH_SUBDIR}/"
            for blob in self._bucket.list_blobs(prefix=prefix):
                name = blob.name.rsplit("/", 1)[-1]
                if not name.endswith(".json.gz"):
                    continue
                target = self.breadth_dir / name
                if target.exists():
                    continue
                try:
                    blob.download_to_filename(str(target))
                    fetched += 1
                except Exception:
                    # A partial download must not poison the cache.
                    try:
                        target.unlink()
                    except OSError:
                        pass
        except Exception:
            pass
        return fetched

    def sync_breadth_up(self, known_remote: set[str] | None = None) -> int:
        """Upload local breadth-cache day files missing from GCS. Returns how
        many uploaded; no-op when no bucket is configured."""
        if self._bucket is None:
            return 0
        uploaded = 0
        try:
            prefix = f"{self.prefix}/{BREADTH_SUBDIR}/"
            remote = known_remote
            if remote is None:
                remote = {
                    b.name.rsplit("/", 1)[-1]
                    for b in self._bucket.list_blobs(prefix=prefix)
                }
            for path in sorted(self.breadth_dir.glob("*.json.gz")):
                if path.name in remote:
                    continue
                try:
                    self._bucket.blob(prefix + path.name).upload_from_filename(str(path))
                    uploaded += 1
                except Exception:
                    break  # a bucket outage: stop, local copies remain
        except Exception:
            pass
        return uploaded
