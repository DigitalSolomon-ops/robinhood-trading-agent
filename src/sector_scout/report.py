"""Render the Sector Scout run table: HTML (the email body) and DOCX (the
attachment), BOTH generated from the one computed table so they can never
disagree.

Report order is the trader's decision path (the point of the overhaul):
  1. The read (judgement, 3 to 5 sentences)
  2. The change log (what moved since the previous run)
  3. The segment board (every fund, both lenses, sorted by RS ascending)
  4. Per selected segment: the Strategy (four beats), the leaders,
     probability and EV, the order ticket, the falsifier
  5. Six month calendar
  6. Settlement record, method, provenance and the disclaimer

House style: solomon-brand palette; direct labels as the secondary encoding
(the earth tones fail a chroma floor for CVD, so no color-only meaning). NO
EM DASHES AND NO EN DASHES anywhere in the output; a unit test greps for
both. Missing values render as n/a, never invented.

ANALYSIS ONLY. Educational framing throughout: "the structure that expresses
this view", never an instruction to buy.
"""

from __future__ import annotations

import html
from typing import Any

# solomon-brand tokens
INK = "#181613"
PARCHMENT = "#F8F6F1"
SAND = "#EAE6DC"
STONE = "#ABA69A"
GOLD = "#C29A2B"
JUDGMENT = "#2F4858"
CEDAR = "#6E7F5A"
CLARET = "#8a2b2b"

DISCLAIMER = (
    "NOT FINANCIAL ADVICE. This report is generated automatically by a software "
    "screener from delayed public market data. It is educational analysis of "
    "structures that express a view; it is not a recommendation, solicitation, or "
    "personalized investment advice, and no one has reviewed it. Options are complex "
    "and carry a substantial risk of the TOTAL LOSS of the amount paid; spreads can "
    "lose their full debit. Probabilities are model outputs and historical base "
    "rates over small samples; past results DO NOT predict or guarantee future "
    "performance. Nothing here places, previews, reviews, or cancels any order. You "
    "alone are responsible for your decisions; consider a licensed advisor."
)


# The legend: every term the report uses, in plain language. ONE structure
# rendered by both the HTML and the DOCX so the two can never disagree.
LEGEND: list[tuple[str, list[tuple[str, str]]]] = [
    ("Classifications (lens one: extremes)", [
        ("Coiled", "washed out against its own history (low relative strength and "
         "price percentile) but stabilising, and not expensive. A recovery setup, "
         "expressed with a call debit spread."),
        ("Falling knife", "the same depth but still falling. No structure is offered; "
         "the falsifier states what would turn it Coiled."),
        ("Extended", "near the top of its range with rich valuation or thinning "
         "breadth. Expressed with a put debit spread."),
        ("Leading and earning it", "near highs with valuation in line and earnings "
         "growing into it. Trend continuation, expressed with calls."),
        ("Mid range", "neither extreme; selected only when the continuation score is strong."),
    ]),
    ("The Top 9 and the opportunity score", [
        ("OPP / SCORE", "one cumulative 0 to 100 score per fund: classification up to "
         "25 points, RS and price percentile fit up to 10 each (a Coiled fund earns "
         "them for being washed out, others for strength), 3 month and 12 month "
         "momentum up to 10 each, continuation up to 20 (inverted for bearish plays, "
         "where weakness confirms exhaustion), premium cost fit up to 10, and rate "
         "purity up to 5. The breakdown prints beside every score. An operator-set "
         "ranking heuristic; every weight is config-tunable."),
        ("IDEAL ENTRY", "the underlying price to work the entry at today: a pullback "
         "into strength for bullish plays, a fade of strength for bearish ones, set a "
         "fraction of one ATR from the last close."),
        ("DAY FLOOR / DAY CEILING", "the session's expected range, one 14 day ATR "
         "either side of the last close. Enter near the favorable edge; never chase "
         "through the far edge."),
        ("Price action", "the plain-English tape to wait for before entering, and the "
         "level that voids the entry for the day."),
    ]),
    ("Segment board columns", [
        ("RS PCT", "rank percentile of the fund divided by SPY within its own history: "
         "0 is the weakest it has ever been against the market in the data window, 100 "
         "the strongest. The board sorts by this, weakest first; it is the leading axis."),
        ("PRICE PCT", "the share of the fund's own monthly closes sitting below today's "
         "price, over the labeled data window."),
        ("3M / 12M", "total return over the last three and twelve months."),
        ("CONT x/8", "the continuation score: beating SPY over 12 months +2, trend "
         "accelerating +2, P/E at or below the universe median +2, within 15 percent of "
         "the 200 day average +1, leader earnings improving +1."),
        ("IV RANK", "where today's at-the-money implied volatility sits in its own "
         "trailing year: 0 cheapest, 100 richest. Under 30 the report buys premium "
         "(debit spreads); over 70 it expresses the same view as a credit spread. "
         "Reads 'collecting (N/252)' until enough daily history accumulates."),
        ("BETA", "sensitivity to TLT (long bonds). When several selected funds sit "
         "above 0.5 they are one interest-rate trade, and the notes say so."),
    ]),
    ("Probability and edge", [
        ("Model probability", "the Black-Scholes chance the fund finishes past the "
         "spread's breakeven at expiry, computed from the legs' live implied volatility."),
        ("Empirical base rate", "how often this same classification actually moved the "
         "required amount within the window in this fund's own history, always with its "
         "sample size. LOW CONFIDENCE below 10 occurrences."),
        ("Divergence", "when the model and the base rate disagree by more than 15 "
         "points it is flagged: the options market is pricing something the history "
         "does not contain."),
        ("EXPECTED VALUE", "model-average dollars per spread across every possible "
         "finish, not just the extremes. Plays are ranked by this, not by comfort."),
        ("R:R", "maximum gain divided by maximum loss."),
    ]),
    ("The order ticket", [
        ("LIMIT (modeled fill)", "the price to offer: each leg's mark nudged toward "
         "the touch by 40 percent of its half-spread, rounded to five cents. A model, "
         "not a broker estimate. MIDPOINT and WORST CASE beside it show what patience "
         "saves and what certainty costs."),
        ("BREAKEVEN / MOVE REQ", "the underlying level, and the percent move to reach "
         "it, where the spread starts paying at expiry."),
        ("DELTA / NET DELTA", "share-equivalent exposure per contract; the net figure "
         "is the whole spread's."),
        ("IV", "that leg's implied volatility from the live snapshot."),
        ("OI", "open interest: contracts outstanding, the liquidity that lets you exit."),
        ("SPR%", "the bid-ask spread as a percent of mark: the cost of trading it."),
        ("DTE", "calendar days to expiry."),
        ("TAKE PROFIT / ROLL/CLOSE BY", "close at 65 percent of max gain; exit or roll "
         "by the stated date, about 45 days before expiry, whichever comes first."),
        ("THE FALSIFIER", "the specific price or event that ends the thesis. Exit "
         "there, no debate: it is also what settlement grades a LOSS against."),
    ]),
    ("General", [
        ("n/a", "the live number was missing (off market hours, thin data, or not on "
         "the data plan) and is never invented."),
        ("Breadth", "the share of the fund's constituents above their own 200 day "
         "average; a strong fund with weak breadth is one mega cap dragging "
         "stragglers, and gets demoted. The coverage percent shows how much of the "
         "cache window has been collected so far."),
        ("Settlement", "every published structure is graded later against its own "
         "falsifier, target, and deadline; the running record prints in every email."),
    ]),
]


