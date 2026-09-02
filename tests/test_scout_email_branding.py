"""DigitalSolomon email branding: the shared logo header + multipart/related
builder used by all three scout/alert emails.

BRANDING ONLY -- these assert MIME structure and header markup; nothing here
sends mail or touches an order path.
"""

from __future__ import annotations

from datetime import date

import src.scout_email_branding as branding
from src.entry_alerts import email as ea_email
from src.options_scout import email as opt_email
from src.smallcap_scout import email as sc_email


def test_logo_asset_ships_in_repo():
    # assets/logo-email.png is committed, so the branded (image) path is live.
    assert branding.has_logo() is True


def test_branded_header_embeds_cid_logo_and_wordmark():
    header = branding.branded_header("Options Scout", "sub · analysis only", accent="#12203a")
    assert f"cid:{branding.LOGO_CID}" in header
    assert "DigitalSolomon" in header
    assert "Options Scout" in header
    assert "#12203a" in header  # the accent ground is honored


def test_branded_header_text_only_when_logo_absent(monkeypatch):
    monkeypatch.setattr(branding, "_LOGO_BYTES", None)
    header = branding.branded_header("Any Product", "sub")
    assert "<img" not in header
    assert f"cid:{branding.LOGO_CID}" not in header
    # the wordmark + product still render, so a logo-less checkout is still branded
    assert "DigitalSolomon" in header
    assert "Any Product" in header


def test_build_message_is_related_with_one_inline_png():
    msg = branding.build_message("subj", "from@x.com", "to@x.com", "<b>hi</b>", "plain body")
    assert msg.get_content_type() == "multipart/related"
    parts = list(msg.walk())
    assert any(p.get_content_type() == "multipart/alternative" for p in parts)
    images = [p for p in parts if p.get_content_type() == "image/png"]
    assert len(images) == 1
    assert images[0].get("Content-ID") == f"<{branding.LOGO_CID}>"
    assert images[0].get("Content-Disposition", "").startswith("inline")
    # both the HTML and plain bodies are present (decoded from their transfer encoding)
    html_parts = [p for p in parts if p.get_content_type() == "text/html"]
    plain_parts = [p for p in parts if p.get_content_type() == "text/plain"]
    assert html_parts and "hi" in html_parts[0].get_payload(decode=True).decode("utf-8")
    assert plain_parts and "plain body" in plain_parts[0].get_payload(decode=True).decode("utf-8")
    assert msg.as_string()  # serializes without raising
    assert msg["Subject"] == "subj" and msg["To"] == "to@x.com"


def test_build_message_without_logo_still_valid(monkeypatch):
    monkeypatch.setattr(branding, "_LOGO_BYTES", None)
    msg = branding.build_message("s", "f@x.com", "t@x.com", "<b>hi</b>", "plain")
    assert [p for p in msg.walk() if p.get_content_type() == "image/png"] == []
    assert any(p.get_content_type() == "multipart/alternative" for p in msg.walk())


def test_each_scout_email_header_references_the_logo():
    today = date(2026, 9, 1)
    cid = f"cid:{branding.LOGO_CID}"
    assert cid in opt_email.render_email_html([], today)
    assert cid in sc_email.render_email_html([], today)
    assert cid in ea_email.render_email_html([], today)
