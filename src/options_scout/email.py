"""Compose and (optionally) send the Options Scout email.

ANALYSIS ONLY. This module renders a ranked list of CANDIDATE plays and sends it
over Gmail SMTP. It has no order path. The Gmail app password is read env-first
(GMAIL_APP_PASSWORD) then Secret Manager (`gmail-app-password`) and is NEVER
logged or written to disk.

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

from .analyzer import Play
from .config import resolve_from_addr, resolve_gmail_app_password, resolve_to_addr

DISCLAIMER = (
    "NOT FINANCIAL ADVICE. This email is generated automatically by a software "
    "screener from delayed public market data. It is not a recommendation, "
    "solicitation, or personalized investment advice, and no one has reviewed it. "
    "Options are complex and carry a substantial risk of the TOTAL LOSS of the "
    "amount paid. The backtested hit-rates are historical base rates over small "
    "samples; past results DO NOT predict or guarantee future performance. Every "
    "price level is on the underlying, not the option. You alone are responsible "
    "for your own trading decisions -- do your own research and consider a "
    "licensed advisor."
)


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


def build_subject(plays: list[Play], today: date) -> str:
    return f"Options Scout — {today.isoformat()} — {len(plays)} ranked plays"


def _badge(direction: str) -> str:
    color = "#0b7a3b" if direction == "call" else "#b0203a"
    return (
        f'<span style="display:inline-block;background:{color};color:#fff;'
        f'font-weight:700;font-size:12px;letter-spacing:.5px;padding:3px 8px;'
        f'border-radius:4px;">{_e(direction.upper())}</span>'
    )


def _play_block(rank: int, play: Play) -> str:
    strike = f"{play.strike:g}" if play.strike is not None else "n/a"
    expiry = play.expiry_date or "n/a"
    ticker = play.contract_ticker or "(no listed contract matched)"
    target_label = "CEILING (target)" if play.direction == "call" else "CEILING (stop)"
    floor_label = "FLOOR (stop)" if play.direction == "call" else "FLOOR (target)"
    lc = (
        '<span style="color:#b0203a;font-weight:700;"> LOW CONFIDENCE</span>'
        if play.hit_rate.low_confidence
        else ""
    )
    return f"""
    <tr><td style="padding:0 0 18px 0;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="border:1px solid #e2e5ea;border-radius:8px;">
        <tr><td style="padding:14px 16px;">
          <div style="font-size:15px;color:#111;">
            <span style="color:#8a9099;">#{rank}</span>&nbsp; {_badge(play.direction)}
            &nbsp; <strong style="font-size:17px;">{_e(play.symbol)}</strong>
            <span style="color:#8a9099;">&nbsp;ref {play.reference_close:g}</span>
          </div>
          <div style="margin-top:6px;font-size:13px;color:#333;">
            Contract: <strong>{_e(strike)} {_e(play.direction)}</strong> exp {_e(expiry)}
            &nbsp;<span style="color:#8a9099;font-family:monospace;">{_e(ticker)}</span>
          </div>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                 style="margin-top:10px;font-size:13px;">
            <tr>
              <td style="padding:4px 6px;background:#f5f7fa;border-radius:4px;">
                ENTRY<br><strong>{play.entry:g}</strong></td>
              <td style="width:8px;"></td>
              <td style="padding:4px 6px;background:#f5f7fa;border-radius:4px;">
                {target_label}<br><strong>{play.ceiling:g}</strong></td>
              <td style="width:8px;"></td>
              <td style="padding:4px 6px;background:#f5f7fa;border-radius:4px;">
                {floor_label}<br><strong>{play.floor:g}</strong></td>
            </tr>
          </table>
          <div style="margin-top:10px;font-size:13px;color:#333;">
            <strong>Hit rate:</strong> {play.hit_rate.hit_rate * 100:.0f}%
            over {play.hit_rate.occurrences} past occurrences{lc}
            &nbsp;|&nbsp; <strong>Conviction:</strong> {play.conviction:g}/100
            &nbsp;|&nbsp; <strong>Horizon:</strong> {play.horizon_days}d
          </div>
          <div style="margin-top:8px;font-size:13px;color:#444;line-height:1.5;">
            {_e(play.rationale)}
          </div>
        </td></tr>
      </table>
    </td></tr>"""


def render_email_html(plays: list[Play], today: date) -> str:
    if plays:
        rows = "".join(_play_block(i + 1, p) for i, p in enumerate(plays))
    else:
        rows = (
            '<tr><td style="padding:16px;font-size:14px;color:#444;">'
            "No candidate setups cleared the screen today. No plays to report."
            "</td></tr>"
        )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#eef1f5;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef1f5;">
<tr><td align="center" style="padding:20px 12px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="max-width:620px;background:#ffffff;border-radius:10px;overflow:hidden;
                font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
    <tr><td style="background:#12203a;padding:18px 16px;">
      <div style="color:#fff;font-size:18px;font-weight:700;">Options Scout</div>
      <div style="color:#9fb0cc;font-size:13px;margin-top:2px;">
        {_e(today.isoformat())} &middot; {len(plays)} ranked candidate plays &middot; analysis only
      </div>
    </td></tr>
    <tr><td style="padding:16px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
        {rows}
      </table>
    </td></tr>
    <tr><td style="padding:0 16px 18px 16px;">
      <div style="border:2px solid #b0203a;background:#fdf2f4;border-radius:8px;
                  padding:14px 16px;">
        <div style="color:#b0203a;font-weight:800;font-size:13px;letter-spacing:.5px;">
          &#9888; DISCLAIMER &mdash; READ THIS</div>
        <div style="color:#5a1120;font-size:12px;line-height:1.6;margin-top:6px;">
          {_e(DISCLAIMER)}
        </div>
      </div>
    </td></tr>
    <tr><td style="padding:0 16px 20px 16px;color:#9098a4;font-size:11px;line-height:1.5;">
      Generated by the Options Scout screener from delayed public data
      (Massive / Polygon.io). Levels are on the underlying; the free data tier
      does not provide option quotes, greeks, or implied volatility. This tool
      never places, reviews, or cancels any order.
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""


def compose_email(
    plays: list[Play], config: dict[str, Any], today: date | None = None
) -> tuple[str, str]:
    today = today or datetime.now(UTC).date()
    return build_subject(plays, today), render_email_html(plays, today)


def send_or_preview(
    plays: list[Play],
    config: dict[str, Any],
    *,
    dry_run: bool,
    out_path: str | None = None,
    today: date | None = None,
    smtp_factory: Any = None,
    print_fn: Any = print,
) -> EmailResult:
    """Compose the email, then either send it (Gmail SMTP + STARTTLS) or, in
    dry-run, print/write it WITHOUT opening any SMTP connection.

    `smtp_factory` is only for tests -- it must never be exercised in dry-run.
    """
    today = today or datetime.now(UTC).date()
    subject, body = compose_email(plays, config, today)
    to_addr = resolve_to_addr(config)
    from_addr = resolve_from_addr(config)

    if out_path:
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(body)

    if dry_run:
        if print_fn is not None:
            print_fn(f"[DRY RUN] Subject: {subject}")
            print_fn(f"[DRY RUN] To: {to_addr}  From: {from_addr}")
            print_fn(f"[DRY RUN] {len(plays)} play(s). Not sending; no SMTP connection opened.")
            print_fn(body)
        return EmailResult(subject, body, sent=False, dry_run=True, to_addr=to_addr,
                           detail="dry-run: composed, not sent")

    password = resolve_gmail_app_password()
    if not password:
        return EmailResult(subject, body, sent=False, dry_run=False, to_addr=to_addr,
                           detail="no Gmail app password (env GMAIL_APP_PASSWORD or Secret Manager GMAIL_VAULT_NAME); not sent")

    email_cfg = config.get("email", {}) or {}
    host = email_cfg.get("smtp_host", "smtp.gmail.com")
    port = int(email_cfg.get("smtp_port", 587))

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = from_addr
    message["To"] = to_addr
    message.attach(MIMEText("This is an HTML email; enable HTML to read the Options Scout report.", "plain", "utf-8"))
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