def _e(text: Any) -> str:
    return html.escape(str(text))


def _num(value: Any, fmt: str = "{:,.2f}", na: str = "n/a") -> str:
    if value is None:
        return na
    try:
        return fmt.format(float(value))
    except (TypeError, ValueError):
        return na


def _pct(value: Any, na: str = "n/a") -> str:
    if value is None:
        return na
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return na


def _sanitize(text: str) -> str:
    """The output-wide no-dash rule, enforced at the seam: em and en dashes
    become plain hyphens wherever any upstream string carried one."""
    return text.replace("—", "-").replace("–", "-")


# --- the read -------------------------------------------------------------------


def compose_read(table: dict[str, Any]) -> str:
    """3 to 5 sentences of judgement composed from the table: best segment
    for calls, best for puts, the single best risk-to-reward named with its
    structure."""
    plays = table.get("plays") or []
    tradeable = [p for p in plays if p.get("classification") != "Falling knife"]
    with_ticket = [p for p in tradeable if p.get("ticket")]
    bullish = [p for p in tradeable if p.get("direction") == "bullish"]
    bearish = [p for p in tradeable if p.get("direction") == "bearish"]
    sentences: list[str] = []

    def _structure_of(play: dict[str, Any]) -> str:
        name = (play.get("ticket") or {}).get("structure") or play.get("intended_structure")
        if play.get("ticket"):
            return f"expressed as a {name}"
        return f"to be expressed as a {name} (live quotes pending market hours)"

    if bullish:
        top = bullish[0]
        ev_tail = (
            f" with expected value {_num(top.get('expected_value'), '{:+,.0f}')} dollars per spread"
            if top.get("expected_value") is not None
            else ""
        )
        sentences.append(
            f"The best segment for calls is {top['fund']} ({top['classification']}, "
            f"opportunity score {_num((top.get('opportunity') or {}).get('score'), '{:g}')}), "
            f"{_structure_of(top)}{ev_tail}."
        )
    else:
        sentences.append("No segment cleared the screen for calls this run.")
    if bearish:
        top = bearish[0]
        sentences.append(
            f"The best segment for puts is {top['fund']} ({top['classification']}), "
            f"{_structure_of(top)}."
        )
    else:
        sentences.append("No segment set up for puts this run.")
    best_rr = max(
        (p for p in with_ticket if p.get("reward_to_risk")),
        key=lambda p: p["reward_to_risk"],
        default=None,
    )
    if best_rr:
        sentences.append(
            f"The single best risk to reward on the board is {best_rr['fund']}: "
            f"{best_rr['structure']} at {_num(best_rr['reward_to_risk'], '{:.2f}')} to 1, "
            f"probability of profit {_pct(best_rr.get('prob_profit_bs'))} by the model"
            + (
                f" against {_pct(best_rr.get('prob_profit_empirical'))} empirically."
                if best_rr.get("prob_profit_empirical") is not None
                else " (no matching empirical sample; the base rate line says why)."
            )
        )
    knife = [p for p in plays if p.get("classification") == "Falling knife"]
    if knife:
        sentences.append(
            "No structure is offered on "
            + ", ".join(p["fund"] for p in knife)
            + ": falling knives get a falsifier, not a trade."
        )
    return " ".join(sentences)


# --- HTML ---------------------------------------------------------------------------


def _h2(text: str) -> str:
    return (
        f'<div style="font-size:16px;font-weight:700;color:{JUDGMENT};'
        f'border-bottom:2px solid {GOLD};padding-bottom:4px;margin:22px 0 10px 0;'
        f'font-family:Poppins,Inter,Segoe UI,sans-serif;">{_e(text)}</div>'
    )


def _chip(label: str, value: str, mono: bool = True) -> str:
    face = "font-family:JetBrains Mono,Consolas,monospace;" if mono else ""
    return (
        f'<td style="padding:5px 8px;background:{SAND};border-radius:4px;vertical-align:top;">'
        f'<span style="color:{STONE};font-size:10px;letter-spacing:.4px;">{_e(label)}</span><br>'
        f'<strong style="{face}font-size:13px;color:{INK};">{value}</strong></td>'
        '<td style="width:6px;"></td>'
    )


