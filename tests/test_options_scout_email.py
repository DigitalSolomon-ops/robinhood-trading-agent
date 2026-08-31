from __future__ import annotations

from datetime import date

import pytest

from src.options_scout.analyzer import Play
from src.options_scout.backtest import HitRate
from src.options_scout.email import DISCLAIMER, render_email_html, send_or_preview
from src.options_scout.indicators import Factor

TODAY = date(2026, 8, 30)


def make_play(**overrides) -> Play:
    hit = HitRate(direction="call", horizon_days=10, occurrences=42, hits=27,
                  low_confidence=False, min_occurrences=10)
    defaults = dict(
        symbol="AAPL",
        direction="call",
        reference_close=205.11,
        entry=205.11,
        ceiling=214.30,
        floor=199.60,
        expected_move_pct=0.045,
        horizon_days=10,
        strike=210.0,
        expiry_date="2026-09-18",
        contract_ticker="O:AAPL260918C00210000",
        conviction=72.4,
        factors=(Factor("trend", 0.8, 0.4, 0.32), Factor("momentum", 0.5, 0.3, 0.15),
                 Factor("rsi", 0.6, 0.3, 0.18)),
        news_score=0.5,
        news_citation="massive_news 1/2 rated positive",
        regime_ratio=0.6,
        regime_note="breadth 60% advancing",
        hit_rate=hit,
        rank_score=0.42,
        rationale="CALL on AAPL. Trend: EMA20 above EMA50. Momentum: MACD positive. "
                  "RSI(14) 62 (above midline). Realized-vol expected move ~4.5% over 10 "
                  "trading days. Backtest: 64% hit rate over 42 past occurrences.",
        premium=5.25,
        premium_source="last_quote_midpoint",
        open_interest=4213.0,
        day_volume=1875.0,
        delta=0.42,
        theta=-0.08,
        implied_volatility=0.28,
        contract_selection="target_delta",
    )
    defaults.update(overrides)
    return Play(**defaults)


def test_html_contains_every_play_field():
    play = make_play()
    html = render_email_html([play], TODAY)
    for token in [
        "AAPL",
        "CALL",
        "210",                       # strike
        "2026-09-18",                # expiry
        "O:AAPL260918C00210000",     # contract ticker
        "205.11",                    # entry / reference
        "214.3",                     # ceiling
        "199.6",                     # floor
        "ENTRY",
        "CEILING",
        "FLOOR",
        "past occurrences",          # hit rate w/ sample size
        "Conviction",
        "Trend:",                    # rationale signals
    ]:
        assert token in html, f"missing {token!r}"


def test_html_renders_real_contract_economics():
    """Premium, cost per contract, max loss, breakeven, OI + day volume, and
    greeks all render from the snapshot data."""
    html = render_email_html([make_play()], TODAY)
    assert "$5.25" in html               # premium per share
    assert "$525.00" in html             # cost per contract = premium x 100
    assert "MAX LOSS" in html
    assert "215.25" in html              # breakeven = strike 210 + premium 5.25
    assert "BREAKEVEN" in html
    assert "OPEN INTEREST" in html
    assert "4,213" in html               # open interest
    assert "1,875" in html               # day volume
    assert "Greeks:" in html
    assert "+0.420" in html              # delta
    assert "-0.080" in html              # theta
    assert "28.0%" in html               # implied volatility


def test_html_shows_pending_note_when_greeks_absent():
    """Weekend/after-hours: greeks + IV are None, so a clear pending note shows
    instead of blanks -- but premium and open interest still render."""
    play = make_play(delta=None, theta=None, implied_volatility=None,
                     contract_selection="strike_distance")
    html = render_email_html([play], TODAY)
    assert "pending" in html.lower()
    assert "market hours" in html.lower()
    assert "Greeks:" not in html         # no greek values are shown
    assert "$525.00" in html             # premium-derived economics still render
    assert "4,213" in html               # open interest still shows


def test_html_shows_premium_pending_when_premium_is_none():
    """If neither a live quote nor a day close populated (rare off-hours), the
    premium reads 'pending' and the derived costs degrade to n/a, no crash."""
    play = make_play(premium=None, premium_source=None,
                     delta=None, theta=None, implied_volatility=None)
    html = render_email_html([play], TODAY)
    assert "pending" in html.lower()
    assert "PREMIUM" in html


def test_html_contains_the_prominent_disclaimer():
    html = render_email_html([make_play()], TODAY)
    assert "NOT FINANCIAL ADVICE" in html
    assert "DISCLAIMER" in html
    assert "TOTAL LOSS" in html
    assert "past results" in DISCLAIMER.lower() or "past results" in html.lower()
    assert DISCLAIMER in html


def test_low_confidence_is_surfaced_in_the_html():
    hit = HitRate("call", 10, 4, 3, low_confidence=True, min_occurrences=10)
    html = render_email_html([make_play(hit_rate=hit)], TODAY)
    assert "LOW CONFIDENCE" in html


def test_empty_plays_still_renders_with_disclaimer():
    html = render_email_html([], TODAY)
    assert "No candidate setups" in html
    assert "NOT FINANCIAL ADVICE" in html


def test_dry_run_never_opens_an_smtp_connection():
    printed = []

    def boom():
        raise AssertionError("dry-run must not construct an SMTP client")

    result = send_or_preview(
        [make_play()], {"email": {}}, dry_run=True, today=TODAY,
        smtp_factory=boom, print_fn=printed.append,
    )
    assert result.sent is False
    assert result.dry_run is True
    assert any("DRY RUN" in line for line in printed)


def test_dry_run_can_write_html_to_a_file(tmp_path):
    out = tmp_path / "scout.html"
    send_or_preview([make_play()], {"email": {}}, dry_run=True, out_path=str(out),
                    today=TODAY, print_fn=lambda *_: None)
    text = out.read_text(encoding="utf-8")
    assert "NOT FINANCIAL ADVICE" in text
    assert "AAPL" in text


def test_send_uses_starttls_login_and_sendmail(monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-pw-fixture")  # placeholder, not a real secret
    monkeypatch.setenv("GMAIL_USER", "sender@example.com")
    monkeypatch.setenv("OPTIONS_SCOUT_TO", "dest@example.com")

    events = []

    class FakeSMTP:
        def starttls(self):
            events.append("starttls")

        def login(self, user, password):
            events.append(("login", user, password))

        def sendmail(self, from_addr, to_addrs, msg):
            events.append(("sendmail", from_addr, tuple(to_addrs)))
            assert "app-pw-fixture" not in msg  # the password never rides in the body

        def quit(self):
            events.append("quit")

    result = send_or_preview(
        [make_play()], {"email": {}}, dry_run=False, today=TODAY,
        smtp_factory=lambda: FakeSMTP(), print_fn=lambda *_: None,
    )
    assert result.sent is True
    assert "starttls" in events
    assert ("login", "sender@example.com", "app-pw-fixture") in events
    assert ("sendmail", "sender@example.com", ("dest@example.com",)) in events


def test_send_without_password_does_not_send(monkeypatch):
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.setenv("DS_VAULT_NO_GCLOUD", "1")  # block the Secret Manager fallback

    def boom():
        raise AssertionError("must not open SMTP without a password")

    result = send_or_preview([make_play()], {"email": {}}, dry_run=False, today=TODAY,
                             smtp_factory=boom, print_fn=lambda *_: None)
    assert result.sent is False
    assert "no Gmail app password" in result.detail
