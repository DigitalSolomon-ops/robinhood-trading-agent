"""Config + secret resolution for the Sector Scout.

ANALYSIS ONLY. Nothing here can place, review, or cancel an order.

Secrets follow the house env-first-then-Secret-Manager pattern via the
canonical helpers in options_scout.config (imported, not copied, so the two
lanes cannot drift). The Gmail app password and the Massive key are never
logged and never written to disk.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

# The canonical house secret path -- reuse, never fork.
from ..options_scout.config import (  # noqa: F401  (re-exported for the lane)
    DEFAULT_EMAIL,
    resolve_gmail_app_password,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "sector_scout.yaml"


def load_sector_config(path: Path | None = None) -> dict[str, Any]:
    """Load config/sector_scout.yaml. Missing file -> empty dict (callers apply
    their own defaults), so a fresh checkout without the file still runs."""
    target = path or CONFIG_PATH
    if not target.exists():
        return {}
    with target.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def resolve_from_addr(config: dict[str, Any]) -> str:
    return os.getenv("GMAIL_USER") or (config.get("email") or {}).get("from_addr") or DEFAULT_EMAIL


def resolve_to_addr(config: dict[str, Any]) -> str:
    """Recipient. Deliberately the operator address only on this lane -- no
    report-recipients list is consulted (see the brief: the second recipient
    was removed for Sector Scout)."""
    return os.getenv("SECTOR_SCOUT_TO") or (config.get("email") or {}).get("to_addr") or DEFAULT_EMAIL


def resolve_state_dir(config: dict[str, Any]) -> Path:
    """Local state directory (run tables, IV history, breadth cache, open
    calls). GCS is layered on top by state.py when a bucket is configured."""
    raw = (config.get("state") or {}).get("local_dir") or "data/sector_scout"
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path


def resolve_min_interval(config: dict[str, Any]) -> float:
    """Stock-side request spacing. Env wins (matches scout_settlement's
    MASSIVE_MIN_INTERVAL_SECONDS convention), then config, then 13s -- the
    observed ~5 req/min stock entitlement needs deterministic spacing."""
    raw = os.getenv("MASSIVE_MIN_INTERVAL_SECONDS")
    if raw:
        try:
            return max(float(raw), 0.0)
        except (TypeError, ValueError):
            pass
    try:
        return max(float((config.get("history") or {}).get("min_interval_seconds", 13.0)), 0.0)
    except (TypeError, ValueError):
        return 13.0