def _changelog_html(cl: dict[str, Any]) -> str:
    if cl.get("is_first_run"):
        return (
            f'<div style="font-size:13px;color:{STONE};">First stored run: no prior state '
            "to diff. The change log begins tomorrow.</div>"
        )
    parts: list[str] = []
    for alert in cl.get("falsifier_alerts") or []:
        parts.append(
            f'<div style="background:#fdf0f0;border:2px solid {CLARET};border-radius:6px;'
            f'padding:8px 10px;margin:4px 0;font-size:13px;color:{CLARET};font-weight:700;">'
            f"FALSIFIER TRIGGERED: {_e(alert)}</div>"
        )
    def _list(title: str, rows: list[str]) -> None:
        if rows:
            items = "".join(f"<li>{_e(r)}</li>" for r in rows)
            parts.append(
                f'<div style="font-size:13px;color:{INK};margin-top:6px;"><strong>{_e(title)}'
                f"</strong><ul style=\"margin:4px 0 0 18px;padding:0;\">{items}</ul></div>"
            )
    _list("Classification moves", cl.get("classification_moves") or [])
    _list("Continuation score moves", cl.get("score_moves") or [])
    _list("Order tickets re-priced", cl.get("ticket_reprices") or [])
    if not parts:
        return (
            f'<div style="font-size:13px;color:{CEDAR};">Quiet session: no classification '
            f"moved, no score changed, no ticket re-priced vs {_e(cl.get('prior_run_date'))}. "
            "The six month read stands.</div>"
        )
    return "".join(parts)


def _board_html(funds: list[dict[str, Any]]) -> str:
    head_cells = "".join(
        f'<th style="text-align:left;padding:5px 6px;font-size:10px;color:{STONE};'
        f'letter-spacing:.5px;border-bottom:1px solid {SAND};">{h}</th>'
        for h in ("FUND", "OPP", "CLASS", "RS PCT", "PRICE PCT", "3M", "12M", "CONT", "IV RANK", "BETA")
    )
    rows_html: list[str] = []
    for f in funds:
        ext = f.get("extremes") or {}
        cont = f.get("continuation") or {}
        ivr = f.get("iv_rank") or {}
        cls = f.get("classification") or "n/a"
        color = {
            "Coiled": CEDAR, "Falling knife": CLARET, "Extended": GOLD,
            "Leading and earning it": JUDGMENT,
        }.get(cls, STONE)
        iv_txt = (
            f"{ivr.get('iv_rank'):.0f}" if ivr.get("iv_rank") is not None
            else f"({ivr.get('days_collected', 0)}/252)"
        )
        opp_score = (f.get("opportunity") or {}).get("score")
        rows_html.append(
            "<tr>"
            f'<td style="padding:4px 6px;font-weight:700;color:{INK};">{_e(f["symbol"])}</td>'
            f'<td style="padding:4px 6px;font-weight:700;color:{GOLD};">{_num(opp_score, "{:g}")}</td>'
            f'<td style="padding:4px 6px;color:{color};font-weight:600;">{_e(cls)}</td>'
            f'<td style="padding:4px 6px;">{_num(ext.get("rs_pctile"), "{:.0f}")}</td>'
            f'<td style="padding:4px 6px;">{_num(ext.get("price_pctile"), "{:.0f}")}</td>'
            f'<td style="padding:4px 6px;">{_pct(ext.get("ret_3m"))}</td>'
            f'<td style="padding:4px 6px;">{_pct(ext.get("ret_12m"))}</td>'
            f'<td style="padding:4px 6px;">{cont.get("score", "n/a")}/8</td>'
            f'<td style="padding:4px 6px;">{iv_txt}</td>'
            f'<td style="padding:4px 6px;">{_num(f.get("rate_beta"), "{:+.2f}")}</td>'
            "</tr>"
        )
    return (
        '<div style="overflow-x:auto;"><table role="presentation" cellpadding="0" cellspacing="0" '
        f'style="width:100%;font-size:12px;font-family:JetBrains Mono,Consolas,monospace;color:{INK};">'
        f"<tr>{head_cells}</tr>{''.join(rows_html)}</table></div>"
    )


def _strategy_beats(play: dict[str, Any]) -> str:
    """The four required beats, in order, in plain prose, from the data."""
    ext_row = play.get("_fund_row_extremes") or {}
    cont = play.get("_fund_row_continuation") or {}
    ivr = play.get("iv_rank") or {}
    ticket = play.get("ticket") or {}
    legs = ticket.get("legs") or []
    long_leg = legs[0] if legs else {}
    cls = play.get("classification")
    bullish = play.get("direction") == "bullish"

    breadth_val = (play.get("breadth") or {}).get("pct_above_200d")
    breadth_txt = _pct(breadth_val) if breadth_val is not None else "n/a"
    why_sector = (
        f"Why this sector: {play['fund']} classifies {cls} with relative strength "
        f"percentile {_num(ext_row.get('rs_pctile'), '{:.0f}')} and price percentile "
        f"{_num(ext_row.get('price_pctile'), '{:.0f}')} over {_e(ext_row.get('window_label', 'the available window'))}; "
        f"breadth reads {breadth_txt} of constituents above their own 200 day average."
    )
    if bullish:
        why_direction = (
            f"Why this direction: calls, because the {cls} classification argues "
            + ("recovery from a washed out base with stabilising trend"
               if cls == "Coiled"
               else "trend persistence with the continuation gates passing")
            + f" (continuation score {cont.get('score', 'n/a')}/8)."
        )
    else:
        why_direction = (
            f"Why this direction: puts, because {cls} height with valuation rich and "
            "breadth thinning argues exhaustion rather than persistence."
        )
    accel = cont.get("accel")
    why_now = (
        "Why now: "
        + (f"acceleration {_num(accel, '{:+.2f}')} (3 month pace vs the 12 month), "
           if accel is not None else "")
        + f"price {'above' if ext_row.get('above_sma50') else 'below'} the 50 day and "
        f"{'above' if ext_row.get('above_sma200') else 'below'} the 200 day, weekly RSI "
        f"{_num(ext_row.get('weekly_rsi'), '{:.0f}')}, and the catalyst calendar inside the window."
    )
    delta_val = long_leg.get("delta")
    if delta_val is None:
        moneyness = "positioned near the target delta (greeks pending market hours)"
    elif abs(delta_val) >= 0.5:
        moneyness = "slightly in the money for immediate participation"
    else:
        moneyness = "out of the money for leverage on the recovery"
    why_contract = (
        f"Why this contract: the {ticket.get('dte_calendar', 'n/a')} DTE monthly sits in the "
        f"{play.get('_dte_band', '150 to 240')} day band where six month theses get room while "
        "daily theta stays a fraction of a front month's; the long leg at "
        f"{_num(long_leg.get('strike'), '{:g}')} (delta {_num(delta_val, '{:.2f}')}) is "
        + moneyness
        + f"; IV rank {ivr.get('iv_rank') if ivr.get('iv_rank') is not None else 'is still collecting'}"
        + (" says premium is cheap, so a debit structure buys it"
           if ivr.get("regime") == "buy_premium"
           else " says premium is rich, so the view is expressed as a credit spread"
           if ivr.get("regime") == "sell_premium"
           else "; the classification chose the structure")
        + "."
    )
    return " ".join((why_sector, why_direction, why_now, why_contract))


