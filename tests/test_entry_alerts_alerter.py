"""The entry-hit poll cycle: direction-aware detection, one-time / idempotent
semantics, the market-hours guard, and a side-effect-free dry-run. All network
(prices + SMTP) and the store (temp local dir) are mocked.
"""

from __future__ import annotations

import types
from datetime import datetime

import pytest

from src.entry_alerts import alerter, store
from src.entry_alerts.store import PlayRecord

# A weekday, non-holiday, inside RTH (12:00) and before the open (08:00).
IN_HOURS = datetime(2026, 8, 31, 12, 0)
PRE_OPEN = datetime(2026, 8, 31, 8, 0)
DAY = "2026-08-31"


@pytest.fixture(autouse=True)
def local_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "LOCAL_DIR", tmp_path / "entry_alerts")
    monkeypatch.delenv("ENTRY_ALERTS_BUCKET", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.setenv("DS_VAULT_NO_GCLOUD", "1")  # no Secret Manager fallback in tests
    return tmp_path


class FakePriceClient:
    def __init__(self, prices):
        self.prices = prices

    def get_current_price(self, symbol, on_date=None):
        return self.prices.get(symbol)


def _rec(symbol, direction, entry, target, stop, source="options"):
    return PlayRecord(
        id=store.make_play_id(source, symbol, direction, DAY),
        source=source, symbol=symbol, direction=direction,
        entry=entry, target=target, stop=stop, date=DAY,
    )


CONFIG = {"email": {}, "poll": {"tolerance": 0.001}}


def _boom_smtp():
    raise AssertionError("no SMTP connection may be opened in this test")


# --- entry_hit direction logic ----------------------------------------------


def test_entry_hit_bullish_fires_at_or_above_entry():
    call = _rec("AAPL", "call", 100.0, 110.0, 95.0)
    assert alerter.entry_hit(call, 100.0, 0.001) is True
    assert alerter.entry_hit(call, 105.0, 0.001) is True   # blew through, upward
    assert alerter.entry_hit(call, 99.95, 0.001) is True   # inside the tolerance band
    assert alerter.entry_hit(call, 99.0, 0.001) is False   # still below entry


def test_entry_hit_bearish_fires_at_or_below_entry():
    put = _rec("AAPL", "put", 100.0, 90.0, 105.0)
    assert alerter.entry_hit(put, 100.0, 0.001) is True
    assert alerter.entry_hit(put, 95.0, 0.001) is True     # blew through, downward
    assert alerter.entry_hit(put, 100.05, 0.001) is True   # inside the tolerance band
    assert alerter.entry_hit(put, 101.0, 0.001) is False   # still above entry


def test_entry_hit_long_behaves_bullish():
    long = _rec("WINR", "long", 6.0, 7.2, 5.4, source="smallcap")
    assert alerter.entry_hit(long, 6.0, 0.001) is True
    assert alerter.entry_hit(long, 5.5, 0.001) is False


# --- run_cycle: detection + one-time semantics ------------------------------


def test_cycle_fires_a_hit_and_marks_it_fired():
    store.save_plays([_rec("AAPL", "call", 100.0, 110.0, 95.0)], day=DAY, bucket=None)
    client = FakePriceClient({"AAPL": 101.0})

    result = alerter.run_cycle(dry_run=False, client=client, config=CONFIG, now=IN_HOURS,
                               bucket=None, smtp_factory=_boom_smtp, print_fn=lambda *_: None)

    assert result.skipped is False
    assert [h.play.symbol for h in result.hits] == ["AAPL"]
    # marked fired in the store
    assert store.load_day(DAY, bucket=None).fired == {"options:AAPL:call:2026-08-31"}


def test_cycle_does_not_fire_a_play_below_entry():
    store.save_plays([_rec("AAPL", "call", 100.0, 110.0, 95.0)], day=DAY, bucket=None)
    client = FakePriceClient({"AAPL": 98.0})

    result = alerter.run_cycle(dry_run=False, client=client, config=CONFIG, now=IN_HOURS,
                               bucket=None, smtp_factory=_boom_smtp, print_fn=lambda *_: None)

    assert result.hits == []
    assert result.sent is False
    assert "nothing new" in result.detail
    assert store.load_day(DAY, bucket=None).fired == set()


def test_second_cycle_is_idempotent_and_sends_nothing():
    store.save_plays([_rec("AAPL", "call", 100.0, 110.0, 95.0)], day=DAY, bucket=None)
    client = FakePriceClient({"AAPL": 101.0})

    first = alerter.run_cycle(dry_run=False, client=client, config=CONFIG, now=IN_HOURS,
                              bucket=None, smtp_factory=_boom_smtp, print_fn=lambda *_: None)
    assert len(first.hits) == 1

    # Same cycle again: the play is already fired -> no hit, no email.
    second = alerter.run_cycle(dry_run=False, client=client, config=CONFIG, now=IN_HOURS,
                               bucket=None, smtp_factory=_boom_smtp, print_fn=lambda *_: None)
    assert second.hits == []
    assert second.sent is False


def test_cycle_sends_one_email_summarizing_all_new_hits(monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-pw-fixture")
    monkeypatch.setenv("GMAIL_USER", "sender@example.com")
    monkeypatch.setenv("ENTRY_ALERTS_TO", "dest@example.com")
    store.save_plays(
        [_rec("AAPL", "call", 100.0, 110.0, 95.0), _rec("TSLA", "put", 200.0, 180.0, 210.0)],
        day=DAY, bucket=None,
    )
    client = FakePriceClient({"AAPL": 101.0, "TSLA": 199.0})

    sends = []

    class FakeSSL:
        def login(self, *a): pass
        def sendmail(self, frm, to, msg): sends.append((frm, tuple(to), msg))
        def quit(self): pass

    result = alerter.run_cycle(dry_run=False, client=client,
                               config={"email": {"smtp_port": 465}, "poll": {"tolerance": 0.001}},
                               now=IN_HOURS, bucket=None,
                               smtp_factory=lambda: FakeSSL(), print_fn=lambda *_: None)

    assert result.sent is True
    assert {h.play.symbol for h in result.hits} == {"AAPL", "TSLA"}
    assert len(sends) == 1  # ONE email, both hits in it
    body = sends[0][2]
    assert "AAPL" in body and "TSLA" in body


# --- market-hours guard ------------------------------------------------------


def test_cycle_skips_outside_market_hours_and_sends_nothing():
    store.save_plays([_rec("AAPL", "call", 100.0, 110.0, 95.0)], day=DAY, bucket=None)
    client = FakePriceClient({"AAPL": 101.0})

    result = alerter.run_cycle(dry_run=False, client=client, config=CONFIG, now=PRE_OPEN,
                               bucket=None, smtp_factory=_boom_smtp, print_fn=lambda *_: None)

    assert result.skipped is True
    assert "outside regular trading hours" in result.reason
    assert result.email is None
    assert result.sent is False
    # nothing was fired
    assert store.load_day(DAY, bucket=None).fired == set()


def test_force_bypasses_the_market_hours_guard():
    store.save_plays([_rec("AAPL", "call", 100.0, 110.0, 95.0)], day=DAY, bucket=None)
    client = FakePriceClient({"AAPL": 101.0})

    result = alerter.run_cycle(dry_run=True, client=client, config=CONFIG, now=PRE_OPEN,
                               bucket=None, force=True, smtp_factory=_boom_smtp,
                               print_fn=lambda *_: None)
    assert result.skipped is False
    assert len(result.hits) == 1


# --- dry-run is side-effect free --------------------------------------------


def test_dry_run_opens_no_smtp_and_persists_no_fired_state():
    store.save_plays([_rec("AAPL", "call", 100.0, 110.0, 95.0)], day=DAY, bucket=None)
    client = FakePriceClient({"AAPL": 101.0})
    printed = []

    result = alerter.run_cycle(dry_run=True, client=client, config=CONFIG, now=IN_HOURS,
                               bucket=None, smtp_factory=_boom_smtp, print_fn=printed.append)

    assert result.dry_run is True
    assert result.sent is False
    assert len(result.hits) == 1
    assert any("DRY RUN" in line for line in printed)
    # A dry-run must NOT consume the play's one alert.
    assert store.load_day(DAY, bucket=None).fired == set()
