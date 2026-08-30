"""Config + secret resolution for the Small-Cap Scout.

ANALYSIS ONLY. Nothing here can place, review, or cancel an order.

Secrets and the from-address reuse the Options Scout's helpers, which follow the
house env-first-then-Secret-Manager pattern (the `secret-manager-credentials`
skill): a value already in the process env always wins; only when it is absent
do we shell out to the gcloud CLI the operator authenticates daily. A fetched
value is never written to disk and never logged.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

# Reuse the Options Scout secret + address helpers rather than re-implementing
# the house pattern. resolve_gmail_app_password does env-first then Secret
# Manager (GMAIL_VAULT_NAME, default "gmail-app-password") with the whitespace
# strip and the Windows gcloud.cmd fix; resolve_from_addr does GMAIL_USER-first.
from ..options_scout.config import (  # noqa: F401 (re-exported for callers)
    DEFAULT_EMAIL,
    GMAIL_VAULT_NAME,
    resolve_from_addr,
    resolve_gmail_app_password,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "smallcap_scout.yaml"


def load_scout_config(path: Path | None = None) -> dict[str, Any]:
    """Load config/smallcap_scout.yaml. Missing file -> empty dict (callers apply
    their own defaults), so a fresh checkout without the file still runs."""
    target = path or CONFIG_PATH
    if not target.exists():
        return {}
    with target.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def resolve_to_addr(config: dict[str, Any]) -> str:
    """Recipient: SMALLCAP_SCOUT_TO env, then config email.to_addr, then the
    house default. (The Options Scout analogue reads OPTIONS_SCOUT_TO; this lane
    uses its own env key so the two routines can target different inboxes.)"""
    return os.getenv("SMALLCAP_SCOUT_TO") or (config.get("email") or {}).get("to_addr") or DEFAULT_EMAIL