def _leaders_html(play: dict[str, Any]) -> str:
    leaders = play.get("leaders") or []
    if not leaders:
        return f'<div style="font-size:12px;color:{STONE};">no leaders confirmed this run</div>'
    rows = "".join(
        "<tr>"
        f'<td style="padding:3px 6px;font-weight:700;">{_e(l["ticker"])}</td>'
        f'<td style="padding:3px 6px;">{_e(l.get("name") or "")}</td>'
        f'<td style="padding:3px 6px;">{_num(l.get("market_cap"), "{:,.0f}")}</td>'
        f'<td style="padding:3px 6px;">{_num(l.get("pe"), "{:.1f}")}</td>'
        f'<td style="padding:3px 6px;">{_num(l.get("pb"), "{:.2f}")}</td>'
        f'<td style="padding:3px 6px;">{_pct(l.get("pos_52w"))}</td>'
        "</tr>"
        for l in leaders
    )
    dropped = play.get("dropped_seeds") or []
    dropped_html = (
        f'<div style="font-size:11px;color:{STONE};margin-top:3px;">Seeds dropped '
        f"(did not resolve live): {_e(', '.join(dropped))}</div>"
        if dropped else ""
    )
    return (
        '<div style="overflow-x:auto;"><table role="presentation" cellpadding="0" cellspacing="0" '
        f'style="width:100%;font-size:11px;font-family:JetBrains Mono,Consolas,monospace;">'
        f'<tr><th style="text-align:left;padding:3px 6px;color:{STONE};">TICKER</th>'
        f'<th style="text-align:left;padding:3px 6px;color:{STONE};">NAME</th>'
        f'<th style="text-align:left;padding:3px 6px;color:{STONE};">MKT CAP</th>'
        f'<th style="text-align:left;padding:3px 6px;color:{STONE};">P/E</th>'
        f'<th style="text-align:left;padding:3px 6px;color:{STONE};">P/B</th>'
        f'<th style="text-align:left;padding:3px 6px;color:{STONE};">52W POS</th></tr>'
        f"{rows}</table></div>{dropped_html}"
    )


def _ticket_html(play: dict[str, Any]) -> str:
    ticket = play.get("ticket") or {}
    if not ticket:
        return (
            f'<div style="background:{SAND};border-radius:6px;padding:10px;font-size:13px;'
            f'color:{INK};"><strong>No structure.</strong> {_e(play.get("skipped_reason") or "")}</div>'
        )
    legs = ticket.get("legs") or []
    leg_rows = "".join(
        "<tr>"
        f'<td style="padding:3px 6px;font-weight:700;color:'
        f'{CEDAR if l.get("action") == "buy" else CLARET};">{_e(l.get("action", "").upper())}</td>'
        f'<td style="padding:3px 6px;font-family:JetBrains Mono,Consolas,monospace;">{_e(l.get("ticker"))}</td>'
        f'<td style="padding:3px 6px;">{_num(l.get("strike"), "{:g}")}</td>'
        f'<td style="padding:3px 6px;">{_e(l.get("expiry"))}</td>'
        f'<td style="padding:3px 6px;">{_num(l.get("bid"))} / {_num(l.get("ask"))}</td>'
        f'<td style="padding:3px 6px;">{_num(l.get("mark"))}</td>'
        f'<td style="padding:3px 6px;">{_num(l.get("delta"), "{:+.2f}")}</td>'
        f'<td style="padding:3px 6px;">{_pct(l.get("iv"))}</td>'
        f'<td style="padding:3px 6px;">{_num(l.get("open_interest"), "{:,.0f}")}</td>'
        f'<td style="padding:3px 6px;">{_num(l.get("spread_pct_of_mark") if isinstance(l.get("spread_pct_of_mark"), (int, float)) else None, "{:.1f}")}</td>'
        "</tr>"
        for l in legs
    )
    chips1 = (
        _chip("LIMIT (modeled fill)", _num(ticket.get("limit_price")))
        + _chip("MIDPOINT", _num(ticket.get("midpoint")))
        + _chip("WORST CASE", _num(ticket.get("worst_case")))
        + _chip("ORDER", _e(ticket.get("order_type", "n/a")), mono=False)
        + _chip("DTE", str(ticket.get("dte_calendar", "n/a")))
    )
    chips2 = (
        _chip("MAX LOSS $", _num(ticket.get("max_loss"), "{:,.0f}"))
        + _chip("MAX GAIN $", _num(ticket.get("max_gain"), "{:,.0f}"))
        + _chip("R:R", _num(ticket.get("reward_to_risk"), "{:.2f}"))
        + _chip("BREAKEVEN", _num(ticket.get("breakeven")))
        + _chip("MOVE REQ", _num(ticket.get("move_required_pct"), "{:+.1f}") + "%")
        + _chip("NET DELTA", _num(ticket.get("net_delta"), "{:+.2f}"))
    )
    chips3 = (
        _chip("TAKE PROFIT $", _num(ticket.get("take_profit_level"), "{:,.0f}"))
        + _chip("ROLL/CLOSE BY", _e(ticket.get("roll_or_close_date", "n/a")))
    )
    rules = "".join(f"<li>{_e(r)}</li>" for r in ticket.get("execution_rules") or [])
    snap_at = legs[0].get("snapshot_at") if legs else "n/a"
    return (
        f'<div style="border:1px solid {SAND};border-radius:8px;padding:10px;">'
        f'<div style="font-weight:700;font-size:14px;color:{JUDGMENT};">ORDER TICKET: '
        f'{_e(ticket.get("structure", ""))}</div>'
        '<div style="overflow-x:auto;"><table role="presentation" cellpadding="0" cellspacing="0" '
        'style="width:100%;font-size:11px;margin-top:6px;">'
        f'<tr><th style="text-align:left;padding:3px 6px;color:{STONE};">SIDE</th>'
        f'<th style="text-align:left;padding:3px 6px;color:{STONE};">CONTRACT</th>'
        f'<th style="text-align:left;padding:3px 6px;color:{STONE};">STRIKE</th>'
        f'<th style="text-align:left;padding:3px 6px;color:{STONE};">EXPIRY</th>'
        f'<th style="text-align:left;padding:3px 6px;color:{STONE};">BID/ASK</th>'
        f'<th style="text-align:left;padding:3px 6px;color:{STONE};">MARK</th>'
        f'<th style="text-align:left;padding:3px 6px;color:{STONE};">DELTA</th>'
        f'<th style="text-align:left;padding:3px 6px;color:{STONE};">IV</th>'
        f'<th style="text-align:left;padding:3px 6px;color:{STONE};">OI</th>'
        f'<th style="text-align:left;padding:3px 6px;color:{STONE};">SPR%</th></tr>'
        f"{leg_rows}</table></div>"
        f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:8px;"><tr>{chips1}</tr></table>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:6px;"><tr>{chips2}</tr></table>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:6px;"><tr>{chips3}</tr></table>'
        f'<div style="font-size:11px;color:{STONE};margin-top:6px;">{_e(ticket.get("fill_model_note", ""))} '
        f"Snapshot captured {_e(snap_at)}.</div>"
        f'<div style="font-size:12px;color:{INK};margin-top:6px;"><strong>Standing rules:</strong>'
        f'<ul style="margin:4px 0 0 18px;padding:0;">{rules}</ul></div>'
        "</div>"
    )


