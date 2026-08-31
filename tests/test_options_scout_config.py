"""Fail-fast secret resolution: a stale gcloud token must fail in seconds with a
remedy, never hang on the secret-access call."""
from __future__ import annotations

import pytest

from src.options_scout import config as cfg


def test_no_gcloud_env_short_circuits(monkeypatch):
    monkeypatch.setenv("DS_VAULT_NO_GCLOUD", "1")
    # Must not even attempt a subprocess when the gcloud path is disabled.
    monkeypatch.setattr(cfg.subprocess, "run", lambda *a, **k: pytest.fail("no subprocess expected"))
    assert cfg._secret_from_manager("massive-api") is None


def test_stale_auth_fails_fast_without_secret_access(monkeypatch):
    monkeypatch.delenv("DS_VAULT_NO_GCLOUD", raising=False)
    monkeypatch.setattr(cfg, "_gcloud_auth_alive", lambda gcloud: False)
    monkeypatch.setattr(
        cfg.subprocess, "run",
        lambda *a, **k: pytest.fail("secret-access must not run when auth is stale"),
    )
    assert cfg._secret_from_manager("massive-api") is None


def test_live_auth_reaches_secret_access(monkeypatch):
    monkeypatch.delenv("DS_VAULT_NO_GCLOUD", raising=False)
    monkeypatch.setattr(cfg, "_gcloud_auth_alive", lambda gcloud: True)

    class _R:
        returncode = 0
        stdout = "the-secret-value\n"
        stderr = ""

    monkeypatch.setattr(cfg.subprocess, "run", lambda *a, **k: _R())
    assert cfg._secret_from_manager("massive-api") == "the-secret-value"
