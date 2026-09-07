"""The Sector Scout analyzer: universe -> lenses -> factors -> selection ->
structures -> ONE computed run table.

REWORKED 2026-09-07 for the Robinhood wiring and the 2026-09-05 defect list:

* SOURCE AUTHORITY -- Massive owns bars, reference, short interest, news;
  Robinhood (via the agent-filled snapshot, robinhood_source.py) owns
  fundamentals, gate indicators, option chains/instruments/quotes, and
  earnings. No figure blends the two; every table states its source.
* P0-2/P0-3 -- leader fundamentals (P/E, P/B, market cap, 52w range) come
  from the snapshot's Robinhood rows, all 8 seeds in one batched request;
  unresolved seeds are dropped AND NAMED. The broken Massive P/E computation
  is deleted, not repaired. Without a fresh snapshot, valuation reads n/a --
  never a wrong number.
* P1-1 -- the continuation gates are a HARD FILTER: a fund failing any gate
  has its score voided and cannot be a long candidate. Gate results print on
  the board.
* P1-3 -- Extended yields a bearish structure only when acceleration is
  negative AND price has lost the 50 day; otherwise it is a WATCH entry with
  the trigger level stated.
* P0-1 -- structures price from Robinhood option quotes with the Options
  Scout null-greeks fallback (strike-nearest) and prior-session pricing on
  settled marks, labeled. The send gate lives in the runner.
* 2-YEAR WINDOW -- percentiles are computed on WEEKLY bars (~104 obs) and
  labeled 2-year everywhere.

The run table is the single source of truth: HTML and DOCX render from it,
it persists for the change log, and settlement records come from it.

ANALYSIS ONLY. Nothing in this package places, previews, reviews, or cancels
an order; the broker-guard AST test enforces it.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ..options_scout.indicators import atr as atr_series
from .base_rate import replay_base_rate
from .data import FundHistory, SectorDataClient
from .factors import (
    breadth_read,
    collapse_correlated,
    correlation_matrix,
    iv_rank_read,
    rate_beta,
    seasonality_note,
    short_interest_context,
    skew_note,
    term_structure_note,
)
from .lenses import (
    COILED,
    EXTENDED,
    FALLING_KNIFE,
    LEADING,
    classify,
    continuation_read,
    extremes_read,
)
from .opportunity import (
    day_levels,
    opportunity_read,
    price_action_plan,
    realized_vol_percentile,
)
from .robinhood_source import RhSnapshot
from .state import StateStore
from .structures import (
    Leg,
    build_credit_spread_ticket,
    build_debit_spread_ticket,
    expected_move_6m,
    implied_move_from_straddle,
    leg_from_rh_quote,
    measured_move_strike,
    pick_monthly_expiry,
)


def forward_window(run_date: date, months: int = 6) -> tuple[date, date]:
    """The option window: run date FORWARD `months` months (~182 days for 6).
    The first prototype computed this backwards; test_forward_window pins it."""
    return run_date, run_date + timedelta(days=round(months * 30.44))


def _median(values: list[float]) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


# --- leaders (Robinhood fundamentals; Massive never computes a ratio here) ----


def confirm_leaders(
    seeds: list[str],
    constituent_series: dict[str, list[tuple[date, float]]],
    rh: RhSnapshot | None,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Resolve the 8 seed leaders. Price/52w context comes from the breadth
    cache; P/E, P/B, market cap, 52w range and the one-line role come from
    the ROBINHOOD fundamentals rows in the snapshot (P0-2). Returns
    (leaders, dropped_from_cache, unresolved_fundamentals) -- both failure
    lists are NAMED in the report, never silent n/a (P0-3)."""
    leaders: list[dict[str, Any]] = []
    dropped: list[str] = []
    unresolved: list[str] = []
    rh_not_found = {s.upper() for s in (rh.fundamentals_not_found if rh else [])}

    for ticker in seeds:
        sym = ticker.upper()
        pts = constituent_series.get(sym)
        cache_last = pts[-1][1] if pts else None

        row = rh.fundamentals.get(sym) if rh else None
        if rh is not None and row is None:
            unresolved.append(sym)
            if sym in rh_not_found:
                dropped.append(sym)
                continue
        if row is None and cache_last is None:
            dropped.append(sym)
            continue

        def _f(key: str) -> float | None:
            if not row:
                return None
            raw = row.get(key)
            try:
                return float(raw) if raw is not None else None
            except (TypeError, ValueError):
                return None

        hi52, lo52 = _f("high_52_weeks"), _f("low_52_weeks")
        last = cache_last
        pos = None
        if last is not None and hi52 and lo52 and hi52 > lo52:
            pos = round((last - lo52) / (hi52 - lo52), 3)
        elif pts and len(pts) >= 30:
            closes = [c for _, c in pts]
            yr = closes[-252:] if len(closes) >= 252 else closes
            hi, lo = max(yr), min(yr)
            pos = round((closes[-1] - lo) / (hi - lo), 3) if hi > lo else None

        leaders.append(
            {
                "ticker": sym,
                "last": round(last, 2) if last is not None else None,
                "pos_52w": pos,
                "market_cap": _f("market_cap"),
                "pe": round(_f("pe_ratio"), 2) if _f("pe_ratio") is not None else None,
                "pb": round(_f("pb_ratio"), 2) if _f("pb_ratio") is not None else None,
                "role": (row or {}).get("industry"),
                "ex_dividend_date": (row or {}).get("ex_dividend_date"),
                "fundamentals_source": "robinhood" if row else None,
            }
        )

    leaders.sort(key=lambda l: (l.get("market_cap") or 0.0), reverse=True)
    return leaders, dropped, unresolved


