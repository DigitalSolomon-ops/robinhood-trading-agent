"""Compose and (optionally) send the ENTRY-HIT alert email.

NOTIFICATION ONLY. This renders a compact, mobile-friendly alert listing the
plays whose ENTRY level was reached this cycle and sends it over Gmail SMTP. It
has no order path. The Gmail app password is read env-first (GMAIL_APP_PASSWORD)
then Secret Manager (`gmail-app-password`) and is NEVER logged or written to
disk.

--dry-run composes and returns/prints the email and never opens an SMTP socket.
"""

from __future__ import annotations

import html
import smtplib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from .config import resolve_from_addr, resolve_gmail_app_password, resolve_to_addr
from .store import PlayRecord

DISCLAIMER = (
    "NOT FINANCIAL ADVICE. This is an automated price alert from a software "
    "screener using delayed public market data -- it only tells you a level was "
    "reached. It is not a recommendation, solicitation, or personalized advice, "
    "and no one reviewed it. Every level is on the UNDERLYING, not the option, "
    "and the shown price is delayed. You alone are responsible for your own "
    "trading decisions -- do your own research and consider a licensed advisor."
)


@dataclass(frozen=True)
class Hit:
    """One play whose entry level was reached, with the price that triggered it."""

    play: PlayRecord
    current_price: float


@dataclass(frozen=True)
class EmailResult:
    subject: str
    html: str
    sent: bool
    dry_run: bool
    to_addr: str
    detail: str


def _e(text: Any) -> str:
    return html.escape(str(text))


def _contract_label(play: PlayRecord) -> str:
    d = play.direction.lower()
    if d in ("call", "put"):
        return f"{play.symbol} {d.upper()} (options)"
    if d == "long":
        return f"{play.symbol} LONG (shares)"
    return f"{play.symbol} {d.upper()}"


def build_subject(hits: list[Hit], today: date) -> str:
    if not hits:
        return f"Entry Alerts — {today.isoformat()} — nothing new"
    if len(hits) == 1:
        return f"ENTRY HIT — {hits[0].play.symbol} @ {hits[0].play.entry:g} — {today.isoformat()}"
    symbols = ", ".join(h.play.symbol for h in hits)
    return f"ENTRY HIT — {len(hits)} plays ({symbols}) — {today.isoformat()}"


def _hit_block(hit: Hit) -> str:
    play = hit.play
    badge_color = "#0b7a3b" if play.bullish else "#b0203a"
    return f"""
    <tr><td style="padding:0 0 14px 0;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="border:1px solid #e2e5ea;border-radius:8px;">
        <tr><td style="padding:12px 14px;">
          <div style="font-size:15px;color:#111;">
            <span style="display:inline-block;background:{badge_color};color:#fff;
                   font-weight:700;font-size:11px;padding:2px 7px;border-radius:4px;">
              ENTRY HIT</span>
            &nbsp; <strong style="font-size:16px;">{_e(_contract_label(play))}</strong>
          </div>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                 style="margin-top:10px;font-size:13px;">
            <tr>
              <td style="padding:4px 6px;background:#eef7f0;border-radius:4px;">
                ENTRY<br><strong>{play.entry:g}</strong></td>
              <td style="width:6px;"></td>
              <td style="padding:4px 6px;background:#eef2fb;border-radius:4px;">
                NOW<br><strong>{hit.current_price:g}</strong></td>
              <td style="width:6px;"></td>
              <td style="padding:4px 6px;background:#f5f7fa;border-radius:4px;">
                TARGET<br><strong>{play.target:g}</strong></td>
              <td style="width:6px;"></td>
              <td style="padding:4px 6px;background:#fdf2f4;border-radius:4px;">
                STOP<br><strong>{play.stop:g}</strong></td>
            </tr>
          </table>
          <div style="margin-top:8px;font-size:12px;color:#8a9099;">
            source: {_e(play.source)} &middot; delayed price on the underlying
          </div>
        </td></tr>
      </table>
    </td></tr>"""


