"""Config + secret resolution for the Entry-Hit Alerter.

NOTIFICATION ONLY. Nothing here can place, review, or cancel an order.

Reuses the scouts' house secret helpers (env-first, then Secret Manager) so the
Gmail app password is resolved exactly one way across the three routines. The
alerter adds its own env overrides (ENTRY_ALERTS_TO, ENTRY_ALERTS_BUCKET) and
its own config file, but never its own secret path.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

# Reuse the scouts' resolver verbatim -- one secret path for all three routines.
from ..options_scout.config import resolve_gmail_app_password  # noqa: F401  (re-exported)

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "entry_alerts.yaml"
DEFAULT_EMAIL = "digitalsolomon.com@gmail.com"


def load_alerts_config(path: Path | None = None) -> dict[str, Any]:
    """Load config/entry_alerts.yaml. Missing file -> empty dict (callers apply
    their own defaults), so a fresh checkout without the file still runs."""
    target = path or CONFIG_PATH
    if not target.exists():
        return {}
    with target.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def resolve_from_addr(config: dict[str, Any]) -> str:
    return os.getenv("GMAIL_USER") or (config.get("email") or {}).get("from_addr") or DEFAULT_EMAIL


def resolve_to_addr(config: dict[str, Any]) -> str:
    return os.getenv("ENTRY_ALERTS_TO") or (config.get("email") or {}).get("to_addr") or DEFAULT_EMAIL


def resolve_bucket(config: dict[str, Any]) -> str | None:
    """GCS bucket for the shared day-state, env-first (ENTRY_ALERTS_BUCKET) then
    config (gcs.bucket). None means the local-file dev fallback is used."""
    env = os.getenv("ENTRY_ALERTS_BUCKET")
    if env:
        return env
    bucket = (config.get("gcs") or {}).get("bucket")
    return str(bucket) if bucket else None


def resolve_tolerance(config: dict[str, Any]) -> float:
    """Fractional band around the entry level within which a near-touch counts
    as a hit (e.g. 0.001 = 0.1%). Never negative."""
    try:
        tol = float((config.get("poll") or {}).get("tolerance", 0.001))
    except (TypeError, ValueError):
        tol = 0.001
    return max(tol, 0.0)


def resolve_enabled_sources(config: dict[str, Any]) -> set[str]:
    """Which scout sources the alerter watches. Defaults to both."""
    raw = config.get("enabled_sources")
    if not raw:
        return {"options", "smallcap"}
    return {str(s).strip().lower() for s in raw if str(s).strip()}
