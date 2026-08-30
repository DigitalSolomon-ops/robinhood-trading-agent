from __future__ import annotations

from datetime import date

from src.smallcap_scout.email import (
    DISCLAIMER,
    EOD_NOTICE,
    render_email_html,
    send_or_preview,
)
from src.smallcap_scout.levels import Levels
from src.smallcap_scout.scanner import (
    PILLAR_BIG_MOVE,
    PILLAR_CATALYST,
    PILLAR_LOW_FLOAT,
    PILLAR_PRICE,
    PILLAR_RVOL,
    ScoutPick,
)

TODAY = date(2026, 8, 28)


def make_pick(**overrides) -> ScoutPick:
    levels = Levels(
        entry=6.00, target=7.20, stop=5.40, breakout_ref=6.10, atr=0.35,
        realized_vol=0.06, horizon_days=5, method="test method",
    )
    defaults = dict(
        symbol="WINR",
        last_price=6.00,
        pct_change=20.0,
        volume=600_000.0,
        rvol=6.0,
        baseline_days=20,
        float_shares=10_000_000.0,
        float_basis="share_class_shares_outstanding",
        float_known=True,
        market_cap=60_000_000.0,
        news_score=1.0,
        catalyst="Winner Inc lands a major contract",
        news_citation="massive_news 1/1 rated positive",
        levels=levels,
        pillars=(PILLAR_BIG_MOVE, PILLAR_RVOL, PILLAR_PRICE, PILLAR_LOW_FLOAT, PILLAR_CATALYST),
        rank_score=0.87,
        reasoning="Pillars hit: big move, high RVOL, price range, low float, catalyst. "
                  "+20.0% on the session; 6.0x relative volume; $6.00 in range; "
                  "~10.0M shares outstanding (float proxy); with a positive recent catalyst.",
    )
    defaults.update(overrides)
    return ScoutPick(**defaults)


def test_html_contains_every_pick_field():
    html = render_email_html([make_pick()], TODAY)
    for token in [
        "WINR",
        "+20%",                                   # % move
        "6",                                      # last price / rvol
        "RVOL",
        "10.0M",                                  # float proxy, human-formatted
        "Winner Inc lands a major contract",      # catalyst headline
        "ENTRY",
        "TARGET",
        "STOP",
        "7.2",                                    # target level
        "5.4",                                    # stop level
        "low float",                              # a pillar badge
        "Pillars hit:",                           # reasoning line
    ]:
        assert token in html, f"missing {token!r}"


def test_html_carries_the_eod_not_realtime_notice():
    html = render_email_html([make_pick()], TODAY)
    assert "END-OF-DAY" in html
    assert "not a" in html.lower() and "pre-market" in html.lower()
    assert EOD_NOTICE in html


def test_html_contains_the_prominent_disclaimer():
    html = render_email_html([make_pick()], TODAY)
    assert "NOT FINANCIAL ADVICE" in html
    assert "DISCLAIMER" in html
    assert "SUBSTANTIAL RISK" in html
    assert "shares-outstanding" in html.lower()  # float labelled as a proxy
    assert DISCLAIMER in html


def test_unknown_float_is_rendered_as_unknown():
    pick = make_pick(float_shares=None, float_known=False,
                     pillars=(PILLAR_BIG_MOVE, PILLAR_RVOL, PILLAR_PRICE))
    html = render_email_html([pick], TODAY)
    assert "unknown" in html.lower()


def test_empty_picks_still_renders_with_disclaimer_and_notice():
    html = render_email_html([], TODAY)
    assert "No small-cap momentum names cleared" in html
    assert "NOT FINANCIAL ADVICE" in html
    assert "END-OF-DAY" in html


def test_dry_run_never_opens_an_smtp_connection():
    printed = []

    def boom():
        raise AssertionError("dry-run must not construct an SMTP client")

    result = send_or_preview(
        [make_pick()], {"email": {}}, dry_run=True, today=TODAY,
        smtp_factory=boom, print_fn=printed.append,
    )
    assert result.sent is False
    assert result.dry_run is True
    assert any("DRY RUN" in line for line in printed)


def test_dry_run_can_write_html_to_a_file(tmp_path):
    out = tmp_path / "scout.html"
    send_or_preview([make_pick()], {"email": {}}, dry_run=True, out_path=str(out),
                    today=TODAY, print_fn=lambda *_: None)
    text = out.read_text(encoding="utf-8")
    assert "NOT FINANCIAL ADVICE" in text
    assert "WINR" in text


def test_send_over_ssl_465_does_not_starttls(monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-pw-fixture")  # placeholder, not a real secret
    monkeypatch.setenv("GMAIL_USER", "sender@example.com")
    monkeypatch.setenv("SMALLCAP_SCOUT_TO", "dest@example.com")

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

    result = send_or_preview(
        [make_pick()], {"email": {"smtp_port": 465}}, dry_run=False, today=TODAY,
        smtp_factory=lambda: FakeSSLSMTP(), print_fn=lambda *_: None,
    )
    assert result.sent is True
    assert "starttls" not in events
    assert ("login", "sender@example.com", "app-pw-fixture") in events
    assert ("sendmail", "sender@example.com", ("dest@example.com",)) in events


def test_send_over_starttls_587_calls_starttls(monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-pw-fixture")
    monkeypatch.setenv("GMAIL_USER", "sender@example.com")
    monkeypatch.setenv("SMALLCAP_SCOUT_TO", "dest@example.com")

    events = []

    class FakeSMTP:
        def starttls(self):
            events.append("starttls")

        def login(self, user, password):
            events.append("login")

        def sendmail(self, from_addr, to_addrs, msg):
            events.append("sendmail")

        def quit(self):
            events.append("quit")

    result = send_or_preview(
        [make_pick()], {"email": {"smtp_port": 587}}, dry_run=False, today=TODAY,
        smtp_factory=lambda: FakeSMTP(), print_fn=lambda *_: None,
    )
    assert result.sent is True
    assert "starttls" in events


def test_send_without_password_does_not_send(monkeypatch):
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.setenv("DS_VAULT_NO_GCLOUD", "1")  # block the Secret Manager fallback

    def boom():
        raise AssertionError("must not open SMTP without a password")

    result = send_or_preview([make_pick()], {"email": {}}, dry_run=False, today=TODAY,
                             smtp_factory=boom, print_fn=lambda *_: None)
    assert result.sent is False
    assert "no Gmail app password" in result.detail
