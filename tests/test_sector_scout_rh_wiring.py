"""The Robinhood wiring: snapshot round-trip and staleness, fundamentals as
the P/E authority (P0-2), full leader resolution with named failures (P0-3),
fill-rate limits, the liquidity gate, prior-session pricing (P0-1), and the
null-guarded narrative (P1-4)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from src.sector_scout.analyzer import confirm_leaders
from src.sector_scout.report import _strategy_beats
from src.sector_scout.robinhood_source import (
    RhSnapshot,
    build_manifest,
    load_snapshot,
    save_snapshot,
)
from src.sector_scout.structures import (
    Leg,
    build_debit_spread_ticket,
    implied_move_from_straddle,
    leg_from_rh_quote,
    liquidity_violations,
)

# Live values recorded from the connector on 2026-09-07 (the P0-2 fixture).
RH_FUNDAMENTALS = {
    "NVDA": {"pe_ratio": "29.120421", "pb_ratio": "24.292100",
             "market_cap": "5666095413500.0", "high_52_weeks": "236.54",
             "low_52_weeks": "164.07", "industry": "Semiconductors",
             "ex_dividend_date": "2026-09-10"},
    "XOM": {"pe_ratio": "20.521697", "pb_ratio": "2.528110",
            "market_cap": "655685532100.0", "high_52_weeks": "176.41",
            "low_52_weeks": "108.35", "industry": "Integrated Oil",
            "ex_dividend_date": "2026-08-17"},
}


def _snapshot(**overrides) -> RhSnapshot:
    base = dict(
        generated_at=datetime.now(UTC).isoformat(),
        fundamentals=RH_FUNDAMENTALS,
        fundamentals_not_found=["GONETICKER"],
    )
    base.update(overrides)
    return RhSnapshot(**base)


def test_snapshot_round_trip_and_staleness(tmp_path: Path) -> None:
    path = tmp_path / "rh.json"
    save_snapshot(
        {"generated_at": datetime.now(UTC).isoformat(),
         "fundamentals": RH_FUNDAMENTALS, "fundamentals_not_found": []},
        path,
    )
    snap = load_snapshot(path)
    assert snap is not None
    assert snap.fundamental("NVDA", "pe_ratio") == 29.120421
    assert snap.is_fresh(20)

    stale = RhSnapshot(generated_at=(datetime.now(UTC) - timedelta(hours=30)).isoformat())
    assert not stale.is_fresh(20)
    assert "hours old" in stale.age_label()

    # Wrong schema version refuses to load (never a silent misparse).
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema_version"] = 999
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert load_snapshot(path) is None


def test_pe_within_10pct_of_broker_value() -> None:
    """P0-2 acceptance: a known ticker's P/E is within 10 percent of the
    Robinhood value. NVDA reads near 29, not near 7."""
    snap = _snapshot()
    leaders, _, _ = confirm_leaders(["NVDA"], {}, snap)
    pe = leaders[0]["pe"]
    assert pe is not None
    assert abs(pe - 29.12) / 29.12 < 0.10
    assert pe > 20  # not the broken quarterly-EPS 6.9


def test_leaders_resolve_fully_and_failures_are_named() -> None:
    """P0-3: all resolvable seeds populate; unresolved ones are NAMED."""
    snap = _snapshot()
    cache = {"NVDA": [(date(2026, 9, 5), 230.0)], "XOM": [(date(2026, 9, 5), 160.0)]}
    leaders, dropped, unresolved = confirm_leaders(
        ["NVDA", "XOM", "GONETICKER", "NOSNAPROW"], cache, snap
    )
    symbols = {l["ticker"] for l in leaders}
    assert symbols == {"NVDA", "XOM"}
    assert "GONETICKER" in dropped            # in the broker's not_found
    assert "GONETICKER" in unresolved
    assert "NOSNAPROW" in unresolved          # named, never a silent n/a row
    nvda = next(l for l in leaders if l["ticker"] == "NVDA")
    assert nvda["pe"] == 29.12
    assert nvda["role"] == "Semiconductors"
    assert nvda["pos_52w"] is not None        # from RH 52w range + cache last
    assert nvda["fundamentals_source"] == "robinhood"


def _quote(bid, ask, mark, *, delta=0.5, oi=1000, vol=50, hfb=None, hfs=None,
           cop=None, close=None) -> dict:
    q = {"bid_price": bid, "ask_price": ask, "adjusted_mark_price": mark,
         "mark_price": mark, "delta": delta, "implied_volatility": 0.25,
         "theta": -0.02, "vega": 0.30, "open_interest": oi, "volume": vol,
         "updated_at": "2026-09-08T15:00:00Z",
         "high_fill_rate_buy_price": hfb, "high_fill_rate_sell_price": hfs,
         "chance_of_profit_long": cop}
    if close is not None:
        q["close"] = {"price": close}
    return q


def _inst(strike, typ="call", expiry="2027-03-19") -> dict:
    return {"id": f"id-{typ}-{strike}", "strike_price": strike,
            "type": typ, "expiration_date": expiry}


def test_fill_rate_fields_drive_the_limit() -> None:
    """P0 field list: the ticket limit is high_fill_rate_buy(long) minus
    high_fill_rate_sell(short), rounded to five cents -- not the midpoint,
    not the old model."""
    long_leg = leg_from_rh_quote(
        "buy", "call", _inst(100), _quote(4.0, 4.4, 4.2, hfb=4.32, cop=0.41),
        snapshot_at="t",
    )
    short_leg = leg_from_rh_quote(
        "sell", "call", _inst(110), _quote(1.55, 1.65, 1.6, delta=0.28, hfs=1.53),
        snapshot_at="t",
    )
    built = build_debit_spread_ticket(
        direction="bullish", long_leg=long_leg, short_leg=short_leg,
        spot=100.0, today=date(2026, 9, 8), cfg={}, rate=0.04,
        implied_move_pct=0.18,
    )
    assert built is not None
    ticket, _ = built
    assert ticket.limit_basis == "fill_rate_fields"
    assert ticket.limit_price == 2.8            # 4.32 - 1.53 = 2.79 -> 2.80
    assert ticket.midpoint == 2.6
    assert ticket.worst_case == 2.85            # long ask - short bid
    assert ticket.broker_chance_of_profit_long == 0.41
    assert ticket.pricing_basis == "live"
    assert ticket.theta_pct_of_debit_60d is not None
    assert ticket.vega_crush_pnl is not None
    assert ticket.move_required_vs_implied is not None
    assert ticket.exit_underlying_at_take_profit is not None
    assert ticket.liquidity_notes == ()


def test_prior_session_pricing_labels_itself() -> None:
    """P0-1: a pre-market snapshot with dark quotes still prices a ticket
    from settled marks, labeled prior-session, never aborted."""
    long_leg = leg_from_rh_quote(
        "buy", "call", _inst(100), _quote(None, None, 4.2, delta=None, vol=0),
        snapshot_at="t",
    )
    short_leg = leg_from_rh_quote(
        "sell", "call", _inst(110), _quote(None, None, 1.6, delta=None, vol=0),
        snapshot_at="t",
    )
    assert long_leg is not None and long_leg.pricing_basis == "prior_session_close"
    built = build_debit_spread_ticket(
        direction="bullish", long_leg=long_leg, short_leg=short_leg,
        spot=100.0, today=date(2026, 9, 8), cfg={}, rate=0.04,
    )
    assert built is not None
    ticket, _ = built
    assert ticket.pricing_basis == "prior_session_close"
    assert ticket.limit_basis == "prior_session_midpoint"
    assert ticket.worst_case is None
    assert "Prior-session pricing" in ticket.fill_model_note


def test_liquidity_gate_flags_unfillable_legs() -> None:
    """The 2026-09-04 IHI shape: OI 0 and 9, spread wider than the debit."""
    thin = leg_from_rh_quote(
        "buy", "call", _inst(55), _quote(1.0, 2.0, 1.5, oi=9, vol=0),
        snapshot_at="t",
    )
    hits = liquidity_violations(thin, {})
    assert any("open interest" in h for h in hits)
    assert any("spread" in h for h in hits)
    healthy = leg_from_rh_quote(
        "buy", "call", _inst(100), _quote(4.0, 4.2, 4.1, oi=5000, vol=200),
        snapshot_at="t",
    )
    assert liquidity_violations(healthy, {}) == []


def test_implied_move_from_straddle() -> None:
    assert implied_move_from_straddle(6.0, 5.0, 100.0) == 0.11
    assert implied_move_from_straddle(None, 5.0, 100.0) is None


FORBIDDEN_WORDS = ("rich", "thinning", "persistent", "exhausted")


def test_narrative_never_asserts_unbacked_adjectives() -> None:
    """P1-4: a play with null breadth, null valuation and no ticket must not
    claim rich / thinning / persistent / exhausted anywhere in its strategy."""
    play = {
        "fund": "COPX",
        "classification": "Extended",
        "direction": "bearish",
        "intended_structure": "long put debit spread",
        "skipped_reason": "no priceable structure this run",
        "breadth": {"pct_above_200d": None, "coverage": 0.3},
        "iv_rank": {"iv_rank": None, "regime": "collecting"},
        "_fund_row_extremes": {"rs_pctile": 96.0, "price_pctile": 97.0,
                               "ret_3m": -0.02, "ret_12m": 0.5,
                               "above_sma50": False, "above_sma200": True,
                               "weekly_rsi": None, "window_label": "2.0y"},
        "_fund_row_continuation": {"score": None, "accel": -0.2},
        "_fund_row_valuation": {"leader_median_pe": None, "universe_median_pe": None},
        "_dte_band": "150 to 240",
    }
    text = _strategy_beats(play).lower()
    for word in FORBIDDEN_WORDS:
        assert word not in text, f"unbacked adjective {word!r} in: {text[:300]}"
    assert "not yet computable" in text          # nulls say so, plainly
    assert "no contract priced this run" in text


def test_narrative_backs_valuation_claims_with_numbers() -> None:
    play = {
        "fund": "GDX",
        "classification": "Extended",
        "direction": "bearish",
        "intended_structure": "long put debit spread",
        "breadth": {"pct_above_200d": 0.35},
        "iv_rank": {"iv_rank": None, "regime": "collecting"},
        "_fund_row_extremes": {"rs_pctile": 95.0, "price_pctile": 96.0,
                               "ret_3m": -0.03, "ret_12m": 0.5,
                               "above_sma50": False, "above_sma200": True,
                               "weekly_rsi": 55.0, "window_label": "2.0y"},
        "_fund_row_continuation": {"score": 2, "accel": -0.3},
        "_fund_row_valuation": {"leader_median_pe": 31.0, "universe_median_pe": 22.0},
        "_dte_band": "150 to 240",
        "skipped_reason": None,
    }
    text = _strategy_beats(play)
    assert "31.0" in text and "22.0" in text     # the claim carries its numbers
    assert "2.0y" in text                        # percentiles labeled with the window


def test_manifest_batches_respect_tool_limits() -> None:
    config = {
        "universe": {"sectors": ["XLE"], "industries": ["SMH"]},
        "seed_leaders": {"XLE": [f"T{i}" for i in range(8)],
                         "SMH": [f"S{i}" for i in range(8)]},
        "structures": {"target_dte": 180, "dte_min": 150, "dte_max": 240},
    }
    manifest = build_manifest(config)
    for batch in manifest["requests"]["fundamentals"]["batches"]:
        assert len(batch) <= 10
    for batch in manifest["requests"]["constituent_weekly_closes"]["batches"]:
        assert len(batch) <= 10
    assert "20" in manifest["requests"]["instruments_and_quotes"]["note"]
