"""The ENTRY-HIT alert email: fields, disclaimer, SSL/STARTTLS, and a dry-run
that opens no SMTP socket. All SMTP is mocked.
"""

from __future__ import annotations

from datetime import date

from src.entry_alerts.email import DISCLAIMER, Hit, render_email_html, send_or_preview
from src.entry_alerts.store import PlayRecord

TODAY = date(2026, 8, 31)


def _hit(symbol="AAPL", direction="call", entry=100.0, target=110.0, stop=95.0,
         price=101.0, source="options"):
    rec = PlayRecord(id=f"{source}:{symbol}:{direction}:2026-08-31", source=source,
                     symbol=symbol, direction=direction, entry=entry, target=target,
                     stop=stop, date="2026-08-31")
    return Hit(play=rec, current_price=price)


def test_html_contains_every_alert_field():
    html = render_email_html([_hit()], TODAY)
    for token in ["ENTRY HIT", "AAPL", "CALL", "ENTRY", "NOW", "TARGET", "STOP",
                  "100", "101", "110", "95"]:
        assert token in html, f"missing {token!r}"


def test_html_labels_a_long_share_play():
    html = render_email_html([_hit(symbol="WINR", direction="long", source="smallcap")], TODAY)
    assert "WINR" in html
    assert "LONG" in html
    assert "shares" in html.lower()


def test_html_carries_the_not_financial_advice_disclaimer():
    html = render_email_html([_hit()], TODAY)
    assert "NOT FINANCIAL ADVICE" in html
    assert DISCLAIMER in html


def test_empty_hits_still_renders_with_disclaimer():
    html = render_email_html([], TODAY)
    assert "No new entry levels" in html
    assert "NOT FINANCIAL ADVICE" in html


def test_dry_run_never_opens_an_smtp_connection():
    printed = []

    def boom():
        raise AssertionError("dry-run must not construct an SMTP client")

    result = send_or_preview([_hit()], {"email": {}}, dry_run=True, today=TODAY,
                             smtp_factory=boom, print_fn=printed.append)
    assert result.sent is False and result.dry_run is True
    assert any("DRY RUN" in line for line in printed)


def test_send_over_ssl_465_does_not_starttls(monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-pw-fixture")  # placeholder, not a real secret
    monkeypatch.setenv("GMAIL_USER", "sender@example.com")
    monkeypatch.setenv("ENTRY_ALERTS_TO", "dest@example.com")

    events = []

    class FakeSSLSMTP:
        def starttls(self):
            events.append("starttls")  # must NOT be called on the 465/SSL path

        def login(self, user, password):
            events.append(("login", user, password))

        def sendmail(self, from_addr, to_addrs, msg):
            events.append(("sendmail", from_addr, tuple(to_addrs)))
            assert "app-pw-fixture" not in msg  # the password never rides in the body

        def quit(self):
            events.append("quit")

    result = send_or_preview([_hit()], {"email": {"smtp_port": 465}}, dry_run=False,
                             today=TODAY, smtp_factory=lambda: FakeSSLSMTP(),
                             print_fn=lambda *_: None)
    assert result.sent is True
    assert "starttls" not in events
    assert ("login", "sender@example.com", "app-pw-fixture") in events
    assert ("sendmail", "sender@example.com", ("dest@example.com",)) in events


def test_send_without_password_does_not_send(monkeypatch):
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.setenv("DS_VAULT_NO_GCLOUD", "1")  # block the Secret Manager fallback

    def boom():
        raise AssertionError("must not open SMTP without a password")

    result = send_or_preview([_hit()], {"email": {}}, dry_run=False, today=TODAY,
                             smtp_factory=boom, print_fn=lambda *_: None)
    assert result.sent is False
    assert "no Gmail app password" in result.detail