def _play_block_html(rank: int, play: dict[str, Any]) -> str:
    prob_line = (
        f"Model probability of profit {_pct(play.get('prob_profit_bs'))}"
        + (
            f" | empirical base rate {_pct(play.get('prob_profit_empirical'))} "
            f"({_e((play.get('base_rate') or {}).get('summary', ''))})"
            if play.get("prob_profit_empirical") is not None
            else f" | empirical base rate: {_e((play.get('base_rate') or {}).get('summary', 'n/a'))}"
        )
    )
    divergence = ""
    if play.get("divergence_flagged"):
        divergence = (
            f'<div style="background:#fff6e0;border:1px solid {GOLD};border-radius:4px;'
            f'padding:6px 8px;font-size:12px;color:{INK};margin-top:5px;">'
            f"The model and the base rate disagree by {_num(play.get('divergence_points'), '{:.0f}')} "
            "points. That gap is itself a finding: the options market is pricing something "
            "the history does not contain.</div>"
        )
    corr = play.get("correlated_with") or []
    corr_html = (
        f'<div style="font-size:12px;color:{STONE};margin-top:4px;">One position, not two: '
        f"{_e(', '.join(corr))} correlates above the threshold; this fund is the primary "
        "expression and the alternative is listed on the board.</div>"
        if corr else ""
    )
    ivr_val = (play.get("iv_rank") or {}).get("iv_rank")
    ev_chip = (
        _chip("EXPECTED VALUE $/spread", _num(play.get("expected_value"), "{:+,.0f}"))
        + _chip("R:R", _num(play.get("reward_to_risk"), "{:.2f}"))
        + _chip("IV RANK", _e(ivr_val) if ivr_val is not None else "collecting")
        + _chip("RATE BETA", _num(play.get("rate_beta"), "{:+.2f}"))
    )
    context_bits = []
    for label, key in (
        ("Short interest", "short_interest"),
        ("Term structure", "term_structure"),
        ("Skew", "skew"),
        ("Seasonality", "seasonality"),
    ):
        val = play.get(key)
        if val and val != "n/a":
            context_bits.append(f"<strong>{label}:</strong> {_e(val)}")
    context_html = (
        f'<div style="font-size:12px;color:{INK};margin-top:6px;line-height:1.5;">'
        + "<br>".join(context_bits) + "</div>"
        if context_bits else ""
    )
    return (
        f'<div style="border:1px solid {SAND};border-radius:10px;padding:14px;margin:14px 0;'
        f'background:#ffffff;">'
        f'<div style="font-size:16px;font-weight:800;color:{INK};">'
        f'<span style="color:{STONE};">#{rank}</span> {_e(play["fund"])} '
        f'<span style="color:{JUDGMENT};font-size:13px;">{_e(play.get("classification", ""))}'
        f' | {_e(play.get("direction", ""))} | spot {_num(play.get("spot"))}</span></div>'
        f'<div style="font-size:13px;color:{INK};line-height:1.65;margin-top:8px;">'
        f'<strong style="color:{JUDGMENT};">THE STRATEGY.</strong> {_e(play.get("strategy", ""))}</div>'
        f'{corr_html}'
        f'<div style="margin-top:10px;">{_leaders_html(play)}</div>'
        f'<div style="font-size:13px;color:{INK};margin-top:8px;">{prob_line}</div>'
        f"{divergence}"
        f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:8px;"><tr>{ev_chip}</tr></table>'
        f'<div style="margin-top:10px;">{_ticket_html(play)}</div>'
        f'{context_html}'
        f'<div style="background:#fdf0f0;border-left:4px solid {CLARET};padding:8px 10px;'
        f'margin-top:10px;font-size:13px;color:{INK};"><strong>THE FALSIFIER.</strong> '
        f'{_e(play.get("falsifier") or play.get("skipped_reason") or "n/a")}</div>'
        "</div>"
    )


