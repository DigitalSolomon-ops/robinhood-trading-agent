"""Shared DigitalSolomon branding for the scout / alert emails.

Puts the DigitalSolomon logo + wordmark in the header of every outbound email and
builds the MIME structure that makes the logo render inline across mail clients.

The logo is embedded as a CID (Content-ID) inline attachment inside a
``multipart/related`` message -- the only approach that reliably renders in Gmail
(which blocks ``data:`` URIs) without relying on an external, IAP-gated URL. If the
logo asset is absent (e.g. a checkout without ``assets/``), the header degrades to
a clean text-only wordmark and nothing is attached -- a missing asset must never
break a send.

BRANDING ONLY. Nothing here reads a secret, opens a socket, or touches an order
path; it only composes MIME parts around an HTML body the caller already rendered.
"""

from __future__ import annotations

import html as _html
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# This file lives at src/scout_email_branding.py, so parents[1] is the project
# root in both dev (agent/) and the container (/app) -- assets/ sits beside src/.
ROOT = Path(__file__).resolve().parents[1]

# Prefer the small, email-optimized logo; fall back to the full-resolution one.
_LOGO_CANDIDATES = (ROOT / "assets" / "logo-email.png", ROOT / "assets" / "logo.png")

# The Content-ID the header <img> references (src="cid:dslogo").
LOGO_CID = "dslogo"
BRAND_NAME = "DigitalSolomon"


def _load_logo_bytes() -> bytes | None:
    for path in _LOGO_CANDIDATES:
        try:
            if path.exists():
                data = path.read_bytes()
                if data:
                    return data
        except OSError:
            continue
    return None


# Read once at import; a missing/unreadable file yields None (text-only fallback).
_LOGO_BYTES: bytes | None = _load_logo_bytes()


def has_logo() -> bool:
    return _LOGO_BYTES is not None


def branded_header(product: str, subtitle: str, *, accent: str = "#12203a") -> str:
    """The email's top banner: the DigitalSolomon logo + wordmark, the product
    name, and a subtitle line, on the brand's dark ground. Returns a single
    email-table ``<tr>`` -- drop it in where the old text-only header row was.

    With the logo asset present the wordmark sits beside the inline logo
    (``src="cid:dslogo"``); without it the same banner renders text-only."""
    product_html = _html.escape(product)
    subtitle_html = _html.escape(subtitle)
    if has_logo():
        logo_cell = (
            '<td width="52" style="padding:0 12px 0 0;vertical-align:middle;">'
            f'<img src="cid:{LOGO_CID}" width="44" height="44" alt="{BRAND_NAME} logo" '
            'style="display:block;width:44px;height:44px;border-radius:8px;">'
            "</td>"
        )
    else:
        logo_cell = ""
    return (
        f'<tr><td style="background:{accent};padding:16px 18px;">'
        '<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
        f"{logo_cell}"
        '<td style="vertical-align:middle;">'
        '<div style="color:#ffffff;font-size:12px;font-weight:700;letter-spacing:1.5px;'
        f'text-transform:uppercase;opacity:.82;">{BRAND_NAME}</div>'
        '<div style="color:#ffffff;font-size:18px;font-weight:700;margin-top:1px;">'
        f"{product_html}</div>"
        '<div style="color:#9fb0cc;font-size:12px;margin-top:2px;">'
        f"{subtitle_html}</div>"
        "</td></tr></table>"
        "</td></tr>"
    )


def attach_logo(message: MIMEMultipart) -> bool:
    """Attach the inline logo (Content-ID <dslogo>) to a ``multipart/related``
    message. No-op returning False when the asset is absent."""
    if _LOGO_BYTES is None:
        return False
    image = MIMEImage(_LOGO_BYTES, _subtype="png")
    image.add_header("Content-ID", f"<{LOGO_CID}>")
    image.add_header("Content-Disposition", "inline", filename="digitalsolomon.png")
    message.attach(image)
    return True


def build_message(
    subject: str,
    from_addr: str,
    to_header: str,
    html_body: str,
    plain_fallback: str,
) -> MIMEMultipart:
    """Build the outbound message: a ``multipart/related`` carrying a
    ``multipart/alternative`` (plain + HTML) plus the inline logo. Callers send it
    exactly as before via ``server.sendmail(from_addr, recipients, msg.as_string())``.

    Using ``related`` (not a bare ``alternative``) is what lets the HTML reference
    the logo by ``cid:`` and render it inline instead of as a separate attachment."""
    root = MIMEMultipart("related")
    root["Subject"] = subject
    root["From"] = from_addr
    root["To"] = to_header

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(plain_fallback, "plain", "utf-8"))
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    root.attach(alt)

    attach_logo(root)
    return root
