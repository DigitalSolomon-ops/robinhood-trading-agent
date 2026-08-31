"""Entry-alert store: record builders + local-file persistence with a preserved
fired set. The GCS path is not exercised here (no bucket -> local fallback);
tests pin the day-state JSON round-trip and the additive save semantics.
"""

from __future__ import annotations

import types

import pytest

from src.entry_alerts import store


@pytest.fixture(autouse=True)
def local_store(tmp_path, monkeypatch):
    """Force the local-file backend into a temp dir, with no GCS bucket."""
    monkeypatch.setattr(store, "LOCAL_DIR", tmp_path / "entry_alerts")
    monkeypatch.delenv("ENTRY_ALERTS_BUCKET", raising=False)
    return tmp_path


DAY = "2026-08-31"


def _options_play(symbol="AAPL", direction="call", entry=100.0, target=110.0, stop=95.0):
    return types.SimpleNamespace(symbol=symbol, direction=direction, entry=entry,
                                 target=target, stop=stop)


def _smallcap_pick(symbol="WINR", entry=6.0, target=7.2, stop=5.4, with_levels=True):
    levels = types.SimpleNamespace(entry=entry, target=target, stop=stop) if with_levels else None
    return types.SimpleNamespace(symbol=symbol, levels=levels)


def test_record_from_options_play_uses_direction_aware_levels():
    rec = store.record_from_options_play(_options_play(), DAY)
    assert rec is not None
    assert rec.source == "options"
    assert rec.symbol == "AAPL"
    assert rec.direction == "call"
    assert (rec.entry, rec.target, rec.stop) == (100.0, 110.0, 95.0)
    assert rec.bullish is True
    assert rec.id == "options:AAPL:call:2026-08-31"


def test_record_from_put_is_bearish():
    rec = store.record_from_options_play(_options_play(direction="put"), DAY)
    assert rec.bullish is False


def test_record_from_smallcap_pick_is_long():
    rec = store.record_from_smallcap_pick(_smallcap_pick(), DAY)
    assert rec is not None
    assert rec.source == "smallcap"
    assert rec.direction == "long"
    assert rec.bullish is True
    assert rec.id == "smallcap:WINR:long:2026-08-31"


def test_smallcap_pick_without_levels_yields_none():
    assert store.record_from_smallcap_pick(_smallcap_pick(with_levels=False), DAY) is None


def test_save_then_load_round_trips_records():
    rec = store.record_from_options_play(_options_play(), DAY)
    backend = store.save_plays([rec], day=DAY, bucket=None)
    assert backend == "local"

    state = store.load_day(DAY, bucket=None)
    assert set(state.plays) == {rec.id}
    assert state.plays[rec.id].entry == 100.0
    assert state.fired == set()


def test_save_is_additive_and_preserves_the_fired_set():
    a = store.record_from_options_play(_options_play(symbol="AAPL"), DAY)
    b = store.record_from_options_play(_options_play(symbol="MSFT"), DAY)
    store.save_plays([a], day=DAY, bucket=None)

    # fire A, then save B -- A's fired flag must survive the second save.
    store.mark_fired({a.id}, day=DAY, bucket=None)
    store.save_plays([b], day=DAY, bucket=None)

    state = store.load_day(DAY, bucket=None)
    assert set(state.plays) == {a.id, b.id}
    assert state.fired == {a.id}


def test_mark_fired_is_idempotent():
    rec = store.record_from_options_play(_options_play(), DAY)
    store.save_plays([rec], day=DAY, bucket=None)
    store.mark_fired({rec.id}, day=DAY, bucket=None)
    store.mark_fired({rec.id}, day=DAY, bucket=None)
    assert store.load_day(DAY, bucket=None).fired == {rec.id}


def test_load_missing_day_is_empty():
    state = store.load_day("2099-01-01", bucket=None)
    assert state.plays == {}
    assert state.fired == set()
