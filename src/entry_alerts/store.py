"""Persist today's active plays and the set of alerts already fired.

ANALYSIS / NOTIFICATION ONLY. This module stores and reads back a small JSON
document; it has no order path and never touches a trading gate, the crypto
lane, or the equities order path.

The day's state lives in ONE JSON object per calendar day:

    {"date": "YYYY-MM-DD",
     "plays": {"<id>": {id, source, symbol, direction, entry, target, stop, date}, ...},
     "fired": ["<id>", ...]}

Primary store  : a Google Cloud Storage object gs://<bucket>/entry-alerts/<date>.json
Dev fallback   : a local file  data/entry_alerts/<date>.json

GCS is used when a bucket is resolvable (ENTRY_ALERTS_BUCKET / config) AND the
google-cloud-storage library imports. The import is LAZY and every GCS call is
guarded, so a lean container that lacks the library (or the bucket) simply falls
back to the local file rather than failing at import time -- the packaging
lesson from the scouts: nothing imported here can break a scout's email.

No secret is ever stored: the records are price levels and tickers only.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LOCAL_DIR = ROOT / "data" / "entry_alerts"
GCS_PREFIX = "entry-alerts"

# The recipients list lives at the BUCKET ROOT (not under the per-day prefix), so
# gs://<bucket>/recipients.json; local dev fallback is data/entry_alerts/recipients.json.
RECIPIENTS_OBJECT = "recipients.json"

# A day file name is exactly a calendar date; the recipients file is not, so the
# report-day lister can tell them apart by shape.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Deliberately permissive single-line email check. This is a display/config
# surface behind IAP, not an RFC-5322 validator; it only rejects the obvious
# garbage (no @, spaces, missing TLD) so a typo does not silently land.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SOURCE_OPTIONS = "options"
SOURCE_SMALLCAP = "smallcap"

# Bullish plays are entered on strength (price rising to/through entry); a put is
# the only bearish shape the scouts emit. Kept here so the alerter and the store
# agree on what "direction" means.
BULLISH_DIRECTIONS = frozenset({"call", "long"})
BEARISH_DIRECTIONS = frozenset({"put", "short"})


@dataclass(frozen=True)
class PlayRecord:
    """One day's active play, reduced to exactly what the alert needs. Levels
    are on the UNDERLYING; nothing here is an order or a live quote."""

    id: str
    source: str  # "options" | "smallcap"
    symbol: str
    direction: str  # "call" | "put" | "long"
    entry: float
    target: float
    stop: float
    date: str  # YYYY-MM-DD

    @property
    def bullish(self) -> bool:
        return self.direction.lower() in BULLISH_DIRECTIONS

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PlayRecord | None":
        try:
            return cls(
                id=str(raw["id"]),
                source=str(raw.get("source", "")),
                symbol=str(raw["symbol"]),
                direction=str(raw["direction"]),
                entry=float(raw["entry"]),
                target=float(raw["target"]),
                stop=float(raw["stop"]),
                date=str(raw.get("date", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class DayState:
    """The mutable per-day document: active plays keyed by id + the fired set."""

    date: str
    plays: dict[str, PlayRecord]
    fired: set[str]

    def to_json(self) -> str:
        return json.dumps(
            {
                "date": self.date,
                "plays": {pid: asdict(rec) for pid, rec in self.plays.items()},
                "fired": sorted(self.fired),
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def empty(cls, day: str) -> "DayState":
        return cls(date=day, plays={}, fired=set())

    @classmethod
    def from_json(cls, text: str, day: str) -> "DayState":
        try:
            raw = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return cls.empty(day)
        if not isinstance(raw, dict):
            return cls.empty(day)
        plays: dict[str, PlayRecord] = {}
        for pid, rec in (raw.get("plays") or {}).items():
            if isinstance(rec, dict):
                parsed = PlayRecord.from_dict(rec)
                if parsed is not None:
                    plays[str(pid)] = parsed
        fired = {str(x) for x in (raw.get("fired") or [])}
        return cls(date=str(raw.get("date") or day), plays=plays, fired=fired)


# --- id + record builders ----------------------------------------------------


def make_play_id(source: str, symbol: str, direction: str, day: str) -> str:
    """Deterministic id so the SAME play re-saved on the same day is the same
    record, and its fired-state survives a re-save. One entry alert per
    (source, symbol, direction) per day."""
    return f"{source}:{symbol.upper()}:{direction.lower()}:{day}"


def record_from_options_play(play: Any, day: str) -> PlayRecord | None:
    """Build a PlayRecord from an options_scout Play (duck-typed).

    Uses the Play's `entry` and its direction-aware `target`/`stop` properties.
    Returns None if a required level is missing/uncastable -- the caller swallows
    that, so a malformed play never breaks the email."""
    try:
        symbol = str(play.symbol)
        direction = str(play.direction)
        entry = float(play.entry)
        target = float(play.target)
        stop = float(play.stop)
    except (AttributeError, TypeError, ValueError):
        return None
    return PlayRecord(
        id=make_play_id(SOURCE_OPTIONS, symbol, direction, day),
        source=SOURCE_OPTIONS,
        symbol=symbol,
        direction=direction,
        entry=entry,
        target=target,
        stop=stop,
        date=day,
    )


def record_from_smallcap_pick(pick: Any, day: str) -> PlayRecord | None:
    """Build a PlayRecord from a smallcap_scout ScoutPick (duck-typed).

    Small-cap picks are long-only momentum continuations; their levels live on
    `pick.levels`. A pick without computed levels yields None (no entry to alert
    on)."""
    levels = getattr(pick, "levels", None)
    if levels is None:
        return None
    try:
        symbol = str(pick.symbol)
        entry = float(levels.entry)
        target = float(levels.target)
        stop = float(levels.stop)
    except (AttributeError, TypeError, ValueError):
        return None
    direction = "long"
    return PlayRecord(
        id=make_play_id(SOURCE_SMALLCAP, symbol, direction, day),
        source=SOURCE_SMALLCAP,
        symbol=symbol,
        direction=direction,
        entry=entry,
        target=target,
        stop=stop,
        date=day,
    )


# --- backend selection -------------------------------------------------------


def _object_name(day: str) -> str:
    return f"{GCS_PREFIX}/{day}.json"


def _local_path(day: str) -> Path:
    return LOCAL_DIR / f"{day}.json"


def _gcs_bucket(bucket_name: str | None):
    """Return a GCS Bucket handle, or None if unavailable. Lazy import so the
    library being absent (the lean scout containers) is a graceful fallback, not
    an import-time crash."""
    if not bucket_name:
        return None
    try:
        from google.cloud import storage  # type: ignore
    except Exception:
        return None
    try:
        client = storage.Client()
        return client.bucket(bucket_name)
    except Exception:
        return None


# --- load / save -------------------------------------------------------------


def load_day(day: str, *, bucket: str | None = None) -> DayState:
    """Load the day's state from GCS (preferred) or the local file, or an empty
    state when neither exists. Never raises on a missing/corrupt object."""
    gcs = _gcs_bucket(bucket)
    if gcs is not None:
        try:
            blob = gcs.blob(_object_name(day))
            if blob.exists():
                return DayState.from_json(blob.download_as_text(), day)
            return DayState.empty(day)
        except Exception:
            # fall through to local on any GCS hiccup
            pass
    path = _local_path(day)
    if path.exists():
        try:
            return DayState.from_json(path.read_text(encoding="utf-8"), day)
        except OSError:
            return DayState.empty(day)
    return DayState.empty(day)


def _persist(state: DayState, *, bucket: str | None) -> str:
    """Write the state back. Returns the backend used ("gcs" | "local")."""
    gcs = _gcs_bucket(bucket)
    if gcs is not None:
        try:
            gcs.blob(_object_name(state.date)).upload_from_string(
                state.to_json(), content_type="application/json"
            )
            return "gcs"
        except Exception:
            pass
    path = _local_path(state.date)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.to_json(), encoding="utf-8")
    return "local"


def _persist_raw(raw: dict[str, Any], day: str, *, bucket: str | None) -> str:
    """Write a RAW day document back verbatim -- preserving every field, including
    optional per-play fields the DayState dataclass would drop (rank, contract,
    and the settlement engine's `outcome`). GCS preferred, local fallback. Mirrors
    _persist but for a raw dict rather than a DayState."""
    text = json.dumps(raw, indent=2, sort_keys=True)
    gcs = _gcs_bucket(bucket)
    if gcs is not None:
        try:
            gcs.blob(_object_name(day)).upload_from_string(text, content_type="application/json")
            return "gcs"
        except Exception:
            pass
    path = _local_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return "local"


def save_outcomes(
    day: str, outcomes: dict[str, dict[str, Any]], *, bucket: str | None = None
) -> str:
    """Merge a settled `outcome` dict into named plays of a day document, PRESERVING
    every existing field (levels, the fired set, and any other per-play fields).

    Operates on the RAW document (never via DayState, which drops optional fields),
    so recording an outcome cannot clobber a play's other data or the fired set. A
    play id absent from the document is skipped. Returns the backend used
    ("gcs"|"local"), or "skipped" when the day has no document / no plays, or
    "unchanged" when nothing matched. Never raises on the normal missing-backend
    paths.

    INVARIANT: the settlement engine only writes to PAST days (a day needs a later
    session before it can settle), while the scouts (save_plays) and the alerter
    (mark_fired) only touch the CURRENT day. So this raw-merge never races a
    DayState write that would drop the outcomes it just wrote."""
    raw = load_report_raw(day, bucket=bucket)
    if raw is None:
        return "skipped"
    plays = raw.get("plays")
    if not isinstance(plays, dict):
        return "skipped"
    changed = False
    for pid, outcome in outcomes.items():
        rec = plays.get(pid)
        if isinstance(rec, dict) and outcome is not None:
            rec["outcome"] = outcome
            changed = True
    if not changed:
        return "unchanged"
    return _persist_raw(raw, day, bucket=bucket)


def save_plays(records: list[PlayRecord], *, day: str, bucket: str | None = None) -> str:
    """Merge `records` into the day's active plays and persist, PRESERVING the
    fired set. Additive: re-saving the same play id overwrites its levels but
    never resurrects or clears a fired flag. Returns the backend used.

    Callers (the scouts) wrap this in try/except so a persistence failure never
    breaks their email -- but this function itself is written not to raise on
    the normal missing-backend paths."""
    state = load_day(day, bucket=bucket)
    for rec in records:
        if rec is not None:
            state.plays[rec.id] = rec
    return _persist(state, bucket=bucket)


def mark_fired(ids: set[str], *, day: str, bucket: str | None = None) -> DayState:
    """Add `ids` to the day's fired set and persist. Returns the updated state.
    Idempotent: re-marking an already-fired id is a no-op on the set."""
    state = load_day(day, bucket=bucket)
    state.fired |= set(ids)
    _persist(state, bucket=bucket)
    return state


# --- report reads (RAW, for the dashboard) -----------------------------------
#
# The dashboard renders EXACTLY what is on disk/in the bucket, including optional
# fields the DayState dataclass drops (contract, conviction, rank, and the
# not-yet-written actual/outcome/return the settlement engine will add later).
# So these readers return the raw parsed JSON dicts rather than PlayRecords, and
# never raise -- a missing bucket/day/lib degrades to a local read then to empty.


def load_report_raw(day: str, *, bucket: str | None = None) -> dict[str, Any] | None:
    """Return the day's raw JSON document ({date, plays:{id:{...}}, fired:[...]}),
    or None when neither backend has it. Optional per-play fields are preserved
    verbatim so a future outcome/actual field 'just lights up'."""
    gcs = _gcs_bucket(bucket)
    if gcs is not None:
        try:
            blob = gcs.blob(_object_name(day))
            if blob.exists():
                return _parse_report(blob.download_as_text())
        except Exception:
            pass  # fall through to local on any GCS hiccup
    path = _local_path(day)
    if path.exists():
        try:
            return _parse_report(path.read_text(encoding="utf-8"))
        except OSError:
            return None
    return None


def _parse_report(text: str) -> dict[str, Any] | None:
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return raw if isinstance(raw, dict) else None


def list_report_days(limit: int = 14, *, bucket: str | None = None) -> list[str]:
    """The most recent calendar days that have a plays file, newest first, capped
    at `limit`. Reads GCS when available, else the local dir. Never raises."""
    days: set[str] = set()
    gcs = _gcs_bucket(bucket)
    if gcs is not None:
        try:
            prefix = f"{GCS_PREFIX}/"
            for blob in gcs.list_blobs(prefix=prefix):
                name = blob.name[len(prefix):]
                if name.endswith(".json") and _DATE_RE.match(name[:-5]):
                    days.add(name[:-5])
        except Exception:
            pass
    if not days and LOCAL_DIR.exists():
        try:
            for child in LOCAL_DIR.glob("*.json"):
                stem = child.stem
                if _DATE_RE.match(stem):
                    days.add(stem)
        except OSError:
            pass
    return sorted(days, reverse=True)[: max(int(limit), 0)]


def report_plays(raw: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Flatten a raw day document to a list of play dicts. Honors an optional
    per-record `rank` (else falls back to a stable symbol/id ordering) so the
    table has a deterministic order even though the store sorts by id."""
    if not isinstance(raw, dict):
        return []
    plays = raw.get("plays")
    if not isinstance(plays, dict):
        return []
    records = [rec for rec in plays.values() if isinstance(rec, dict)]
    records.sort(key=lambda r: (_rank_key(r), str(r.get("symbol", "")), str(r.get("id", ""))))
    return records


def _rank_key(rec: dict[str, Any]) -> float:
    try:
        return float(rec.get("rank"))
    except (TypeError, ValueError):
        return float("inf")


def play_outcome(rec: dict[str, Any]) -> dict[str, Any] | None:
    """The optional settled result for a play, from either `outcome` or `actual`.
    Returns None when the settlement engine has not filled one yet (the common
    case today). A present value may be a dict ({verdict, actual, return_pct,...})
    or a bare string ('WIN'/'LOSS'/'OPEN'); both are normalized to a dict."""
    raw = rec.get("outcome")
    if raw is None:
        raw = rec.get("actual")
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    return {"verdict": str(raw)}


def report_summary(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Small header stats for a day: play count and, only when outcomes exist,
    a running accuracy over the settled plays."""
    records = report_plays(raw)
    settled: list[str] = []
    for rec in records:
        outcome = play_outcome(rec)
        if outcome is not None:
            verdict = str(outcome.get("verdict", "")).strip().upper()
            if verdict in {"WIN", "LOSS"}:
                settled.append(verdict)
    summary: dict[str, Any] = {"plays": len(records), "settled": len(settled)}
    if settled:
        wins = sum(1 for v in settled if v == "WIN")
        summary["accuracy_pct"] = round(100.0 * wins / len(settled), 1)
    return summary


# --- recipients (report distribution list) -----------------------------------
#
# A tiny operator-managed distribution list stored as {"recipients": [...]}. The
# dashboard reads/writes it (behind IAP); the scouts read it best-effort to widen
# their send. It lives at the bucket ROOT so it is not mistaken for a day file.


def valid_email(value: str) -> bool:
    return bool(EMAIL_RE.match(value.strip())) if isinstance(value, str) else False


def _recipients_local_path() -> Path:
    return LOCAL_DIR / RECIPIENTS_OBJECT


def load_recipients(*, bucket: str | None = None) -> list[str]:
    """The current recipient list (deduped, order-preserving), or [] on any
    missing/corrupt/unreachable backend. Never raises."""
    gcs = _gcs_bucket(bucket)
    if gcs is not None:
        try:
            blob = gcs.blob(RECIPIENTS_OBJECT)
            if blob.exists():
                return _parse_recipients(blob.download_as_text())
        except Exception:
            pass
    path = _recipients_local_path()
    if path.exists():
        try:
            return _parse_recipients(path.read_text(encoding="utf-8"))
        except OSError:
            return []
    return []


def _parse_recipients(text: str) -> list[str]:
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    values = raw.get("recipients") if isinstance(raw, dict) else raw
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        addr = str(item).strip()
        key = addr.lower()
        if addr and key not in seen:
            seen.add(key)
            out.append(addr)
    return out


def save_recipients(recipients: list[str], *, bucket: str | None = None) -> str:
    """Persist the list as {"recipients":[...]}. Returns the backend used
    ("gcs" | "local"). Mirrors _persist: GCS preferred, local fallback."""
    payload = json.dumps({"recipients": recipients}, indent=2, sort_keys=True)
    gcs = _gcs_bucket(bucket)
    if gcs is not None:
        try:
            gcs.blob(RECIPIENTS_OBJECT).upload_from_string(
                payload, content_type="application/json"
            )
            return "gcs"
        except Exception:
            pass
    path = _recipients_local_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return "local"


def add_recipient(email: str, *, bucket: str | None = None) -> tuple[bool, str]:
    """Validate and add one address (case-insensitive dedupe). Returns
    (ok, message). Never writes an invalid or duplicate address."""
    addr = (email or "").strip()
    if not valid_email(addr):
        return False, f"Not a valid email address: {addr or '(empty)'}"
    current = load_recipients(bucket=bucket)
    if any(addr.lower() == existing.lower() for existing in current):
        return False, f"{addr} is already on the list"
    current.append(addr)
    save_recipients(current, bucket=bucket)
    return True, f"Added {addr}"


def remove_recipient(email: str, *, bucket: str | None = None) -> tuple[bool, str]:
    """Remove one address (case-insensitive). Returns (ok, message)."""
    addr = (email or "").strip()
    current = load_recipients(bucket=bucket)
    kept = [existing for existing in current if existing.lower() != addr.lower()]
    if len(kept) == len(current):
        return False, f"{addr or '(empty)'} was not on the list"
    save_recipients(kept, bucket=bucket)
    return True, f"Removed {addr}"


def recipients_for_send(default_addr: str, *, bucket: str | None = None) -> list[str]:
    """The full send set for a scout: the default operator address FIRST, then
    everyone on the list, deduped (case-insensitive). Best-effort -- any failure
    reading the list yields just [default_addr], so it can never break a send."""
    out = [default_addr]
    seen = {default_addr.strip().lower()}
    try:
        extra = load_recipients(bucket=bucket)
    except Exception:
        extra = []
    for addr in extra:
        key = addr.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(addr)
    return out
