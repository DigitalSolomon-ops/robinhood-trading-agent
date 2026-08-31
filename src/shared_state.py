"""Shared arm/disarm state for the trading lanes.

The 007 dashboard's toggle and the trader loop must agree on whether a lane is
armed. Locally that agreement is the STOP_TRADING* file the trader already reads
(FileArmStore -- a lane is ARMED when its stop file is ABSENT). Deployed to
stateless Cloud Run, a local file is not shared across instances or with a
co-located trader, so the same interface is backed by Firestore instead
(FirestoreArmStore), one document per lane.

`build_arm_store` picks the backend: Firestore when TRADER_ARM_FIRESTORE_PROJECT
is set (the cloud service configures it), else the local files -- so local dev and
the existing trader are byte-for-byte unchanged. Every backend FAILS SAFE: an
unknown / unreadable state reads as DISARMED, never armed.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

LANES = ("crypto", "equities", "options")

_STOP_DEFAULT = {
    "crypto": "STOP_TRADING",
    "equities": "STOP_TRADING_EQUITIES",
    "options": "STOP_TRADING_OPTIONS",
}


def lane_stop_path(root: Path, rules: dict[str, Any], lane: str) -> Path:
    """The stop-file path for a lane, matching the kill-switch conventions:
    crypto = top-level kill_switch.stop_file; equities/options = their own
    <lane>.kill_switch.stop_file."""
    if lane == "crypto":
        name = (rules.get("kill_switch") or {}).get("stop_file", _STOP_DEFAULT["crypto"])
    else:
        name = ((rules.get(lane) or {}).get("kill_switch") or {}).get("stop_file", _STOP_DEFAULT[lane])
    return Path(root) / name


class ArmStore(Protocol):
    def is_armed(self, lane: str) -> bool: ...
    def set_armed(self, lane: str, armed: bool, by: str = "operator") -> None: ...


class FileArmStore:
    """Local backend: ARMED == the lane's stop file is ABSENT (the exact mechanism
    the trader loop already reads). Disarm writes it; arm removes it."""

    def __init__(self, root: Path, rules: dict[str, Any]) -> None:
        self.root = Path(root)
        self.rules = rules

    def _path(self, lane: str) -> Path:
        return lane_stop_path(self.root, self.rules, lane)

    def is_armed(self, lane: str) -> bool:
        if lane not in LANES:
            return False
        return not self._path(lane).exists()

    def set_armed(self, lane: str, armed: bool, by: str = "operator") -> None:
        if lane not in LANES:
            return
        path = self._path(lane)
        path.parent.mkdir(parents=True, exist_ok=True)
        if armed:
            if path.exists():
                path.unlink()
        else:
            path.write_text(
                f"{lane} DISARMED by {by} at {datetime.now(UTC).isoformat()}\n", encoding="utf-8"
            )


class FirestoreArmStore:
    """Cloud backend: arm state in Firestore, shared across the dashboard and a
    co-located trader. Document `<collection>/<lane>` = {armed, by, updated_at}.
    A missing/unreadable doc reads as DISARMED (fail safe)."""

    def __init__(self, project: str, collection: str = "trader_arm") -> None:
        from google.cloud import firestore  # lazy: optional dependency

        self._db = firestore.Client(project=project)
        self._collection = collection

    def is_armed(self, lane: str) -> bool:
        if lane not in LANES:
            return False
        try:
            doc = self._db.collection(self._collection).document(lane).get()
            return bool((doc.to_dict() or {}).get("armed")) if doc.exists else False
        except Exception:
            return False  # fail safe: unreadable state is DISARMED, never armed

    def set_armed(self, lane: str, armed: bool, by: str = "operator") -> None:
        if lane not in LANES:
            return
        self._db.collection(self._collection).document(lane).set(
            {"armed": bool(armed), "by": by, "updated_at": datetime.now(UTC).isoformat()}
        )


def build_arm_store(root: Path, rules: dict[str, Any]) -> ArmStore:
    """Firestore when TRADER_ARM_FIRESTORE_PROJECT is set (the cloud service), else
    the local stop files -- so local runs and the trader loop are unchanged."""
    project = os.getenv("TRADER_ARM_FIRESTORE_PROJECT", "").strip()
    if project:
        try:
            return FirestoreArmStore(project)
        except Exception:
            pass  # any Firestore/import failure -> local files, never crash the surface
    return FileArmStore(Path(root), rules)