def prepare_table(table: dict[str, Any]) -> dict[str, Any]:
    """Inject the derived prose (strategy beats + fund-row context) into each
    play. Idempotent, and called by BOTH renderers so neither depends on the
    other having run first (review finding: the DOCX silently required the
    HTML pass to have mutated the shared table)."""
    band = table.get("structures_band") or {}
    for play in table.get("plays") or []:
        row = next(
            (f for f in table.get("funds", []) if f["symbol"] == play["fund"]), {}
        )
        play["_fund_row_extremes"] = row.get("extremes") or {}
        play["_fund_row_continuation"] = row.get("continuation") or {}
        play["_dte_band"] = (
            f"{band.get('dte_min', 150)} to {band.get('dte_max', 240)}"
        )
        play["strategy"] = _strategy_beats(play)
    return table


def _top9_block_html(rank: int, play: dict[str, Any]) -> str:
    """One plain-English Top 9 entry: the opportunity, the play, the ideal
    entry, and today's ceiling/floor with the price action to wait for."""
    opp = play.get("opportunity") or {}
    levels = play.get("day_levels") or {}
    ticket = play.get("ticket") or {}
    structure = ticket.get("structure") or play.get("intended_structure") or "no structure"
    quotes_note = "" if ticket else " (live quotes pending market hours; sized ticket appears on a market-hours run)"
    ext = play.get("_fund_row_extremes") or {}
    cont = play.get("_fund_row_continuation") or {}

    why = (
        f"{play['fund']} is the number {rank} opportunity on the board with a score of "
        f"{_num(opp.get('score'), '{:g}')} out of 100: it classifies "
        f"{play.get('classification')}, sits at relative strength percentile "
        f"{_num(ext.get('rs_pctile'), '{:.0f}')} and price percentile "
        f"{_num(ext.get('price_pctile'), '{:.0f}')}, has moved "
        f"{_pct(ext.get('ret_3m'))} over three months and {_pct(ext.get('ret_12m'))} over "
        f"twelve, and carries a continuation score of {cont.get('score', 'n/a')}/8."
    )
    the_play = (
        f"The play is a {structure}{quotes_note}, "
        f"{'betting the recovery' if play.get('direction') == 'bullish' and play.get('classification') == 'Coiled' else 'riding the trend' if play.get('direction') == 'bullish' else 'fading the extension'}."
    )
    if levels:
        entry_line = (
            f"Ideal entry on the underlying: {_num(levels.get('ideal_entry'))}. "
            f"Today's expected range: floor {_num(levels.get('day_floor'))}, ceiling "
            f"{_num(levels.get('day_ceiling'))} (last close {_num(levels.get('reference_close'))}, "
            f"ATR {_num(levels.get('atr'))})."
        )
    else:
        entry_line = "Day levels: n/a this run."
    action = play.get("price_action") or ""

    chips = (
        _chip("SCORE", _num(opp.get("score"), "{:g}"))
        + _chip("DIRECTION", _e(play.get("direction", "n/a")), mono=False)
        + _chip("IDEAL ENTRY", _num(levels.get("ideal_entry")))
        + _chip("DAY FLOOR", _num(levels.get("day_floor")))
        + _chip("DAY CEILING", _num(levels.get("day_ceiling")))
    )
    return (
        f'<div style="border:1px solid {SAND};border-left:4px solid {GOLD};border-radius:8px;'
        'padding:12px 14px;margin:10px 0;background:#ffffff;">'
        f'<div style="font-size:15px;font-weight:800;color:{INK};">'
        f'<span style="color:{GOLD};">#{rank}</span> {_e(play["fund"])} '
        f'<span style="color:{STONE};font-size:12px;font-weight:400;">'
        f'score {_e((play.get("opportunity") or {}).get("breakdown", ""))}</span></div>'
        f'<div style="font-size:13px;color:{INK};line-height:1.65;margin-top:6px;">{_e(why)} '
        f'{_e(the_play)}</div>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:8px;"><tr>{chips}</tr></table>'
        f'<div style="font-size:13px;color:{INK};line-height:1.65;margin-top:8px;">'
        f'<strong style="color:{JUDGMENT};">{_e(entry_line)}</strong></div>'
        + (
            f'<div style="font-size:13px;color:{INK};line-height:1.65;margin-top:6px;">{_e(action)}</div>'
            if action else ""
        )
        + "</div>"
    )


def _legend_html() -> str:
    blocks: list[str] = []
    for group, entries in LEGEND:
        rows = "".join(
            '<tr>'
            f'<td style="padding:3px 10px 3px 0;vertical-align:top;white-space:nowrap;">'
            f'<strong style="color:{JUDGMENT};font-size:11px;">{_e(term)}</strong></td>'
            f'<td style="padding:3px 0;color:{INK};font-size:11px;line-height:1.55;">{_e(defn)}</td>'
            '</tr>'
            for term, defn in entries
        )
        blocks.append(
            f'<div style="margin-top:10px;font-size:12px;font-weight:700;color:{STONE};'
            f'letter-spacing:.4px;text-transform:uppercase;">{_e(group)}</div>'
            '<table role="presentation" cellpadding="0" cellspacing="0" '
            f'style="width:100%;margin-top:4px;">{rows}</table>'
        )
    return (
        f'<div style="background:{PARCHMENT};border:1px solid {SAND};border-radius:8px;'
        'padding:12px 14px;">' + "".join(blocks) + "</div>"
    )


def render_html(table: dict[str, Any], changelog: dict[str, Any], settlement_line: str) -> str:
    from .. import scout_email_branding as branding

    table = prepare_table(table)
    plays = table.get("plays") or []

    read = compose_read(table)
    tradeable = [p for p in plays if p.get("classification") != "Falling knife"]
    top9_blocks = "".join(
        _top9_block_html(i + 1, p) for i, p in enumerate(tradeable[:9])
    ) or (
        f'<div style="font-size:13px;color:{STONE};">No tradeable opportunities '
        "cleared the screen this run.</div>"
    )
    play_blocks = "".join(_play_block_html(i + 1, p) for i, p in enumerate(plays))
    calendar_rows = "".join(
        f'<tr><td style="padding:3px 8px;font-weight:700;">{_e(r["ticker"])}</td>'
        f'<td style="padding:3px 8px;">{_e(r["earnings_date"])}</td></tr>'
        for r in table.get("calendar") or []
    ) or '<tr><td style="padding:3px 8px;color:' + STONE + ';">none resolved this run</td></tr>'
    notes_html = "".join(f"<li>{_e(n)}</li>" for n in table.get("notes") or [])
    corr_items = table.get("correlation_matrix") or {}
    corr_html = ", ".join(f"{k} {v:+.2f}" for k, v in sorted(corr_items.items())) or "n/a"

    window = table.get("window") or {}
    subtitle = (
        f"{table.get('run_date')} | window {window.get('start')} to {window.get('end')}"
        f" | data window {table.get('data_window')} | analysis only"
    )
    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:{PARCHMENT};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PARCHMENT};">
