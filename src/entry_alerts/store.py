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
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LOCAL_DIR = ROOT / "data" / "entry_alerts"
GCS_PREFIX = "entry-alerts"

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
