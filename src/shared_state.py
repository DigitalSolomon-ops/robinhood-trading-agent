"""Shared arm/disarm state for the trading lanes.

The 007 dashboard's toggle and the trader loop must agree on whether a lane is
armed. The two lanes that predate this store keep their original meaning: crypto
and equities are ARMED when their STOP_TRADING* file is ABSENT (FileArmStore --
the exact mechanism the trader already reads), so local dev and the existing
trader are byte-for-byte unchanged.

The OPTIONS lane is different on purpose. Options are leveraged, so its arm state
is POSITIVE and fail-CLOSED: a lane is armed ONLY when its own ARM-STATE marker is
PRESENT (a distinct file, NOT the kill-switch stop file). The default -- no marker
-- is DISARMED, and the dashboard toggle is what writes the marker. Absence can
never read as armed for options, and the arm marker is kept separate from
STOP_TRADING_OPTIONS so the emergency kill switch and the arm gate are two
independent controls.

Deployed to stateless Cloud Run, a local file is not shared across instances or
with a co-located trader, so the same interface is backed by Firestore instead
(FirestoreArmStore), one document per lane -- already positive and fail-closed
(a missing/unreadable doc reads DISARMED for every lane).

`build_arm_store` picks the backend: Firestore when TRADER_ARM_FIRESTORE_PROJECT
is set (the cloud service configures it), else the local files. When that env var
IS set but the Firestore backend cannot be constructed, the store does NOT fall
back to local files -- that would silently re-open the fail-OPEN hole for a
cloud service that has no local state -- it returns an always-DISARMED store and
logs loudly. Every backend FAILS SAFE: an unknown / unreadable / unrequested
state reads as DISARMED, never armed.
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

_LOG = logging.getLogger(__name__)

LANES = ("crypto", "equities", "options")

_STOP_DEFAULT = {
    "crypto": "STOP_TRADING",
    "equities": "STOP_TRADING_EQUITIES",
    "options": "STOP_TRADING_OPTIONS",
}

# Lanes whose arm state is a POSITIVE marker (present == armed), kept in a file
# distinct from the lane's kill-switch stop file. Only the leveraged options lane
# is fail-closed this way; crypto and equities keep absence-of-stop-file == armed.
_ARM_MARKER = {
    "options": "ARM_STATE_OPTIONS",
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


def lane_arm_marker_path(root: Path, lane: str) -> Path | None:
    """The POSITIVE arm-state marker path for a lane, or None when the lane does
    not use one (crypto/equities track arm state via their stop file instead).

    Deliberately a DIFFERENT file from lane_stop_path: the arm marker says the
    lane is armed, the stop file is the emergency kill switch, and conflating the
    two is exactly the fail-open bug this separation closes."""
    name = _ARM_MARKER.get(lane)
    return Path(root) / name if name else None


class ArmStore(Protocol):
    def is_armed(self, lane: str) -> bool: ...
    def set_armed(self, lane: str, armed: bool, by: str = "operator") -> None: ...


class FileArmStore:
    """Local backend. Crypto/equities are ARMED when their stop file is ABSENT
    (the exact mechanism the trader loop already reads; disarm writes it, arm
    removes it). The OPTIONS lane is POSITIVE and fail-closed: ARMED only when its
    own ARM-STATE marker is PRESENT -- absence reads DISARMED, and arm writes the
    marker while disarm removes it. The options marker is a separate file from the
    kill-switch stop file."""

    def __init__(self, root: Path, rules: dict[str, Any]) -> None:
        self.root = Path(root)
        self.rules = rules

    def _path(self, lane: str) -> Path:
        return lane_stop_path(self.root, self.rules, lane)

    def _marker(self, lane: str) -> Path | None:
        return lane_arm_marker_path(self.root, lane)

    def is_armed(self, lane: str) -> bool:
        if lane not in LANES:
            return False
        marker = self._marker(lane)
        if marker is not None:
            # POSITIVE + fail-closed: armed only when the marker is present.
            return marker.exists()
        return not self._path(lane).exists()

    def set_armed(self, lane: str, armed: bool, by: str = "operator") -> None:
        if lane not in LANES:
            return
        marker = self._marker(lane)
        if marker is not None:
            marker.parent.mkdir(parents=True, exist_ok=True)
            if armed:
                marker.write_text(
                    f"{lane} arm marker set by {by} at {datetime.now(UTC).isoformat()}\n",
                    encoding="utf-8",
                )
            elif marker.exists():
                marker.unlink()
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
    Already POSITIVE and fail-closed for every lane: a missing/unreadable doc
    reads as DISARMED (armed only when the doc explicitly says so)."""

    def __init__(self, project: str, collection: str = "trader_arm", client: Any | None = None) -> None:
        # `client` is an injection seam ONLY: production passes nothing and the
        # real firestore.Client is built lazily (the dependency is optional and
        # cloud-only). A test supplies a fake client so the fail-closed branches of
        # is_armed can be exercised without google.cloud installed.
        if client is None:
            from google.cloud import firestore  # lazy: optional dependency

            client = firestore.Client(project=project)
        self._db = client
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


class DisarmedArmStore:
    """Fail-closed store: EVERY lane reads DISARMED and arming is a no-op.

    Returned when a cloud (Firestore) arm backend was REQUESTED
    (TRADER_ARM_FIRESTORE_PROJECT is set) but could not be constructed. Falling
    back to the local FileArmStore in that situation would fail OPEN for a cloud
    service that has no local arm state, so instead nothing is ever armed until
    the real cloud backend is reachable again."""

    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    def is_armed(self, lane: str) -> bool:
        return False

    def set_armed(self, lane: str, armed: bool, by: str = "operator") -> None:
        # A broken cloud backend cannot be armed from here; refuse silently-safe.
        return None


def build_arm_store(root: Path, rules: dict[str, Any]) -> ArmStore:
    """Firestore when TRADER_ARM_FIRESTORE_PROJECT is set (the cloud service), else
    the local files -- so local runs and the trader loop are unchanged.

    If the env var is set but the Firestore store cannot be constructed, return an
    always-DISARMED store (never the local fail-open fallback) and log loudly."""
    project = os.getenv("TRADER_ARM_FIRESTORE_PROJECT", "").strip()
    if project:
        try:
            return FirestoreArmStore(project)
        except Exception:
            _LOG.error(
                "TRADER_ARM_FIRESTORE_PROJECT=%r is set but the Firestore arm store could not be "
                "constructed; refusing to fall back to local files (that would fail OPEN). Every lane "
                "reads DISARMED until the cloud arm backend is reachable.",
                project,
                exc_info=True,
            )
            return DisarmedArmStore(reason=f"firestore arm store unavailable for project {project!r}")
    return FileArmStore(Path(root), rules)