<tr><td align="center" style="padding:20px 10px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="max-width:680px;background:#ffffff;border-radius:10px;overflow:hidden;
                font-family:Inter,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:{INK};">
    {branding.branded_header("Sector Scout", subtitle, accent=JUDGMENT)}
    <tr><td style="padding:18px;">
      {_h2("The read")}
      <div style="font-size:14px;line-height:1.7;color:{INK};">{_e(read)}</div>
      {_h2("The Top 9: best opportunities on the board, ranked")}
      <div style="font-size:12px;color:{STONE};margin-bottom:4px;">One cumulative score
      across CLASS, RS PCT, PRICE PCT, 3M, 12M, CONT, IV RANK and BETA, scored by how
      cleanly each column fits the trade the fund argues for. The breakdown prints on
      every entry; the legend has the formula. Day levels are for TODAY's session, from
      the last close and the 14 day average true range.</div>
      {top9_blocks}
      {_h2("Change log")}
      {_changelog_html(changelog)}
      {_h2("The segment board (both lenses, sorted by relative strength ascending)")}
      <div style="font-size:11px;color:{STONE};margin-bottom:6px;">{_e(table.get("calibration_note", ""))}</div>
      {_board_html(table.get("funds") or [])}
      {_h2("The plays, ranked by expected value")}
      {play_blocks or '<div style="font-size:13px;color:' + STONE + ';">No structures this run.</div>'}
      {_h2("Six month calendar (leader earnings inside the option window)")}
      <div style="overflow-x:auto;"><table role="presentation" cellpadding="0" cellspacing="0" style="font-size:12px;">{calendar_rows}</table></div>
      {_h2("Settlement: the report grades itself")}
      <div style="font-size:13px;color:{INK};">{_e(settlement_line)}</div>
      {_h2("Legend: how to read this report")}
      {_legend_html()}
      {_h2("Method and provenance")}
      <div style="font-size:12px;color:{INK};line-height:1.6;">
        Correlations (selected set): <span style="font-family:JetBrains Mono,Consolas,monospace;">{_e(corr_html)}</span>
      </div>
      <ul style="font-size:12px;color:{INK};line-height:1.55;margin:8px 0 0 18px;padding:0;">
        <li>Every option figure traces to a Massive per-contract snapshot captured this run; the timestamp prints on each ticket.</li>
        <li>Indicators, probabilities and base rates are computed locally so the live rule and the replay cannot drift.</li>
        <li>Black-Scholes and the empirical base rate always travel together; the base rate always carries its sample size.</li>
        <li>The five year ratio history endpoint is not on the current data plan (403 verified); the valuation overlay is a cross-sectional leaders-median proxy and is labeled as such.</li>
        <li>Stock history is capped by the plan at the labeled data window; percentiles are computed over the actual window, never extrapolated.</li>
        <li>The fill-model limit price is a model, not a broker estimate.</li>
        {notes_html}
      </ul>
    </td></tr>
    <tr><td style="padding:0 18px 18px 18px;">
      <div style="border:2px solid {CLARET};background:#fdf2f2;border-radius:8px;padding:12px 14px;">
        <div style="color:{CLARET};font-weight:800;font-size:13px;letter-spacing:.5px;">DISCLAIMER, READ THIS</div>
        <div style="color:#5a1120;font-size:12px;line-height:1.6;margin-top:6px;">{_e(DISCLAIMER)}</div>
      </div>
    </td></tr>
    <tr><td style="padding:0 18px 20px 18px;color:{STONE};font-size:11px;line-height:1.5;">
      Generated {_e(table.get("generated_at"))} by the Sector Scout screener from delayed public
      data (Massive). This tool never places, reviews, or cancels any order.
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""
    return _sanitize(body)


# --- DOCX -----------------------------------------------------------------------------


