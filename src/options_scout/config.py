"""Config + secret resolution for the Options Scout.

ANALYSIS ONLY. Nothing here can place, review, or cancel an order.

Secrets follow the house env-first-then-Secret-Manager pattern (the
`secret-manager-credentials` skill): a value already in the process env always
wins; only when it is absent do we shell out to the gcloud CLI the operator
authenticates daily. A fetched value is never written to disk and never logged.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "options_scout.yaml"

# Secret Manager name for the Gmail app password (override at runtime with the
# GMAIL_VAULT_NAME env var). Named GMAIL_VAULT_NAME (not ..._SECRET_...) so the
# repo's own credential-shaped-literal scanner reads it as the vault key NAME it
# is, not a leaked value.
GMAIL_VAULT_NAME = "gmail-app-password"
DEFAULT_EMAIL = "digitalsolomon.com@gmail.com"


def load_scout_config(path: Path | None = None) -> dict[str, Any]:
    """Load config/options_scout.yaml. Missing file -> empty dict (callers apply
    their own defaults), so a fresh checkout without the file still runs."""
    target = path or CONFIG_PATH
    if not target.exists():
        return {}
    with target.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _gcloud_auth_alive(gcloud: str) -> bool:
    """Fast (~6s) preflight that gcloud can actually mint a token. Stale ADC (the
    daily Workspace expiry) makes the secret-access call HANG until its timeout;
    checking auth first lets a stale token fail in seconds with the exact remedy
    instead of a 30s hang. Mirrors the vault getSecretFast fail-fast pattern."""
    try:
        result = subprocess.run(
            [gcloud, "auth", "application-default", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=6,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def _secret_from_manager(name: str) -> str | None:
    """Secret Manager half of the house pattern. Never raises, never logs the
    value. Disabled on Cloud Run/CI via DS_VAULT_NO_GCLOUD (ADC/SDK is the only
    path there and gcloud is not installed)."""
    if os.getenv("DS_VAULT_NO_GCLOUD"):
        return None
    # On Windows `gcloud` is gcloud.cmd; a bare "gcloud" argv[0] raises
    # FileNotFoundError under subprocess without a shell. Resolve the real path.
    gcloud = shutil.which("gcloud") or "gcloud"
    # Fail FAST on stale auth rather than hanging on the secret-access call.
    if not _gcloud_auth_alive(gcloud):
        sys.stderr.write(
            "[vault] gcloud auth is stale/unavailable -- run: "
            "gcloud auth application-default login\n"
        )
        return None
    try:
        result = subprocess.run(
            [gcloud, "secrets", "versions", "access", "latest", f"--secret={name}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def resolve_gmail_app_password(explicit: str | None = None) -> str:
    """Env-first (GMAIL_APP_PASSWORD), then Secret Manager (GMAIL_VAULT_NAME,
    default "gmail-app-password"). Returns "" when neither is set. The value is
    never logged."""
    if explicit:
        return explicit
    value = os.getenv("GMAIL_APP_PASSWORD", "")
    if value:
        return value
    fetched = _secret_from_manager(os.getenv("GMAIL_VAULT_NAME") or GMAIL_VAULT_NAME)
    if fetched:
        # Google displays a 16-char app password in groups of four; the spaces are
        # cosmetic and must be stripped before SMTP AUTH or the login is rejected.
        fetched = "".join(fetched.split())
        os.environ.setdefault("GMAIL_APP_PASSWORD", fetched)
        return fetched
    return ""


def resolve_from_addr(config: dict[str, Any]) -> str:
    return os.getenv("GMAIL_USER") or (config.get("email") or {}).get("from_addr") or DEFAULT_EMAIL


def resolve_to_addr(config: dict[str, Any]) -> str:
    return os.getenv("OPTIONS_SCOUT_TO") or (config.get("email") or {}).get("to_addr") or DEFAULT_EMAIL
