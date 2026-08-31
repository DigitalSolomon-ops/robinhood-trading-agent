"""The scout -> entry-alert-store hook is additive and failure-safe: it persists
the day's plays, and a persistence failure NEVER breaks the scout email.

The email renderer is stubbed here (the dedicated scout email tests cover
rendering): these tests isolate the hook -- that it runs, persists, and that its
exceptions never propagate past the runner into the email step.
"""

from __future__ import annotations

import types
from datetime import UTC, datetime

import pytest

from src.entry_alerts import store
from src.options_scout import runner as options_runner
from src.smallcap_scout import runner as smallcap_runner

SENTINEL = types.SimpleNamespace(dry_run=True, sent=False, subject="stub")


@pytest.fixture(autouse=True)
def local_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "LOCAL_DIR", tmp_path / "entry_alerts")
    monkeypatch.delenv("ENTRY_ALERTS_BUCKET", raising=False)
    # Stub the email step so the hook is tested in isolation from rendering.
    monkeypatch.setattr(options_runner, "send_or_preview", lambda *a, **k: SENTINEL)
    monkeypatch.setattr(smallcap_runner, "send_or_preview", lambda *a, **k: SENTINEL)
    return tmp_path


TODAY = datetime.now(UTC).date().isoformat()


def _options_play(symbol="AAPL", direction="call"):
    return types.SimpleNamespace(symbol=symbol, direction=direction,
                                 entry=100.0, target=110.0, stop=95.0)


def _smallcap_pick(symbol="WINR"):
    levels = types.SimpleNamespace(entry=6.0, target=7.2, stop=5.4)
    return types.SimpleNamespace(symbol=symbol, levels=levels)


def test_options_scout_persists_plays_to_the_store(monkeypatch):
    monkeypatch.setattr(options_runner, "scout_plays", lambda *a, **k: [_options_play()])

    result = options_runner.run_options_scout_email(
        dry_run=True, client=object(), config={"email": {}}, print_fn=lambda *_: None
    )
    assert result is SENTINEL  # the email step was reached and returned
    state = store.load_day(TODAY, bucket=None)
    assert any(rec.symbol == "AAPL" for rec in state.plays.values())


def test_smallcap_scout_persists_picks_to_the_store(monkeypatch):
    monkeypatch.setattr(smallcap_runner, "scan", lambda *a, **k: [_smallcap_pick()])

    result = smallcap_runner.run_smallcap_scout_email(
        dry_run=True, client=object(), config={"email": {}}, print_fn=lambda *_: None
    )
    assert result is SENTINEL
    state = store.load_day(TODAY, bucket=None)
    assert any(rec.symbol == "WINR" and rec.direction == "long"
               for rec in state.plays.values())


def test_a_store_failure_never_breaks_the_options_email(monkeypatch):
    monkeypatch.setattr(options_runner, "scout_plays", lambda *a, **k: [_options_play()])

    def boom(*a, **k):
        raise RuntimeError("GCS is down")

    monkeypatch.setattr(store, "save_plays", boom)

    # The exception is swallowed and the email step still runs.
    result = options_runner.run_options_scout_email(
        dry_run=True, client=object(), config={"email": {}}, print_fn=lambda *_: None
    )
    assert result is SENTINEL


def test_a_store_failure_never_breaks_the_smallcap_email(monkeypatch):
    monkeypatch.setattr(smallcap_runner, "scan", lambda *a, **k: [_smallcap_pick()])

    def boom(*a, **k):
        raise RuntimeError("GCS is down")

    monkeypatch.setattr(store, "save_plays", boom)

    result = smallcap_runner.run_smallcap_scout_email(
        dry_run=True, client=object(), config={"email": {}}, print_fn=lambda *_: None
    )
    assert result is SENTINEL
