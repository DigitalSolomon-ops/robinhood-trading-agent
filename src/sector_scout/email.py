"""Compose and (optionally) send the Sector Scout email: HTML body + DOCX
attachment, both rendered from the one computed run table.

ANALYSIS ONLY. No order path. The Gmail app password is read env-first
(GMAIL_APP_PASSWORD) then Secret Manager and is NEVER logged or written to
disk. --dry-run composes and returns/prints the email and never opens an
SMTP socket.

Recipient: the operator address only (config/env). This lane deliberately
does not read the report-recipients list.
"""

from __future__ import annotations

import smtplib
from dataclasses import dataclass
from datetime import date
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from typing import Any

from .. import scout_email_branding as branding
from .config import resolve_from_addr, resolve_gmail_app_password, resolve_to_addr


@dataclass(frozen=True)
class EmailResult:
    subject: str
    html: str
    sent: bool
    dry_run: bool
    to_addr: str
    detail: str
    docx_attached: bool = False


def build_subject(table: dict[str, Any], today: date) -> str:
    if table.get("send_mode") == "board_only":
        return f"Sector Scout - {today.isoformat()} - board only (no structure priced)"
    tradeable = [
        p for p in table.get("plays") or []
        if p.get("classification") != "Falling knife"
        and p.get("passes_floor") is not False
    ]
    live = sum(
        1 for p in tradeable
        if p.get("ticket") and p.get("pricing_basis") == "live"
    )
    prior = sum(
        1 for p in tradeable
        if p.get("ticket") and p.get("pricing_basis") != "live"
    )
    top = min(len(tradeable), 9)
    if live:
        quotes = f", {live} live tickets" + (f" + {prior} prior-session" if prior else "")
    elif prior:
        quotes = f", {prior} tickets at prior-session pricing"
    else:
        quotes = " (no tickets priced)"
    return f"Sector Scout - {today.isoformat()} - Top {top} opportunities{quotes}"


def build_message_with_docx(
    subject: str,
    from_addr: str,
    to_header: str,
    html_body: str,
    plain_fallback: str,
    docx_bytes: bytes | None,
    docx_name: str,
) -> MIMEMultipart:
    """multipart/mixed( multipart/related( alternative + logo ), docx ).
    Reuses the branding builder for the related part so the logo pattern
    stays in one place; the attachment wraps around it."""
    related = branding.build_message(subject, from_addr, to_header, html_body, plain_fallback)
    if docx_bytes is None:
        return related
    mixed = MIMEMultipart("mixed")
    for key in ("Subject", "From", "To"):
        mixed[key] = related[key]
        del related[key]
    mixed.attach(related)
    attachment = MIMEApplication(
        docx_bytes,
        _subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    attachment.add_header("Content-Disposition", "attachment", filename=docx_name)
    mixed.attach(attachment)
    return mixed


def send_or_preview(
    *,
    subject: str,
    html_body: str,
    docx_bytes: bytes | None,
    config: dict[str, Any],
    today: date,
    dry_run: bool,
    out_path: str | None = None,
    smtp_factory: Any = None,
    print_fn: Any = print,
) -> EmailResult:
    """Send over Gmail SMTP (implicit SSL on 465) or, in dry-run, write/print
    WITHOUT opening any SMTP connection. `smtp_factory` is tests-only."""
    to_addr = resolve_to_addr(config)
    from_addr = resolve_from_addr(config)

    if out_path:
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(html_body)

    if dry_run:
        if print_fn is not None:
            print_fn(f"[DRY RUN] Subject: {subject}")
            print_fn(f"[DRY RUN] To: {to_addr}  From: {from_addr}")
            docx_note = f"{len(docx_bytes):,} bytes" if docx_bytes else "not generated"
            print_fn(f"[DRY RUN] DOCX attachment: {docx_note}. Not sending; no SMTP connection opened.")
        return EmailResult(
            subject, html_body, sent=False, dry_run=True, to_addr=to_addr,
            detail="dry-run: composed, not sent", docx_attached=docx_bytes is not None,
        )

    password = resolve_gmail_app_password()
    if not password:
        return EmailResult(
            subject, html_body, sent=False, dry_run=False, to_addr=to_addr,
            detail="no Gmail app password (env GMAIL_APP_PASSWORD or Secret Manager); not sent",
            docx_attached=False,
        )

    email_cfg = config.get("email", {}) or {}
    host = email_cfg.get("smtp_host", "smtp.gmail.com")
    port = int(email_cfg.get("smtp_port", 465))
    docx_name = f"sector-scout-{today.isoformat()}.docx"
    message = build_message_with_docx(
        subject, from_addr, to_addr, html_body,
        "This is an HTML email; enable HTML to read the Sector Scout report.",
        docx_bytes if (email_cfg.get("attach_docx", True)) else None,
        docx_name,
    )

    use_ssl = port == 465
    if smtp_factory is not None:
        server = smtp_factory()
    elif use_ssl:
        server = smtplib.SMTP_SSL(host, port, timeout=60)
    else:
        server = smtplib.SMTP(host, port, timeout=60)
    try:
        if not use_ssl:
            server.starttls()
        server.login(from_addr, password)
        server.sendmail(from_addr, [to_addr], message.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass
    return EmailResult(
        subject, html_body, sent=True, dry_run=False, to_addr=to_addr,
        detail=f"sent to {to_addr}", docx_attached=docx_bytes is not None,
    )
