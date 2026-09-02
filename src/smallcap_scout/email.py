"""Compose and (optionally) send the Small-Cap Scout email.

ANALYSIS ONLY. This module renders a ranked shares WATCHLIST and sends it over
Gmail SMTP. It has no order path. The Gmail app password is read env-first
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

from .config import resolve_from_addr, resolve_gmail_app_password, resolve_to_addr
from .scanner import ScoutPick, _fmt_float

# The end-of-day honesty notice -- shown in the email header AND documented in
# docs/smallcap-scout.md. The free data tier has no real-time or pre-market feed.
EOD_NOTICE = (
    "Built from END-OF-DAY data (the last completed session). This is a MORNING "
    "WATCHLIST of the prior session momentum leaders with a catalyst -- NOT a "
    "live pre-market gapper scan, which needs paid real-time data."
)

DISCLAIMER = (
    "NOT FINANCIAL ADVICE. This email is generated automatically by a software "
    "screener from delayed, end-of-day public market data. It is not a "
    "recommendation, solicitation, or personalized investment advice, and no one "
    "has reviewed it. Small-cap momentum trading carries SUBSTANTIAL RISK: these "
    "are low-priced, thinly-capitalised, high-volatility names that can gap "
    "against you and lose value rapidly, and you can lose some or ALL of the "
    "money you put in. Float here is a shares-outstanding PROXY, not true free "
    "float. Every level is a reference on the underlying shares, not a live "
    "quote. You alone are responsible for your own trading decisions -- do your "
    "own research and consider a licensed advisor."
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


def build_subject(picks: list[ScoutPick], today: date) -> str:
    return f"Small-Cap Scout — {today.isoformat()} — {len(picks)} momentum names"


def _pillar_badges(pillars: tuple[str, ...]) -> str:
    chips = []
    for pillar in pillars:
        chips.append(
            f'<span style="display:inline-block;background:#0b3d6b;color:#dce8f5;'
            f'font-size:11px;font-weight:600;padding:2px 7px;border-radius:10px;'
            f'margin:2px 3px 0 0;">{_e(pillar)}</span>'
        )
    return "".join(chips)


def _pick_block(rank: int, pick: ScoutPick) -> str:
    levels = pick.levels
    if levels is not None:
        entry = f"{levels.entry:g}"
        target = f"{levels.target:g}"
        stop = f"{levels.stop:g}"
        rr = f" &nbsp;R:R {levels.reward_risk:.1f}" if levels.reward_risk else ""
    else:
        entry = target = stop = "n/a"
        rr = ""
    catalyst = pick.catalyst or "(no rated catalyst headline)"
    float_label = _fmt_float(pick.float_shares)
    float_note = "" if pick.float_known else " (unknown)"
    return f"""
    <tr><td style="padding:0 0 18px 0;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
             style="border:1px solid #e2e5ea;border-radius:8px;">
        <tr><td style="padding:14px 16px;">
          <div style="font-size:15px;color:#111;">
            <span style="color:#8a9099;">#{rank}</span>&nbsp;
            <strong style="font-size:18px;">{_e(pick.symbol)}</strong>
            <span style="color:#0b7a3b;font-weight:700;">&nbsp;+{pick.pct_change:g}%</span>
            <span style="color:#8a9099;">&nbsp;last {pick.last_price:g}</span>
          </div>
          <div style="margin-top:6px;font-size:13px;color:#333;">
            <strong>RVOL {pick.rvol:g}x</strong>
            &nbsp;|&nbsp; Float (shares o/s proxy): <strong>{_e(float_label)}</strong>{_e(float_note)}
            &nbsp;|&nbsp; Rank {pick.rank_score:g}
          </div>
          <div style="margin-top:8px;font-size:13px;color:#333;">
            Catalyst: <em>{_e(catalyst)}</em>
          </div>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                 style="margin-top:10px;font-size:13px;">
            <tr>
              <td style="padding:4px 6px;background:#f5f7fa;border-radius:4px;">
                ENTRY<br><strong>{_e(entry)}</strong></td>
              <td style="width:8px;"></td>
              <td style="padding:4px 6px;background:#eef7f0;border-radius:4px;">
                TARGET<br><strong>{_e(target)}</strong></td>
              <td style="width:8px;"></td>
              <td style="padding:4px 6px;background:#fdf2f4;border-radius:4px;">
                STOP<br><strong>{_e(stop)}</strong></td>
            </tr>
          </table>
          <div style="margin-top:6px;font-size:12px;color:#8a9099;">
            long momentum continuation{rr}
          </div>
          <div style="margin-top:8px;">{_pillar_badges(pick.pillars)}</div>
          <div style="margin-top:8px;font-size:13px;color:#444;line-height:1.5;">
            {_e(pick.reasoning)}
          </div>
        </td></tr>
      </table>
    </td></tr>"""


def render_email_html(picks: list[ScoutPick], today: date) -> str:
    if picks:
        rows = "".join(_pick_block(i + 1, p) for i, p in enumerate(picks))
    else:
        rows = (
            '<tr><td style="padding:16px;font-size:14px;color:#444;">'
            "No small-cap momentum names cleared the screen for the last completed "
            "session. Nothing to watch today."
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
    <tr><td style="background:#0b2440;padding:18px 16px;">
      <div style="color:#fff;font-size:18px;font-weight:700;">Small-Cap Scout</div>
      <div style="color:#9fb0cc;font-size:13px;margin-top:2px;">
        {_e(today.isoformat())} &middot; {len(picks)} ranked momentum names &middot; analysis only
      </div>
    </td></tr>
    <tr><td style="padding:12px 16px 0 16px;">
      <div style="border:1px solid #c9d4e2;background:#f2f6fb;border-radius:8px;
                  padding:10px 12px;color:#274060;font-size:12px;line-height:1.5;">
        &#9432; {_e(EOD_NOTICE)}
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
      Generated by the Small-Cap Scout screener from delayed, end-of-day public
      data (Massive / Polygon.io). 'Float' is a shares-outstanding proxy, not true
      free float. Levels are references on the underlying shares. This tool never
      places, reviews, or cancels any order.
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""


def compose_email(
    picks: list[ScoutPick], config: dict[str, Any], today: date | None = None
) -> tuple[str, str]:
    today = today or datetime.now(UTC).date()
    return build_subject(picks, today), render_email_html(picks, today)


def _recipient_set(to_addr: str, extra_recipients: list[str] | None) -> list[str]:
    """The operator default FIRST, then any extra recipients, deduped
    case-insensitively. Additive and order-stable."""
    out = [to_addr]
    seen = {to_addr.strip().lower()}
    for addr in extra_recipients or []:
        key = str(addr).strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(str(addr).strip())
    return out


def send_or_preview(
    picks: list[ScoutPick],
    config: dict[str, Any],
    *,
    dry_run: bool,
    out_path: str | None = None,
    today: date | None = None,
    smtp_factory: Any = None,
    print_fn: Any = print,
    extra_recipients: list[str] | None = None,
) -> EmailResult:
    """Compose the email, then either send it (Gmail SMTP) or, in dry-run,
    print/write it WITHOUT opening any SMTP connection.

    `to_addr` (the config/env default) stays the primary recipient; any
    `extra_recipients` (the operator-managed report list) are added and deduped.
    `smtp_factory` is only for tests -- it must never be exercised in dry-run.
    """
    today = today or datetime.now(UTC).date()
    subject, body = compose_email(picks, config, today)
    to_addr = resolve_to_addr(config)
    from_addr = resolve_from_addr(config)
    recipients = _recipient_set(to_addr, extra_recipients)

    if out_path:
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(body)

    if dry_run:
        if print_fn is not None:
            print_fn(f"[DRY RUN] Subject: {subject}")
            print_fn(f"[DRY RUN] To: {', '.join(recipients)}  From: {from_addr}")
            print_fn(f"[DRY RUN] {len(picks)} name(s). Not sending; no SMTP connection opened.")
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
    message["To"] = ", ".join(recipients)
    message.attach(MIMEText("This is an HTML email; enable HTML to read the Small-Cap Scout watchlist.", "plain", "utf-8"))
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
        server.sendmail(from_addr, recipients, message.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass
    return EmailResult(subject, body, sent=True, dry_run=False, to_addr=to_addr,
                       detail=f"sent to {', '.join(recipients)}")