# --- option-chain reads: Robinhood snapshot first, Massive fallback -----------


def gate_inputs_label(
    rh_sma50: float | None, rh_sma200: float | None, rh_weekly_rsi: float | None
) -> str:
    """"robinhood" only when the snapshot ACTUALLY carried a gate indicator --
    a fresh snapshot with no indicators means the gates computed locally, and
    the label must say so (the P1-4 rule applied to metadata)."""
    return (
        "robinhood"
        if any(v is not None for v in (rh_sma50, rh_sma200, rh_weekly_rsi))
        else "local"
    )


def _rh_pick_expiry(rh: RhSnapshot, fund: str, today: date, cfg: dict[str, Any]) -> str | None:
    chain = rh.chains.get(fund.upper()) or {}
    expirations = [str(e) for e in chain.get("expiration_dates") or []]
    if not expirations:
        return None
    return pick_monthly_expiry(expirations, today, cfg)


def _rh_legs_for(
    rh: RhSnapshot, fund: str, option_type: str, expiry: str,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(instrument, quote) pairs for one fund/type/expiry from the snapshot."""
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for inst in rh.instruments.get(fund.upper()) or []:
        if str(inst.get("type") or "").lower() != option_type:
            continue
        if str(inst.get("expiration_date") or "") != expiry:
            continue
        quote = rh.option_quotes.get(str(inst.get("id")))
        if quote:
            out.append((inst, quote))
    return out


def _rh_leg_nearest_delta(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    action: str, option_type: str, spot: float, target_delta: float,
    snapshot_at: str,
) -> Leg | None:
    """Delta-target selection over snapshot quotes; when greeks are dark the
    Options Scout fallback applies: strike nearest the reference level. Never
    aborts on null greeks alone (P0-1)."""
    legs = [
        leg
        for inst, quote in pairs
        if (leg := leg_from_rh_quote(action, option_type, inst, quote, snapshot_at=snapshot_at))
        is not None
    ]
    if not legs:
        return None
    with_delta = [l for l in legs if l.delta is not None]
    if with_delta:
        return min(with_delta, key=lambda l: abs(abs(l.delta) - target_delta))
    return min(legs, key=lambda l: abs(l.strike - spot))


def _rh_leg_nearest_strike(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    action: str, option_type: str, level: float, snapshot_at: str,
) -> Leg | None:
    legs = [
        leg
        for inst, quote in pairs
        if (leg := leg_from_rh_quote(action, option_type, inst, quote, snapshot_at=snapshot_at))
        is not None
    ]
    if not legs:
        return None
    return min(legs, key=lambda l: abs(l.strike - level))


def _rh_atm_iv_and_straddle(
    rh: RhSnapshot, fund: str, expiry: str, spot: float, snapshot_at: str,
) -> tuple[float | None, float | None]:
    """(ATM IV, implied move fraction) from the snapshot's target-expiry
    quotes: nearest-to-spot call IV, and the ATM call+put straddle marks."""
    calls = _rh_legs_for(rh, fund, "call", expiry)
    puts = _rh_legs_for(rh, fund, "put", expiry)
    atm_call = _rh_leg_nearest_strike(calls, "buy", "call", spot, snapshot_at)
    atm_put = _rh_leg_nearest_strike(puts, "buy", "put", spot, snapshot_at)
    iv = atm_call.iv if atm_call is not None else None
    implied = implied_move_from_straddle(
        atm_call.mark if atm_call else None,
        atm_put.mark if atm_put else None,
        spot,
    )
    return iv, implied


# --- Massive fallback chain reads (kept verbatim from the pre-wiring build) ---


def _snapshot_leg(
    client: SectorDataClient, fund: str, action: str, option_type: str,
    contract: Any, now_iso: str,
) -> Leg | None:
    try:
        snap = client.get_option_snapshot(fund, contract.ticker)
    except Exception:
        return None
    if snap is None:
        return None
    return Leg(
        action=action,
        option_type=option_type,
        ticker=contract.ticker,
        strike=contract.strike_price,
        expiry=contract.expiration_date,
        bid=snap.bid,
        ask=snap.ask,
        mark=snap.premium,
        delta=snap.delta,
        iv=snap.implied_volatility,
        open_interest=snap.open_interest,
        snapshot_at=now_iso,
        theta=snap.theta,
        vega=snap.vega,
        volume=snap.day_volume,
        pricing_basis="live" if snap.bid is not None else "prior_session_close",
        source="massive",
    )


def atm_iv(client: SectorDataClient, fund: str, spot: float, today: date,
           *, dte_lo: int, dte_hi: int, band_lo: float = 0.85,
           band_hi: float = 1.15) -> float | None:
    """ATM IV via Massive (fallback collector when no snapshot is present)."""
    try:
        contracts = client.get_option_contracts(
            fund,
            contract_type="call",
            expiration_gte=(today + timedelta(days=dte_lo)).isoformat(),
            expiration_lte=(today + timedelta(days=dte_hi)).isoformat(),
            strike_gte=spot * band_lo,
            strike_lte=spot * band_hi,
            limit=250,
        )
    except Exception:
        return None
    if not contracts:
        return None
    nearest_exp = min(c.expiration_date for c in contracts)
    ring = sorted(
        (c for c in contracts if c.expiration_date == nearest_exp),
        key=lambda c: abs(c.strike_price - spot),
    )
    for contract in ring[:2]:
        try:
            snap = client.get_option_snapshot(fund, contract.ticker)
        except Exception:
            continue
        if snap is not None and snap.implied_volatility is not None:
            return snap.implied_volatility
    return None


def chain_for_structure(
    client: SectorDataClient, fund: str, option_type: str, spot: float, today: date,
    cfg: dict[str, Any],
) -> tuple[str | None, list[Any]]:
    """(chosen expiry, Massive reference contracts) inside the DTE band."""
    st = cfg.get("structures", {}) or {}
    lo, hi = int(st.get("dte_min", 150)), int(st.get("dte_max", 240))
    band = st.get("chain_strike_band") or [0.6, 1.6]
    try:
        contracts = client.get_option_contracts(
            fund,
            contract_type=option_type,
            expiration_gte=(today + timedelta(days=lo)).isoformat(),
            expiration_lte=(today + timedelta(days=hi)).isoformat(),
            strike_gte=spot * float(band[0]),
            strike_lte=spot * float(band[1]),
            limit=250,
        )
    except Exception:
        return None, []
    if not contracts:
        return None, []
    expiries = sorted({c.expiration_date for c in contracts})
    chosen = pick_monthly_expiry(expiries, today, cfg)
    if chosen is None:
        return None, []
    return chosen, [c for c in contracts if c.expiration_date == chosen]


def leg_nearest_delta(
    client: SectorDataClient, fund: str, action: str, option_type: str,
    contracts: list[Any], spot: float, target_delta: float, now_iso: str,
    *, scan_max: int,
) -> Leg | None:
    ring = sorted(contracts, key=lambda c: abs(c.strike_price - spot))[:scan_max]
    legs = [
        leg for c in ring
        if (leg := _snapshot_leg(client, fund, action, option_type, c, now_iso)) is not None
    ]
    if not legs:
        return None
    with_delta = [l for l in legs if l.delta is not None]
    if with_delta:
        return min(with_delta, key=lambda l: abs(abs(l.delta) - target_delta))
    return min(legs, key=lambda l: abs(l.strike - spot))


def leg_nearest_strike(
    client: SectorDataClient, fund: str, action: str, option_type: str,
    contracts: list[Any], level: float, now_iso: str, *, scan_max: int = 4,
) -> Leg | None:
    ring = sorted(contracts, key=lambda c: abs(c.strike_price - level))[:scan_max]
    for c in ring:
        leg = _snapshot_leg(client, fund, action, option_type, c, now_iso)
        if leg is not None and leg.mark is not None:
            return leg
    return None


# --- the run table -------------------------------------------------------------------


def build_run_table(
    client: SectorDataClient,
    config: dict[str, Any],
    store: StateStore,
    *,
    today: date | None = None,
    now: datetime | None = None,
    rh: RhSnapshot | None = None,
) -> dict[str, Any]:
    """Analyze the whole universe once; return the computed table (plain
    JSON-serializable dicts throughout)."""
    today = today or datetime.now(UTC).date()
    now = now or datetime.now(UTC)
    now_iso = now.isoformat()

    uni = config.get("universe", {}) or {}
    sectors: list[str] = list(uni.get("sectors") or [])
    industries: list[str] = list(uni.get("industries") or [])
    funds = sectors + industries
    seeds_map: dict[str, list[str]] = config.get("seed_leaders", {}) or {}
    benchmark = str(config.get("benchmark", "SPY"))
    rate_proxy = str(config.get("rate_proxy", "TLT"))
    years = int((config.get("history") or {}).get("years", 5))
    f_cfg = config.get("factors", {}) or {}
    rh_cfg = config.get("robinhood", {}) or {}
    notes: list[str] = []

    win_start, win_end = forward_window(today)
    sel0 = config.get("selection", {}) or {}

    # --- Robinhood snapshot status (source authority: fundamentals, gates,
    # chains, quotes). Stale snapshots degrade to absent, with the age said.
    rh_fresh: RhSnapshot | None = None
    rh_status = "absent"
    if rh is not None:
        max_age = float(rh_cfg.get("snapshot_max_age_hours", 20))
        if rh.is_fresh(max_age, now):
            rh_fresh = rh
            rh_status = f"fresh ({rh.age_label(now)})"
        else:
            rh_status = f"stale ({rh.age_label(now)}); Robinhood fields read n/a"
            notes.append(
                f"Robinhood snapshot is {rh.age_label(now)} (cap {max_age:.0f}h): "
                "fundamentals, gate overrides and option quotes fall back to n/a "
                "rather than presenting stale numbers as live."
            )
    if rh_fresh is None and rh_status == "absent":
        notes.append(
            "No Robinhood snapshot this run: fundamentals and valuation read n/a "
            "(never the broken vendor ratio), gates use local indicators, and "
            "structures fall back to the Massive options path."
        )

    # --- histories (throttled stock-side, Massive) -----------------------------
    spy = client.get_fund_history(benchmark, years, today)
    tlt = client.get_fund_history(rate_proxy, years, today)
    if spy is None:
        raise RuntimeError("benchmark history unavailable -- cannot analyze")
    histories: dict[str, FundHistory] = {}
    for fund in funds:
        hist = client.get_fund_history(fund, years, today)
        if hist is None:
            notes.append(f"{fund}: history unavailable this run; excluded")
            continue
        histories[fund] = hist

    if spy.window_label != f"{years:.0f}.0y":
        notes.append(
            f"History window is {spy.window_label} (plan entitlement cap), not the "
            f"{years}y requested. Every percentile is a {spy.window_label} percentile "
            "computed on WEEKLY bars and labeled with that window; the window excludes "
            "the 2022 drawdown entirely, a known limitation of the plan."
        )

    # --- breadth cache ----------------------------------------------------------
    br_cfg = f_cfg.get("breadth", {}) or {}
    lookback = int(br_cfg.get("lookback_days", 260))
    all_constituents = {t for seeds in seeds_map.values() for t in seeds}
    from .data import BreadthCache

    store.sync_breadth_down()
    cache = BreadthCache(store.breadth_dir, all_constituents)
    fetched = cache.backfill(
        client, today, lookback, int(br_cfg.get("backfill_days_per_run", 40))
    )
    if fetched:
        store.sync_breadth_up()
    coverage = cache.coverage(today, lookback)
    if coverage < 0.999:
        notes.append(
            f"Breadth cache at {coverage * 100:.0f}% coverage (backfilled {fetched} "
            "days this run); constituent breadth reads carry partial-coverage labels until full."
        )
    constituent_series = cache.constituent_series(today, lookback)
    # Snapshot weekly closes accelerate breadth when present (source: Robinhood).
    if rh_fresh is not None and rh_fresh.constituent_weekly_closes:
        for sym, closes in rh_fresh.constituent_weekly_closes.items():
            if sym not in constituent_series or len(constituent_series[sym]) < 200:
                constituent_series[sym] = [
                    (today, c) for c in closes  # date granularity unused by breadth math
                ]
        notes.append(
            "Breadth constituents supplemented from the Robinhood snapshot's weekly bars."
        )

    # --- IV history (self-collected) ---------------------------------------------
    iv_cfg = f_cfg.get("iv_rank", {}) or {}
    st_cfg = config.get("structures", {}) or {}
    iv_history_all = store.load_iv_history()
    atm_band = iv_cfg.get("atm_strike_band") or [0.85, 1.15]
    atm_now: dict[str, float] = {}
    if rh_fresh is None:
        for fund, hist in histories.items():
            iv = atm_iv(
                client, fund, hist.closes_daily[-1], today,
                dte_lo=int(iv_cfg.get("atm_dte_lo", 20)),
                dte_hi=int(iv_cfg.get("atm_dte_hi", 45)),
                band_lo=float(atm_band[0]), band_hi=float(atm_band[1]),
            )
            if iv is not None:
                atm_now[fund] = iv
    # (When a snapshot is present, ATM IV is recorded per selected fund below,
    # from the Robinhood target-expiry quotes.)

    # --- per-fund rows ---------------------------------------------------------------
    fund_rows: list[dict[str, Any]] = []
    leaders_by_fund: dict[str, list[dict[str, Any]]] = {}
    dropped_by_fund: dict[str, list[str]] = {}
    unresolved_by_fund: dict[str, list[str]] = {}
    pe_by_fund: dict[str, float] = {}

    for fund, hist in histories.items():
        ext = extremes_read(
            closes_daily=hist.closes_daily,
            highs_daily=hist.highs_daily,
            lows_daily=hist.lows_daily,
            closes_weekly_pct=hist.closes_weekly,
            spy_closes_weekly_pct=spy.closes_weekly,
            weekly_closes=hist.closes_weekly,
            window_label=hist.window_label,
            cfg=config,
        )
        if ext is None:
            notes.append(f"{fund}: insufficient history for the lenses; excluded")
            continue

        rh_sma50 = rh_fresh.indicator(fund, "sma_50") if rh_fresh else None
        rh_sma200 = rh_fresh.indicator(fund, "sma_200") if rh_fresh else None
        rh_weekly_rsi = rh_fresh.indicator(fund, "rsi_14_weekly") if rh_fresh else None
        cont = continuation_read(
            closes_daily=hist.closes_daily,
            spy_closes_daily=spy.closes_daily,
            weekly_closes=hist.closes_weekly,
            fund_pe=None,                # valuation lands after leaders resolve
            universe_median_pe=None,
            leader_earnings_improving=None,
            cfg=config,
            rh_sma50=rh_sma50,
            rh_sma200=rh_sma200,
            rh_weekly_rsi=rh_weekly_rsi,
        )

        ivr = iv_rank_read(
            iv_history_all.get(fund, {}),
            atm_now.get(fund),
            window_days=int(iv_cfg.get("window_days", 252)),
            min_days=int(iv_cfg.get("min_days", 60)),
            buy_max=float(st_cfg.get("iv_rank_buy_premium_max", 30)),
            sell_min=float(st_cfg.get("iv_rank_sell_premium_min", 70)),
        )

        beta = rate_beta(
            hist.closes_daily, tlt.closes_daily if tlt else [],
            int((f_cfg.get("rate_beta") or {}).get("lookback_months", 12)) * 21,
        )

        fund_rows.append(
            {
                "symbol": fund,
                "layer": "sector" if fund in sectors else "industry",
                "last": round(hist.closes_daily[-1], 2),
                "window": hist.window_label,
                "percentile_basis": f"{hist.window_label} weekly-bar percentiles",
                "extremes": asdict(ext),
                "continuation": asdict(cont) if cont else None,
                "iv_rank": asdict(ivr),
                "realized_vol_pctile": realized_vol_percentile(hist.closes_daily),
                "rate_beta": beta,
                "classification": None,
                "gate_inputs_source": gate_inputs_label(rh_sma50, rh_sma200, rh_weekly_rsi),
            }
        )

    # --- leaders + valuation (Robinhood fundamentals; P0-2, P0-3) --------------------
    for row in fund_rows:
        fund = row["symbol"]
        leaders, dropped, unresolved = confirm_leaders(
            seeds_map.get(fund, []), constituent_series, rh_fresh
        )
        leaders_by_fund[fund] = leaders
        dropped_by_fund[fund] = dropped
        unresolved_by_fund[fund] = unresolved
        if unresolved:
            notes.append(
                f"{fund}: fundamentals unresolved for {', '.join(unresolved)} "
                "(named, not silent n/a)"
            )
        pes = [l["pe"] for l in leaders if l.get("pe")]
        if pes:
            pe_by_fund[fund] = _median(pes)

    universe_median_pe = _median(list(pe_by_fund.values()))

    # --- final classification + gate hard filter --------------------------------------
    from .lenses import ExtremesRead as _ER

    for row in fund_rows:
        fund = row["symbol"]
        hist = histories[fund]
        fund_pe = pe_by_fund.get(fund)
        rich = (fund_pe > universe_median_pe * 1.25) if (fund_pe and universe_median_pe) else None
        cheap = (fund_pe < universe_median_pe * 0.8) if (fund_pe and universe_median_pe) else None
        top2 = [l for l in leaders_by_fund.get(fund, [])[:2] if l.get("pe") is not None]
        earnings_growing = (bool(top2) and all(l["pe"] > 0 for l in top2)) or None

        ext = _ER(**{k: row["extremes"][k] for k in _ER.__dataclass_fields__})
        row["classification"] = classify(
            ext, valuation_rich=rich, valuation_cheap=cheap,
            earnings_growing=earnings_growing, cfg=config,
        )
        cont = continuation_read(
            closes_daily=hist.closes_daily,
            spy_closes_daily=spy.closes_daily,
            weekly_closes=hist.closes_weekly,
            fund_pe=fund_pe,
            universe_median_pe=universe_median_pe,
            leader_earnings_improving=earnings_growing,
            cfg=config,
            rh_sma50=rh_fresh.indicator(fund, "sma_50") if rh_fresh else None,
            rh_sma200=rh_fresh.indicator(fund, "sma_200") if rh_fresh else None,
            rh_weekly_rsi=rh_fresh.indicator(fund, "rsi_14_weekly") if rh_fresh else None,
        )
        cont_dict = asdict(cont) if cont else None
        # P1-1: THE GATES GATE. A fund failing any gate keeps its gate reads
        # (the board prints them) but its score is VOIDED -- it cannot appear
        # as a long candidate and the score column shows the failure.
        if cont_dict is not None and cont_dict["gates_passed"] < 3:
            cont_dict["score_voided_by_gates"] = True
            cont_dict["score"] = None
        row["continuation"] = cont_dict
        row["valuation"] = {
            "leader_median_pe": fund_pe,
            "universe_median_pe": universe_median_pe,
            "basis": (
                "median of confirmed leaders, Robinhood fundamentals"
                if rh_fresh is not None else "n/a (no fresh Robinhood snapshot)"
            ),
        }

        br = breadth_read(
            constituent_series,
            seeds_map.get(fund, []),
            ext.pos_52w,
            coverage=coverage,
            classification=row["classification"],
            demotion_pct=float(br_cfg.get("demotion_pct_above_200d", 40)),
            continuation_score=int((cont_dict or {}).get("score") or 0),
        )
        row["breadth"] = asdict(br)
        if br.demoted:
            row["breadth_demoted"] = True
            row["classification_before_breadth"] = row["classification"]
            if row["classification"] in (EXTENDED, LEADING):
                row["classification"] = "Mid range"
            notes.append(
                f"{fund}: demoted on breadth ({br.pct_above_200d * 100:.0f}% of "
                "constituents above their 200 day) -- a narrow move is a top forming; "
                "excluded from selection and said so here"
            )

    # --- opportunity score (direction-conditioned, renormalised) ----------------------
    for row in fund_rows:
        opp = opportunity_read(row, config)
        row["opportunity"] = {
            "score": opp.score,
            "raw_points": opp.raw_points,
            "live_max": opp.live_max,
            "components": opp.components,
            "direction": opp.direction,
            "no_trade_reason": opp.no_trade_reason,
            "breakdown": opp.breakdown(),
        }

    fund_rows.sort(key=lambda r: r["extremes"]["rs_pctile"])

    # --- selection: tradeable Top N by score, correlation-collapsed -------------------
    max_plays = int(sel0.get("max_plays", 9))
    candidates = [
        r for r in fund_rows
        if (r.get("opportunity") or {}).get("direction") is not None
        and not r.get("breadth_demoted")
    ]
    candidates.sort(key=lambda r: -(r["opportunity"]["score"]))

    corr_cfg = f_cfg.get("correlation", {}) or {}
    corr_lookback = int(corr_cfg.get("lookback_months", 12)) * 21
    closes_map = {r["symbol"]: histories[r["symbol"]].closes_daily for r in candidates}
    matrix = correlation_matrix(closes_map, corr_lookback)
    collapsed = collapse_correlated(
        [r["symbol"] for r in candidates], matrix,
        float(corr_cfg.get("single_position_threshold", 0.80)),
    )
    # P1-5: suppressed funds are MARKED on the board, not silently absent.
    for row in fund_rows:
        if row["symbol"] in collapsed:
            row["suppressed_by"] = collapsed[row["symbol"]]
    primaries = [r for r in candidates if r["symbol"] not in collapsed][:max_plays]

    rb_cfg = f_cfg.get("rate_beta", {}) or {}
    rate_sensitive = [
        r["symbol"] for r in primaries
        if r.get("rate_beta") is not None
        and abs(r["rate_beta"]) > float(rb_cfg.get("shared_exposure_threshold", 0.5))
    ]
    if len(rate_sensitive) >= int(rb_cfg.get("shared_exposure_min_funds", 3)):
        notes.append(
            "One macro exposure, not separate ideas: "
            + ", ".join(rate_sensitive)
            + " all carry rate beta above the threshold -- they are one duration trade."
        )

    # Watch entries (P1-3): Extended funds whose short has not triggered, plus
    # falling knives. Reported with the trigger, never given a structure and
    # never consuming a Top-9 slot.
    watch_rows = [
        r for r in fund_rows
        if (r.get("opportunity") or {}).get("direction") is None
        and r["classification"] in (EXTENDED, FALLING_KNIFE)
    ]

    # --- structures per selected play ---------------------------------------------------
    prob_cfg = config.get("probability", {}) or {}
    rate = float(prob_cfg.get("risk_free_rate", 0.04))
    base_cfg = config.get("base_rate", {}) or {}
    si_cfg = f_cfg.get("short_interest", {}) or {}
    plays: list[dict[str, Any]] = []
    si_budget = int(si_cfg.get("max_funds", 10))
    structures_priced = 0
    min_ev = float(sel0.get("min_expected_value", 0.0))
    min_prob = float(sel0.get("min_prob_profit", 0.0))

    for row in primaries:
        fund = row["symbol"]
        hist = histories[fund]
        spot = hist.closes_daily[-1]
        cls = row["classification"]
        ivr = row["iv_rank"]
        cont = row.get("continuation") or {}
        opp = row.get("opportunity") or {}
        direction = opp.get("direction")
        bullish = direction == "bullish"

        sell_premium_regime = (ivr.get("regime") or "") == "sell_premium"
        if sell_premium_regime:
            intended = "short put credit spread" if bullish else "short call credit spread"
        else:
            intended = "long call debit spread" if bullish else "long put debit spread"

        atr_arr = atr_series(hist.highs_daily, hist.lows_daily, hist.closes_daily, 14)
        atr_val = atr_arr[-1] if atr_arr and atr_arr[-1] else None
        levels = day_levels(spot, atr_val, direction, config) if atr_val else None

        play: dict[str, Any] = {
            "fund": fund,
            "classification": cls,
            "direction": direction,
            "spot": round(spot, 2),
            "iv_rank": ivr,
            "realized_vol_pctile": row.get("realized_vol_pctile"),
            "breadth": row.get("breadth"),
            "rate_beta": row.get("rate_beta"),
            "leaders": leaders_by_fund.get(fund, [])[:8],
            "dropped_seeds": dropped_by_fund.get(fund, []),
            "unresolved_fundamentals": unresolved_by_fund.get(fund, []),
            "correlated_with": [s for s, p in collapsed.items() if p == fund],
            "opportunity": opp,
            "intended_structure": intended,
            "day_levels": (
                {
                    "reference_close": levels.reference_close,
                    "atr": levels.atr,
                    "ideal_entry": levels.ideal_entry,
                    "day_floor": levels.day_floor,
                    "day_ceiling": levels.day_ceiling,
                }
                if levels else None
            ),
            "price_action": (
                price_action_plan(fund, cls, direction, levels) if levels else None
            ),
        }

        if si_cfg.get("enabled", True) and si_budget > 0:
            play["short_interest"] = short_interest_context(
                client.get_short_interest(fund, int(si_cfg.get("readings", 3)))
            )
            si_budget -= 1

        option_type = "call" if bullish else "put"
        dte_band = f"{int(st_cfg.get('dte_min', 150))}-{int(st_cfg.get('dte_max', 240))}"
        scan_max = int(st_cfg.get("delta_scan_max_contracts", 12))
        em6 = expected_move_6m(
            hist.closes_daily, int(st_cfg.get("expected_move_lookback_days", 63))
        )

        built = None
        implied_move = None
        pricing_source = None

        # --- Robinhood pricing path (source of authority when present) -------
        rh_expiry = _rh_pick_expiry(rh_fresh, fund, today, config) if rh_fresh else None
        if rh_fresh is not None and rh_expiry:
            pricing_source = "robinhood"
            rh_iv, implied_move = _rh_atm_iv_and_straddle(
                rh_fresh, fund, rh_expiry, spot, now_iso
            )
            if rh_iv is not None:
                atm_now[fund] = rh_iv
            pairs = _rh_legs_for(rh_fresh, fund, option_type, rh_expiry)
            if sell_premium_regime:
                credit_type = "put" if bullish else "call"
                credit_pairs = _rh_legs_for(rh_fresh, fund, credit_type, rh_expiry)
                short_leg = _rh_leg_nearest_delta(
                    credit_pairs, "sell", credit_type, spot,
                    float(st_cfg.get("credit_short_delta", 0.30)), now_iso,
                )
                if short_leg is not None:
                    width = max(
                        spot * float(st_cfg.get("credit_width_pct", 0.05)),
                        float(st_cfg.get("credit_width_min", 5.0)),
                    )
                    wing = short_leg.strike - width if bullish else short_leg.strike + width
                    long_leg = _rh_leg_nearest_strike(
                        credit_pairs, "buy", credit_type, wing, now_iso
                    )
                    if long_leg is not None and long_leg.strike != short_leg.strike:
                        built = build_credit_spread_ticket(
                            direction=direction, short_leg=short_leg, long_leg=long_leg,
                            spot=spot, today=today, cfg=config, rate=rate,
                        )
            if built is None and pairs:
                target_delta = (
                    float(st_cfg.get("continuation_long_delta", 0.60))
                    if cls != COILED and bullish
                    else float(st_cfg.get("coiled_long_delta", 0.35))
                )
                long_leg = _rh_leg_nearest_delta(
                    pairs, "buy", option_type, spot, target_delta, now_iso
                )
                if long_leg is not None:
                    if bullish:
                        anchor = measured_move_strike(spot, em6) if em6 is not None else 0.0
                        short_level = max(anchor, long_leg.strike + max(spot * 0.03, 1.0))
                    else:
                        anchor = spot * (1.0 - em6) if em6 is not None else float("inf")
                        short_level = min(anchor, long_leg.strike - max(spot * 0.03, 1.0))
                    short_leg = _rh_leg_nearest_strike(
                        pairs, "sell", option_type, short_level, now_iso
                    )
                    if short_leg is not None and short_leg.strike != long_leg.strike:
                        built = build_debit_spread_ticket(
                            direction=direction, long_leg=long_leg, short_leg=short_leg,
                            spot=spot, today=today, cfg=config, rate=rate,
                            implied_move_pct=implied_move,
                        )

        # --- Massive fallback path -------------------------------------------
        if built is None and pricing_source != "robinhood":
            pricing_source = "massive"
            expiry, contracts = chain_for_structure(client, fund, option_type, spot, today, config)
            if contracts:
                target_delta = (
                    float(st_cfg.get("continuation_long_delta", 0.60))
                    if cls != COILED and bullish
                    else float(st_cfg.get("coiled_long_delta", 0.35))
                )
                long_leg = leg_nearest_delta(
                    client, fund, "buy", option_type, contracts, spot, target_delta,
                    now_iso, scan_max=scan_max,
                )
                if long_leg is not None:
                    if bullish:
                        anchor = measured_move_strike(spot, em6) if em6 is not None else 0.0
                        short_level = max(anchor, long_leg.strike + max(spot * 0.03, 1.0))
                    else:
                        anchor = spot * (1.0 - em6) if em6 is not None else float("inf")
                        short_level = min(anchor, long_leg.strike - max(spot * 0.03, 1.0))
                    short_leg = leg_nearest_strike(
                        client, fund, "sell", option_type, contracts, short_level, now_iso,
                    )
                    if short_leg is not None and short_leg.strike != long_leg.strike:
                        built = build_debit_spread_ticket(
                            direction=direction, long_leg=long_leg, short_leg=short_leg,
                            spot=spot, today=today, cfg=config, rate=rate,
                        )

        if built is None:
            play["structure"] = None
            play["skipped_reason"] = (
                f"no priceable {intended} in the {dte_band} DTE band this run "
                f"(pricing source: {pricing_source or 'none'})"
            )
            plays.append(play)
            continue

        ticket, prob = built
        structures_priced += 1
        legs = ticket.legs

        sma200 = None
        if len(hist.closes_daily) >= 200:
            sma200 = sum(hist.closes_daily[-200:]) / 200.0
        dd_frac = 1.0 - float(st_cfg.get("falsifier_drawdown_fraction", 0.10))
        if bullish:
            falsifier_level = round(min(spot * dd_frac, sma200 if sma200 else spot * dd_frac), 2)
            win_level = max(l.strike for l in legs)
            falsifier_text = (
                f"a daily close below {falsifier_level:g} (the 200 day average / "
                f"{(1 - dd_frac) * 100:.0f}% drawdown zone): the trend structure this "
                "play rests on is gone -- exit"
            )
        else:
            falsifier_level = round(max(hist.highs_daily[-63:]), 2)
            win_level = min(l.strike for l in legs)
            falsifier_text = (
                f"a daily close above {falsifier_level:g} (the 3 month high): the "
                "exhaustion thesis is wrong -- exit"
            )

        move_req = abs(ticket.move_required_pct)
        br_rate = replay_base_rate(
            closes_daily=hist.closes_daily,
            highs_daily=hist.highs_daily,
            lows_daily=hist.lows_daily,
            closes_weekly=hist.closes_weekly,
            spy_closes_weekly=spy.closes_weekly,
            weekly_indices=hist.weekly_indices,
            target_classification=cls,
            bullish=bullish,
            required_move_pct=move_req,
            forward_months=int(base_cfg.get("forward_months", 6)),
            min_occurrences=int(base_cfg.get("min_occurrences", 10)),
            window_label=hist.window_label,
            cfg=config,
        )

        ts_cfg = f_cfg.get("term_structure_skew", {}) or {}
        front = atm_now.get(fund)
        target_iv_vals = [l.iv for l in legs if l.iv is not None]
        target_iv = target_iv_vals[0] if target_iv_vals else None
        play["term_structure"] = term_structure_note(
            front, target_iv, cheap_rich_pts=float(ts_cfg.get("cheap_rich_pts", 2.0))
        )
        play["skew"] = skew_note(
            None, None, cls,
            heavy_put_skew_pts=float(ts_cfg.get("heavy_put_skew_pts", 4.0)),
        )

        months_labels: list[str] = []
        for d in hist.dates_daily:
            lab = f"{d.year:04d}-{d.month:02d}"
            if not months_labels or months_labels[-1] != lab:
                months_labels.append(lab)
        play["seasonality"] = seasonality_note(
            hist.closes_monthly, months_labels[-len(hist.closes_monthly):], today.month,
            int(base_cfg.get("forward_months", 6)),
        )

        bs_prob = round(prob.prob_profit, 4)
        emp_prob = round(br_rate.rate, 4) if br_rate.occurrences else None
        divergence = abs(bs_prob - emp_prob) * 100.0 if emp_prob is not None else None

        # P2-6: the EV / probability floor. A play failing it stays reported
        # but is NOT listed in the Top 9 -- the board can come back short.
        passes_floor = (
            (prob.expected_value is not None and prob.expected_value >= min_ev)
            and bs_prob >= min_prob
        )

        play.update(
            {
                "structure": ticket.structure,
                "pricing_source": pricing_source,
                "pricing_basis": ticket.pricing_basis,
                "ticket": {
                    **asdict(ticket),
                    "legs": [
                        {**asdict(l), "spread_pct_of_mark": l.spread_pct_of_mark}
                        for l in legs
                    ],
                },
                "win_level": win_level,
                "falsifier_level": falsifier_level,
                "falsifier": falsifier_text,
                "prob_profit_bs": bs_prob,
                "prob_max_gain_bs": round(prob.prob_max_gain, 4),
                "prob_profit_broker_long_leg": ticket.broker_chance_of_profit_long,
                "prob_profit_empirical": emp_prob,
                "base_rate": {
                    "summary": br_rate.summary(),
                    "occurrences": br_rate.occurrences,
                    "hits": br_rate.hits,
                    "low_confidence": br_rate.low_confidence,
                    "confidence_weight": round(br_rate.confidence_weight, 3),
                },
                "divergence_points": round(divergence, 1) if divergence is not None else None,
                "divergence_flagged": (
                    divergence is not None
                    and divergence > float(prob_cfg.get("divergence_flag_points", 15))
                ),
                "expected_value": prob.expected_value,
                "reward_to_risk": prob.reward_to_risk,
                "passes_floor": passes_floor,
                "factor_values": {
                    "breadth_pct_above_200d": (row.get("breadth") or {}).get("pct_above_200d"),
                    "iv_rank": ivr.get("iv_rank"),
                    "realized_vol_pctile": row.get("realized_vol_pctile"),
                    "rate_beta": row.get("rate_beta"),
                    "continuation_score": cont.get("score"),
                    "rs_pctile": row["extremes"]["rs_pctile"],
                },
            }
        )
        plays.append(play)

    # Persist today's ATM IV observations (Robinhood target-expiry when
    # snapshotted, Massive front-month otherwise -- the source is recorded).
    if atm_now:
        store.record_iv(today, atm_now)

    plays.sort(
        key=lambda p: (
            (p.get("opportunity") or {}).get("score") or 0.0,
            p.get("expected_value") or 0.0,
        ),
        reverse=True,
    )

    # Watch entries render after the plays: no structure, trigger stated.
    watch: list[dict[str, Any]] = []
    for row in watch_rows:
        fund = row["symbol"]
        hist = histories[fund]
        cont = row.get("continuation") or {}
        sma50_val = None
        if len(hist.closes_daily) >= 50:
            sma50_val = round(sum(hist.closes_daily[-50:]) / 50.0, 2)
        opp = row.get("opportunity") or {}
        entry = {
            "fund": fund,
            "classification": row["classification"],
            "spot": row["last"],
            "reason": opp.get("no_trade_reason"),
            "opportunity": opp,
        }
        if row["classification"] == EXTENDED:
            entry["trigger"] = (
                f"the short triggers below {sma50_val:g} (the 50 day average) with "
                "acceleration negative"
                if sma50_val is not None
                else "the short triggers on a close below the 50 day average with acceleration negative"
            )
            entry["trigger_level"] = sma50_val
        else:  # falling knife
            entry["trigger"] = (
                "reclassifies Coiled on a close back above the 50 day average with the "
                "3 month return turning positive"
            )
        watch.append(entry)

    # --- earnings calendar inside the window ------------------------------------------
    calendar: list[dict[str, Any]] = []
    if rh_fresh is not None and rh_fresh.earnings:
        for sym, rows_e in rh_fresh.earnings.items():
            for e in rows_e:
                report = (e.get("report") or {}) if isinstance(e, dict) else {}
                edate = report.get("date") or e.get("date")
                if edate and win_start.isoformat() <= str(edate) <= win_end.isoformat():
                    calendar.append({"ticker": sym, "earnings_date": str(edate),
                                     "source": "robinhood"})
        calendar.sort(key=lambda r: r["earnings_date"])
    else:
        try:
            from ..scout_enrichment.enrichment import enrich_symbols

            leader_syms = sorted(
                {l["ticker"] for p in plays for l in (p.get("leaders") or [])[:4]}
            )
            horizon_days = (win_end - today).days
            enriched = enrich_symbols(leader_syms, today=today, earnings_horizon_days=horizon_days)
            for sym, e in (enriched or {}).items():
                edate = getattr(e, "earnings_date", None)
                if edate:
                    calendar.append({"ticker": sym, "earnings_date": str(edate),
                                     "source": "finnhub"})
            calendar.sort(key=lambda r: r["earnings_date"])
        except Exception:
            notes.append("earnings calendar unavailable this run (no snapshot; enrichment errored or no key)")

    from .opportunity import CONTRIBUTING_COLUMNS, EXCLUDED_COLUMNS_NOTE

    return {
        "run_date": today.isoformat(),
        "generated_at": now_iso,
        "window": {"start": win_start.isoformat(), "end": win_end.isoformat()},
        "structures_band": {
            "dte_min": int(st_cfg.get("dte_min", 150)),
            "dte_max": int(st_cfg.get("dte_max", 240)),
        },
        "data_window": spy.window_label,
        "percentile_basis": f"{spy.window_label} percentiles on weekly bars",
        "benchmark": benchmark,
        "rh_snapshot_status": rh_status,
        "structures_priced": structures_priced,
        "contributing_columns": list(CONTRIBUTING_COLUMNS),
        "excluded_columns_note": EXCLUDED_COLUMNS_NOTE,
        "calibration_note": (
            "Relative strength is the leading axis; absolute price percentile is the "
            "secondary filter. Percentiles are computed on weekly bars over the labeled "
            f"{spy.window_label} window (never five-year). The board sorts by RS ascending."
        ),
        "funds": fund_rows,
        "plays": plays,
        "watch": watch,
        "correlation_matrix": {f"{a}/{b}": v for (a, b), v in matrix.items()},
        "calendar": calendar,
        "notes": notes,
        "snapshot_source": (
            "Option figures trace to Robinhood connector quotes captured in the run's "
            "snapshot when present, else Massive per-contract snapshots; timestamps on "
            "every leg."
        ),
    }
