"""Sector Scout email: dry-run never opens SMTP, the DOCX attaches as
multipart/mixed, and the recipient is the operator address only."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from src.sector_scout.email import build_message_with_docx, send_or_preview

CONFIG = {"email": {"from_addr": "digitalsolomon.com@gmail.com",
                    "to_addr": "digitalsolomon.com@gmail.com",
                    "smtp_host": "smtp.gmail.com", "smtp_port": 465,
                    "attach_docx": True}}


def _boom() -> None:
    raise AssertionError("SMTP factory must never be constructed in dry-run")


def test_dry_run_never_opens_smtp(tmp_path: Path) -> None:
    out = tmp_path / "scout.html"
    result = send_or_preview(
        subject="Sector Scout - test",
        html_body="<html><body>report</body></html>",
        docx_bytes=b"PK-fake",
        config=CONFIG,
        today=date(2026, 9, 4),
        dry_run=True,
        out_path=str(out),
        smtp_factory=_boom,
        print_fn=lambda *_: None,
    )
    assert result.dry_run and not result.sent
    assert out.read_text(encoding="utf-8").startswith("<html>")
    assert result.docx_attached


def test_no_password_means_not_sent(monkeypatch) -> None:
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.setenv("DS_VAULT_NO_GCLOUD", "1")  # no Secret Manager path either
    result = send_or_preview(
        subject="s", html_body="<html></html>", docx_bytes=None,
        config=CONFIG, today=date(2026, 9, 4), dry_run=False,
        smtp_factory=_boom, print_fn=None,
    )
    assert not result.sent
    assert "no Gmail app password" in result.detail


class FakeSMTP:
    def __init__(self) -> None:
        self.events: list = []

    def starttls(self) -> None:
        self.events.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.events.append(("login", user))

    def sendmail(self, from_addr: str, recipients: list[str], body: str) -> None:
        self.events.append(("sendmail", from_addr, tuple(recipients)))
        self.body = body

    def quit(self) -> None:
        self.events.append("quit")


def test_send_goes_to_operator_only(monkeypatch) -> None:
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.delenv("SECTOR_SCOUT_TO", raising=False)
    server = FakeSMTP()
    result = send_or_preview(
        subject="Sector Scout - test",
        html_body="<html><body>x</body></html>",
        docx_bytes=b"PK-fake-docx",
        config=CONFIG, today=date(2026, 9, 4), dry_run=False,
        smtp_factory=lambda: server, print_fn=None,
    )
    assert result.sent
    send_events = [e for e in server.events if isinstance(e, tuple) and e[0] == "sendmail"]
    assert send_events == [("sendmail", "digitalsolomon.com@gmail.com",
                            ("digitalsolomon.com@gmail.com",))]
    # Port 465 = implicit SSL: no STARTTLS call.
    assert "starttls" not in server.events
    # The app password never appears in the message body.
    assert "abcdefghijklmnop" not in server.body


def test_docx_attaches_as_mixed() -> None:
    msg = build_message_with_docx(
        "s", "a@x.com", "b@x.com", "<html></html>", "plain", b"PK-fake", "r.docx",
    )
    assert msg.get_content_type() == "multipart/mixed"
    parts = msg.get_payload()
    assert parts[0].get_content_type() == "multipart/related"
    assert parts[1].get_filename() == "r.docx"


def test_no_docx_stays_related() -> None:
    msg = build_message_with_docx("s", "a@x.com", "b@x.com", "<html></html>", "p", None, "r.docx")
    assert msg.get_content_type() == "multipart/related"