def render_email_html(hits: list[Hit], today: date) -> str:
    if hits:
        rows = "".join(_hit_block(h) for h in hits)
    else:
        rows = (
            '<tr><td style="padding:14px;font-size:14px;color:#444;">'
            "No new entry levels were reached this cycle."
            "</td></tr>"
        )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#eef1f5;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef1f5;">
<tr><td align="center" style="padding:16px 10px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="max-width:520px;background:#ffffff;border-radius:10px;overflow:hidden;
                font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
    <tr><td style="background:#12203a;padding:16px 14px;">
      <div style="color:#fff;font-size:17px;font-weight:700;">Entry-Hit Alert</div>
      <div style="color:#9fb0cc;font-size:12px;margin-top:2px;">
        {_e(today.isoformat())} &middot; {len(hits)} new hit{'s' if len(hits) != 1 else ''} &middot; notification only
      </div>
    </td></tr>
    <tr><td style="padding:14px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        {rows}
      </table>
    </td></tr>
    <tr><td style="padding:0 14px 16px 14px;">
      <div style="border:1px solid #b0203a;background:#fdf2f4;border-radius:8px;
                  padding:10px 12px;color:#5a1120;font-size:11px;line-height:1.6;">
        <strong style="color:#b0203a;">NOT FINANCIAL ADVICE.</strong> {_e(DISCLAIMER)}
      </div>
    </td></tr>
    <tr><td style="padding:0 14px 16px 14px;color:#9098a4;font-size:10px;line-height:1.5;">
      Generated by the Entry-Hit Alerter from delayed public data
      (Massive / Polygon.io). Levels are on the underlying. This tool never
      places, reviews, or cancels any order.
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""


def compose_email(hits: list[Hit], today: date | None = None) -> tuple[str, str]:
    today = today or datetime.now(UTC).date()
    return build_subject(hits, today), render_email_html(hits, today)


def send_or_preview(
    hits: list[Hit],
    config: dict[str, Any],
    *,
    dry_run: bool,
    out_path: str | None = None,
    today: date | None = None,
    smtp_factory: Any = None,
    print_fn: Any = print,
) -> EmailResult:
    """Compose the alert, then either send it (Gmail SMTP + SSL/STARTTLS) or, in
    dry-run, print/write it WITHOUT opening any SMTP connection.

    `smtp_factory` is only for tests -- it must never be exercised in dry-run.
    """
    today = today or datetime.now(UTC).date()
    subject, body = compose_email(hits, today)
    to_addr = resolve_to_addr(config)
    from_addr = resolve_from_addr(config)

    if out_path:
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(body)

    if dry_run:
        if print_fn is not None:
            print_fn(f"[DRY RUN] Subject: {subject}")
            print_fn(f"[DRY RUN] To: {to_addr}  From: {from_addr}")
            print_fn(f"[DRY RUN] {len(hits)} hit(s). Not sending; no SMTP connection opened.")
            print_fn(body)
        return EmailResult(subject, body, sent=False, dry_run=True, to_addr=to_addr,
                           detail="dry-run: composed, not sent")

    password = resolve_gmail_app_password()
    if not password:
        return EmailResult(subject, body, sent=False, dry_run=False, to_addr=to_addr,
                           detail="no Gmail app password (env GMAIL_APP_PASSWORD or Secret Manager GMAIL_VAULT_NAME); not sent")

    email_cfg = config.get("email", {}) or {}
    host = email_cfg.get("smtp_host", "smtp.gmail.com")
    port = int(email_cfg.get("smtp_port", 465))

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = from_addr
    message["To"] = to_addr
    message.attach(MIMEText("This is an HTML email; enable HTML to read the entry-hit alert.", "plain", "utf-8"))
    message.attach(MIMEText(body, "html", "utf-8"))

    # Port 465 uses implicit SSL (SMTP_SSL, no STARTTLS); 587 uses STARTTLS.
    # Many networks block 587 while leaving 465 open, so 465 is the safer default.
    use_ssl = port == 465
    if smtp_factory is not None:
        server = smtp_factory()
    elif use_ssl:
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
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
    return EmailResult(subject, body, sent=True, dry_run=False, to_addr=to_addr,
                       detail=f"sent to {to_addr}")