def render_docx_bytes(table: dict[str, Any], changelog: dict[str, Any], settlement_line: str) -> bytes | None:
    """The DOCX attachment from the SAME table. Returns None when python-docx
    is unavailable (the email still sends; the method section notes it)."""
    try:
        from io import BytesIO

        from docx import Document
        from docx.shared import Pt, RGBColor
    except ImportError:
        return None

    doc = Document()
    ink = RGBColor(0x18, 0x16, 0x13)
    judgment = RGBColor(0x2F, 0x48, 0x58)

    def head(text: str, level: int = 1) -> None:
        h = doc.add_heading(_sanitize(text), level=level)
        for run in h.runs:
            run.font.color.rgb = judgment

    def para(text: str, size: int = 10) -> None:
        p = doc.add_paragraph(_sanitize(text))
        for run in p.runs:
            run.font.size = Pt(size)
            run.font.color.rgb = ink

    table = prepare_table(table)
    window = table.get("window") or {}
    head(f"Sector Scout: {table.get('run_date')}", 0)
    para(
        f"Option window {window.get('start')} to {window.get('end')}. "
        f"Data window {table.get('data_window')}. Analysis only; nothing here trades."
    )

    head("The read")
    para(compose_read(table), 11)

    head("The Top 9: best opportunities on the board, ranked")
    tradeable = [
        p for p in (table.get("plays") or [])
        if p.get("classification") != "Falling knife"
    ]
    for i, play in enumerate(tradeable[:9], start=1):
        opp = play.get("opportunity") or {}
        levels = play.get("day_levels") or {}
        ticket = play.get("ticket") or {}
        structure = ticket.get("structure") or play.get("intended_structure") or "no structure"
        head(f"#{i} {play.get('fund')} (score {opp.get('score', 'n/a')})", 2)
        para(f"Score breakdown: {opp.get('breakdown', 'n/a')}", 9)
        para(
            f"{play.get('fund')} classifies {play.get('classification')}; the play is a "
            f"{structure}" + ("" if ticket else " (live quotes pending market hours)") + ".",
            10,
        )
        if levels:
            para(
                f"Ideal entry {levels.get('ideal_entry')}. Today's expected range: floor "
                f"{levels.get('day_floor')}, ceiling {levels.get('day_ceiling')} (last close "
                f"{levels.get('reference_close')}, ATR {levels.get('atr')}).",
                10,
            )
        if play.get("price_action"):
            para(str(play.get("price_action")), 10)

    head("Change log")
    if changelog.get("is_first_run"):
        para("First stored run: no prior state to diff.")
    else:
        for alert in changelog.get("falsifier_alerts") or []:
            para(f"FALSIFIER TRIGGERED: {alert}", 11)
        for section, rows in (
            ("Classification moves", changelog.get("classification_moves")),
            ("Continuation score moves", changelog.get("score_moves")),
            ("Order tickets re-priced", changelog.get("ticket_reprices")),
        ):
            if rows:
                para(section + ":", 10)
                for r in rows:
                    doc.add_paragraph(_sanitize(str(r)), style="List Bullet")
        if changelog.get("quiet"):
            para(f"Quiet session vs {changelog.get('prior_run_date')}: nothing moved.")

    head("The segment board")
    board = doc.add_table(rows=1, cols=8)
    hdr = board.rows[0].cells
    for i, label in enumerate(("Fund", "Class", "RS pct", "Price pct", "3M", "12M", "Cont", "Beta")):
        hdr[i].text = label
    for f in table.get("funds") or []:
        ext = f.get("extremes") or {}
        cont = f.get("continuation") or {}
        cells = board.add_row().cells
        cells[0].text = str(f.get("symbol"))
        cells[1].text = str(f.get("classification"))
        cells[2].text = _num(ext.get("rs_pctile"), "{:.0f}")
        cells[3].text = _num(ext.get("price_pctile"), "{:.0f}")
        cells[4].text = _pct(ext.get("ret_3m"))
        cells[5].text = _pct(ext.get("ret_12m"))
        cells[6].text = f"{cont.get('score', 'n/a')}/8"
        cells[7].text = _num(f.get("rate_beta"), "{:+.2f}")

    head("The plays")
    for i, play in enumerate(table.get("plays") or [], start=1):
        head(f"#{i} {play.get('fund')}: {play.get('classification')}, {play.get('direction')}", 2)
        para("THE STRATEGY. " + str(play.get("strategy", "")), 10)
        ticket = play.get("ticket") or {}
        if ticket:
            para(
                f"Structure: {ticket.get('structure')}. Limit {_num(ticket.get('limit_price'))} "
                f"(midpoint {_num(ticket.get('midpoint'))}, worst case {_num(ticket.get('worst_case'))}), "
                f"{ticket.get('order_type')}, DTE {ticket.get('dte_calendar')}. "
                f"Max loss {_num(ticket.get('max_loss'), '{:,.0f}')}, max gain "
                f"{_num(ticket.get('max_gain'), '{:,.0f}')}, R:R {_num(ticket.get('reward_to_risk'), '{:.2f}')}, "
                f"breakeven {_num(ticket.get('breakeven'))} "
                f"({_num(ticket.get('move_required_pct'), '{:+.1f}')}% move), net delta "
                f"{_num(ticket.get('net_delta'), '{:+.2f}')}. Take profit at "
                f"{_num(ticket.get('take_profit_level'), '{:,.0f}')}; roll or close by "
                f"{ticket.get('roll_or_close_date')}."
            )
            for leg in ticket.get("legs") or []:
                para(
                    f"  {str(leg.get('action', '')).upper()} {leg.get('ticker')} strike "
                    f"{_num(leg.get('strike'), '{:g}')} exp {leg.get('expiry')}: bid {_num(leg.get('bid'))} "
                    f"ask {_num(leg.get('ask'))} mark {_num(leg.get('mark'))} delta "
                    f"{_num(leg.get('delta'), '{:+.2f}')} IV {_pct(leg.get('iv'))} OI "
                    f"{_num(leg.get('open_interest'), '{:,.0f}')} (snapshot {leg.get('snapshot_at')})",
                    9,
                )
            for rule in ticket.get("execution_rules") or []:
                doc.add_paragraph(_sanitize(str(rule)), style="List Bullet")
        else:
            para("No structure. " + str(play.get("skipped_reason") or ""))
        para(
            f"Probability of profit: model {_pct(play.get('prob_profit_bs'))}, empirical "
            f"{(_pct(play.get('prob_profit_empirical')) if play.get('prob_profit_empirical') is not None else 'n/a')} "
            f"({(play.get('base_rate') or {}).get('summary', 'n/a')}). Expected value "
            f"{_num(play.get('expected_value'), '{:+,.0f}')} dollars per spread."
        )
        if play.get("divergence_flagged"):
            para(
                f"The model and the base rate disagree by {_num(play.get('divergence_points'), '{:.0f}')} "
                "points; the options market is pricing something the history does not contain."
            )
        para("THE FALSIFIER. " + str(play.get("falsifier") or play.get("skipped_reason") or "n/a"))

    head("Six month calendar")
    for row in table.get("calendar") or []:
        doc.add_paragraph(
            _sanitize(f"{row.get('ticker')}: {row.get('earnings_date')}"), style="List Bullet"
        )

    head("Settlement")
    para(settlement_line)

    head("Legend: how to read this report")
    for group, entries in LEGEND:
        head(group, 2)
        for term, defn in entries:
            para(f"{term}: {defn}", 9)

    head("Method, provenance and disclaimer")
    for note in table.get("notes") or []:
        doc.add_paragraph(_sanitize(str(note)), style="List Bullet")
    para(DISCLAIMER, 9)

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()
