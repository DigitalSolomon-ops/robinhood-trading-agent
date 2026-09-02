"""The scouts widen their send to the operator default PLUS the report list.

ANALYSIS ONLY. Confirms the additive, deduped recipient set reaches SMTP without
touching any order path. Uses a fake SMTP client, never a real socket.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.options_scout.analyzer import Play
from src.options_scout.backtest import HitRate
from src.options_scout.email import send_or_preview as options_send
from src.options_scout.indicators import Factor

TODAY = date(2026, 9, 1)


def make_play(**overrides) -> Play:
    hit = HitRate(direction="call", horizon_days=10, occurrences=42, hits=27,
                  low_confidence=False, min_occurrences=10)
    defaults = dict(
        symbol="AAPL", direction="call", reference_close=205.11, entry=205.11,
        ceiling=214.30, floor=199.60, expected_move_pct=0.045, horizon_days=10,
        strike=210.0, expiry_date="2026-09-18", contract_ticker="O:AAPL260918C00210000",
        conviction=72.4, factors=(Factor("trend", 0.8, 0.4, 0.32),),
        news_score=0.5, news_citation="n/a", regime_ratio=0.6, regime_note="n/a",
        hit_rate=hit, rank_score=0.42, rationale="test play",
    )
    defaults.update(overrides)
    return Play(**defaults)


class FakeSMTP:
    def __init__(self):
        self.recipients = None

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def sendmail(self, from_addr, to_addrs, msg):
        self.recipients = list(to_addrs)

    def quit(self):
        pass


def _send_with(extra, monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-pw-fixture")
    monkeypatch.setenv("GMAIL_USER", "sender@example.com")
    monkeypatch.setenv("OPTIONS_SCOUT_TO", "default@example.com")
    fake = FakeSMTP()
    result = options_send(
        [make_play()], {"email": {}}, dry_run=False, today=TODAY,
        smtp_factory=lambda: fake, print_fn=lambda *_: None,
        extra_recipients=extra,
    )
    assert result.sent is True
    return fake.recipients


def test_default_only_when_no_extra_recipients(monkeypatch):
    assert _send_with(None, monkeypatch) == ["default@example.com"]


def test_extra_recipients_are_added_after_the_default(monkeypatch):
    recips = _send_with(["a@x.com", "b@x.com"], monkeypatch)
    assert recips == ["default@example.com", "a@x.com", "b@x.com"]


def test_default_is_deduped_against_the_extra_list(monkeypatch):
    recips = _send_with(["Default@Example.com", "c@x.com"], monkeypatch)
    assert recips == ["default@example.com", "c@x.com"]


def test_dry_run_lists_all_recipients_without_sending(monkeypatch):
    printed = []
    options_send(
        [make_play()], {"email": {"to_addr": "default@example.com"}}, dry_run=True,
        today=TODAY, print_fn=printed.append, extra_recipients=["x@y.com"],
    )
    joined = "\n".join(printed)
    assert "default@example.com" in joined and "x@y.com" in joined
