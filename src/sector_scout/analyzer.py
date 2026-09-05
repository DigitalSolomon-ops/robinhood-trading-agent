"""The Sector Scout analyzer: universe -> lenses -> factors -> selection ->
structures -> ONE computed run table.

The run table is the single source of truth: the HTML email body and the DOCX
attachment are both rendered from it (report.py), so the two can never
disagree; it is also what gets persisted for the next run's change log and
what settlement records come from.

Call budget (the stock entitlement is ~5 req/min, spaced by the client):
  * one daily-bars pull per fund + SPY + TLT (2y, paginated)
  * one grouped-daily call per BACKFILLED breadth day (bounded per run)
  * ticker details / financials / short interest only for confirmed leaders
    and shortlisted funds (bounded in config)
Options-plan calls (contract reference + per-contract snapshots) are not
throttled -- that side of the plan is paid.

ANALYSIS ONLY. Nothing in this package places, previews, reviews, or cancels
an order. Grep acceptance: the words themselves appear here only inside this
disclaimer sentence.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .base_rate import replay_base_rate
from .data import FundHistory, SectorDataClient
from .factors import (
    IVRankRead,
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
from .state import StateStore
from .structures import (
    Leg,
    build_credit_spread_ticket,
    build_debit_spread_ticket,
    expected_move_6m,
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


# --- leaders ---------------------------------------------------------------------


def confirm_leaders(
    seeds: list[str],
    constituent_series: dict[str, list[tuple[date, float]]],
    client: SectorDataClient,
    *,
    detail_budget: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Confirm seed tickers against the breadth cache (present with a recent
    close = resolves). Fetch reference details / fundamentals for the largest
    few within the budget. Unresolved seeds are DROPPED AND NAMED, never
    substituted."""
    confirmed: list[dict[str, Any]] = []
    dropped: list[str] = []
    for ticker in seeds:
        pts = constituent_series.get(ticker.upper())
        if not pts:
            dropped.append(ticker)
            continue
        closes = [c for _, c in pts]
        current = closes[-1]
        yr = closes[-252:] if len(closes) >= 252 else closes
        hi, lo = max(yr), min(yr)
        confirmed.append(
            {
                "ticker": ticker.upper(),
                "last": round(current, 2),
                "pos_52w": round((current - lo) / (hi - lo), 3) if hi > lo else None,
                "market_cap": None,
                "pe": None,
                "pb": None,
                "name": None,
            }
        )

    detailed = 0
    for leader in confirmed:
        if detailed >= detail_budget:
            break
        try:
            details = client.get_ticker_details(leader["ticker"])
        except Exception:
            continue
        leader["market_cap"] = details.market_cap
        leader["name"] = details.name
        fin = client.get_financial_snapshot(leader["ticker"])
        if fin:
            eps = fin.get("eps_diluted")
            equity = fin.get("equity")
            shares = details.shares_outstanding
            if eps and eps > 0:
                # Quarterly EPS x4 as the trailing proxy; labeled a proxy.
                leader["pe"] = round(leader["last"] / (eps * 4.0), 1)
            if equity and shares and shares > 0 and leader["last"] > 0:
                bvps = equity / shares
                if bvps > 0:
                    leader["pb"] = round(leader["last"] / bvps, 2)
        detailed += 1

    confirmed.sort(key=lambda l: (l.get("market_cap") or 0.0), reverse=True)
    return confirmed, dropped


# --- option-chain reads -----------------------------------------------------------


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
    )


def atm_iv(client: SectorDataClient, fund: str, spot: float, today: date,
           *, dte_lo: int, dte_hi: int, band_lo: float = 0.85,
           band_hi: float = 1.15) -> float | None:
    """ATM IV at the nearest expiry inside [dte_lo, dte_hi] days: the contract
    whose strike is closest to spot, one snapshot. None off-hours/missing --
    the IV history simply skips that day."""
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
    """(chosen expiry, contracts at that expiry) inside the DTE band."""
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
    """Scan the strikes bracketing spot, snapshot each, keep the |delta|
    closest to target. Falls back to strike-nearest when greeks are dark."""
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
    notes: list[str] = []

    win_start, win_end = forward_window(today)
    sel0 = config.get("selection", {}) or {}

    # --- histories (throttled stock-side) ------------------------------------
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
            f"{years}y requested; every percentile is computed and labeled over the actual window."
        )

    # --- breadth cache ----------------------------------------------------------
    br_cfg = f_cfg.get("breadth", {}) or {}
    lookback = int(br_cfg.get("lookback_days", 260))
    all_constituents = {t for seeds in seeds_map.values() for t in seeds}
    from .data import BreadthCache

    cache = BreadthCache(store.breadth_dir, all_constituents)
    fetched = cache.backfill(
        client, today, lookback, int(br_cfg.get("backfill_days_per_run", 40))
    )
    coverage = cache.coverage(today, lookback)
    if coverage < 0.999:
        notes.append(
            f"Breadth cache at {coverage * 100:.0f}% coverage (backfilled {fetched} "
            "days this run); constituent breadth reads carry partial-coverage labels until full."
        )
    constituent_series = cache.constituent_series(today, lookback)

    # --- IV rank inputs -----------------------------------------------------------
    iv_cfg = f_cfg.get("iv_rank", {}) or {}
    st_cfg = config.get("structures", {}) or {}
    iv_history_all = store.load_iv_history()
    atm_band = iv_cfg.get("atm_strike_band") or [0.85, 1.15]
    atm_now: dict[str, float] = {}
    for fund, hist in histories.items():
        iv = atm_iv(
            client, fund, hist.closes_daily[-1], today,
            dte_lo=int(iv_cfg.get("atm_dte_lo", 20)),
            dte_hi=int(iv_cfg.get("atm_dte_hi", 45)),
            band_lo=float(atm_band[0]), band_hi=float(atm_band[1]),
        )
        if iv is not None:
            atm_now[fund] = iv
    store.record_iv(today, atm_now)

    # --- per-fund rows ---------------------------------------------------------------
    # Valuation proxy: median confirmed-leader P/E per fund (the plan's ratio
    # endpoint is 403; the method section names this a leaders-median proxy).
    fund_rows: list[dict[str, Any]] = []
    leaders_by_fund: dict[str, list[dict[str, Any]]] = {}
    dropped_by_fund: dict[str, list[str]] = {}
    pe_by_fund: dict[str, float] = {}

    for fund, hist in histories.items():
        ext = extremes_read(
            closes_daily=hist.closes_daily,
            highs_daily=hist.highs_daily,
            lows_daily=hist.lows_daily,
            closes_monthly=hist.closes_monthly,
            spy_closes_monthly=spy.closes_monthly,
            weekly_closes=hist.closes_weekly,
            window_label=hist.window_label,
            cfg=config,
        )
        if ext is None:
            notes.append(f"{fund}: insufficient history for the lenses; excluded")
            continue

        cont = continuation_read(
            closes_daily=hist.closes_daily,
            spy_closes_daily=spy.closes_daily,
            weekly_closes=hist.closes_weekly,
            fund_pe=None,                # filled after leader confirmation below
            universe_median_pe=None,
            leader_earnings_improving=None,
            cfg=config,
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
                "extremes": asdict(ext),
                "continuation": asdict(cont) if cont else None,
                "iv_rank": asdict(ivr),
                "rate_beta": beta,
                "classification": None,  # set below once valuation lands
            }
        )

    # --- leaders for classification-relevant funds -----------------------------------
    # Confirm leaders for every fund from the cache (free); spend detail calls
    # only on the funds the lenses put in play.
    detail_budget = int(sel0.get("leader_detail_budget", 2))
    for row in fund_rows:
        fund = row["symbol"]
        leaders, dropped = confirm_leaders(
            seeds_map.get(fund, []), constituent_series, client,
            detail_budget=0,  # no API detail yet; free confirmation pass
        )
        leaders_by_fund[fund] = leaders
        dropped_by_fund[fund] = dropped

    # Preliminary classification (valuation unknown) to find the shortlist.
    for row in fund_rows:
        ext_obj = row["extremes"]
        from .lenses import ExtremesRead as _ER

        ext = _ER(**{k: ext_obj[k] for k in _ER.__dataclass_fields__})
        row["classification"] = classify(
            ext, valuation_rich=None, valuation_cheap=None, earnings_growing=None,
            cfg=config,
        )

    interesting = [
        r for r in fund_rows
        if r["classification"] in (COILED, EXTENDED, LEADING, FALLING_KNIFE)
        or (r.get("continuation") or {}).get("score", 0) >= 5
    ]
    # Bound the stock-side detail budget: the strongest few carry the report.
    interesting = interesting[: int(sel0.get("shortlist_max", 10))]
    for row in interesting:
        fund = row["symbol"]
        leaders, dropped = confirm_leaders(
            seeds_map.get(fund, []), constituent_series, client,
            detail_budget=detail_budget,
        )
        leaders_by_fund[fund] = leaders
        dropped_by_fund[fund] = dropped
        pes = [l["pe"] for l in leaders if l.get("pe")]
        if pes:
            pe_by_fund[fund] = _median(pes)

    universe_median_pe = _median(list(pe_by_fund.values()))

    # Final classification + continuation rescore with the valuation overlay.
    for row in fund_rows:
        fund = row["symbol"]
        hist = histories[fund]
        fund_pe = pe_by_fund.get(fund)
        rich = (fund_pe > universe_median_pe * 1.25) if (fund_pe and universe_median_pe) else None
        cheap = (fund_pe < universe_median_pe * 0.8) if (fund_pe and universe_median_pe) else None
        # Earnings direction: improving when the two largest confirmed leaders
        # both carry positive trailing EPS (the plan has no forward estimates;
        # method section states the fallback).
        top2 = [l for l in leaders_by_fund.get(fund, [])[:2] if l.get("pe") is not None]
        earnings_growing = bool(top2) and all(l["pe"] > 0 for l in top2) or None

        from .lenses import ExtremesRead as _ER

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
        )
        row["continuation"] = asdict(cont) if cont else None
        row["valuation"] = {
            "leader_median_pe": fund_pe,
            "universe_median_pe": universe_median_pe,
            "basis": "median of confirmed leaders (proxy; ratio history not on plan)",
        }

        # Breadth (factor 1) with the demotion rule -- Extended AND Continuation.
        br = breadth_read(
            constituent_series,
            seeds_map.get(fund, []),
            ext.pos_52w,
            coverage=coverage,
            classification=row["classification"],
            demotion_pct=float(br_cfg.get("demotion_pct_above_200d", 40)),
            continuation_score=int((row.get("continuation") or {}).get("score") or 0),
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

    # Board sort: RS ascending (the calibration fix -- RS is the leading axis).
    fund_rows.sort(key=lambda r: r["extremes"]["rs_pctile"])

    # --- selection --------------------------------------------------------------------
    sel_cfg = config.get("selection", {}) or {}
    max_plays = int(sel_cfg.get("max_plays", 6))

    def _priority(row: dict[str, Any]) -> tuple[int, float]:
        if row.get("breadth_demoted"):
            return (9, 0.0)  # a breadth-demoted fund cannot re-enter selection
        cls = row["classification"]
        cont_score = (row.get("continuation") or {}).get("score", 0)
        if cls == COILED:
            return (0, -row["extremes"]["rs_pctile"])
        if cont_score >= 6:
            return (1, -cont_score)
        if cls in (EXTENDED,):
            return (2, row["extremes"]["rs_pctile"])
        if cls == LEADING and cont_score >= 5:
            return (3, -cont_score)
        return (9, 0.0)

    candidates = [r for r in fund_rows if _priority(r)[0] < 9]
    candidates.sort(key=_priority)

    # Correlation guard over the candidate set.
    corr_cfg = f_cfg.get("correlation", {}) or {}
    corr_lookback = int(corr_cfg.get("lookback_months", 12)) * 21
    closes_map = {r["symbol"]: histories[r["symbol"]].closes_daily for r in candidates}
    matrix = correlation_matrix(closes_map, corr_lookback)
    collapsed = collapse_correlated(
        [r["symbol"] for r in candidates], matrix,
        float(corr_cfg.get("single_position_threshold", 0.80)),
    )
    primaries = [r for r in candidates if r["symbol"] not in collapsed][:max_plays]

    # Shared-duration call-out (factor 5).
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

    # --- structures per selected play ---------------------------------------------------
    prob_cfg = config.get("probability", {}) or {}
    rate = float(prob_cfg.get("risk_free_rate", 0.04))
    base_cfg = config.get("base_rate", {}) or {}
    si_cfg = f_cfg.get("short_interest", {}) or {}
    plays: list[dict[str, Any]] = []
    si_budget = int(si_cfg.get("max_funds", 10))

    for row in primaries:
        fund = row["symbol"]
        hist = histories[fund]
        spot = hist.closes_daily[-1]
        cls = row["classification"]
        ivr = row["iv_rank"]
        cont = row.get("continuation") or {}
        bullish = cls == COILED or (cont.get("score", 0) >= 5 and cls != EXTENDED)
        direction = "bullish" if bullish else "bearish"

        play: dict[str, Any] = {
            "fund": fund,
            "classification": cls,
            "direction": direction,
            "spot": round(spot, 2),
            "iv_rank": ivr,
            "breadth": row.get("breadth"),
            "rate_beta": row.get("rate_beta"),
            "leaders": leaders_by_fund.get(fund, [])[:8],
            "dropped_seeds": dropped_by_fund.get(fund, []),
            "correlated_with": [s for s, p in collapsed.items() if p == fund],
        }

        if cls == FALLING_KNIFE:
            play["structure"] = None
            play["skipped_reason"] = (
                "Falling knife: no structure. The falsifier that would create one is a "
                "monthly close back above the 50 day average with the 3 month return "
                "turning positive -- that combination reclassifies the fund Coiled."
            )
            plays.append(play)
            continue

        # Short interest (context, budgeted).
        if si_cfg.get("enabled", True) and si_budget > 0:
            play["short_interest"] = short_interest_context(
                client.get_short_interest(fund, int(si_cfg.get("readings", 3)))
            )
            si_budget -= 1

        option_type = "call" if bullish else "put"
        expiry, contracts = chain_for_structure(client, fund, option_type, spot, today, config)
        dte_band = f"{int(st_cfg.get('dte_min', 150))}-{int(st_cfg.get('dte_max', 240))}"
        if not contracts:
            play["structure"] = None
            play["skipped_reason"] = f"no listed contracts in the {dte_band} DTE band this run"
            plays.append(play)
            continue

        em6 = expected_move_6m(
            hist.closes_daily, int(st_cfg.get("expected_move_lookback_days", 63))
        )
        scan_max = int(st_cfg.get("delta_scan_max_contracts", 12))
        sell_premium = ivr.get("regime") == "sell_premium"

        built = None
        if sell_premium:
            # Same view, credit structure: bullish -> short put spread.
            credit_type = "put" if bullish else "call"
            _, credit_contracts = chain_for_structure(client, fund, credit_type, spot, today, config)
            if credit_contracts:
                short_delta = float(st_cfg.get("credit_short_delta", 0.30))
                short_leg = leg_nearest_delta(
                    client, fund, "sell", credit_type, credit_contracts, spot,
                    short_delta, now_iso, scan_max=scan_max,
                )
                if short_leg is not None:
                    width = max(
                        spot * float(st_cfg.get("credit_width_pct", 0.05)),
                        float(st_cfg.get("credit_width_min", 5.0)),
                    )
                    wing_level = short_leg.strike - width if bullish else short_leg.strike + width
                    long_leg = leg_nearest_strike(
                        client, fund, "buy", credit_type, credit_contracts, wing_level, now_iso,
                    )
                    if long_leg is not None and long_leg.strike != short_leg.strike:
                        built = build_credit_spread_ticket(
                            direction=direction, short_leg=short_leg, long_leg=long_leg,
                            spot=spot, today=today, cfg=config, rate=rate,
                        )
        if built is None:
            target_delta = (
                float(st_cfg.get("continuation_long_delta", 0.60))
                if cls != COILED and bullish
                else float(st_cfg.get("coiled_long_delta", 0.35))
            )
            long_leg = leg_nearest_delta(
                client, fund, "buy", option_type, contracts, spot, target_delta,
                now_iso, scan_max=scan_max,
            )
            if long_leg is None:
                play["structure"] = None
                play["skipped_reason"] = "no live snapshot for any candidate long leg"
                plays.append(play)
                continue
            if bullish:
                anchor = measured_move_strike(spot, em6) if em6 is not None else 0.0
                short_level = max(anchor, long_leg.strike + max(spot * 0.03, 1.0))
            else:
                anchor = spot * (1.0 - em6) if em6 is not None else float("inf")
                short_level = min(anchor, long_leg.strike - max(spot * 0.03, 1.0))
            short_leg = leg_nearest_strike(
                client, fund, "sell", option_type, contracts, short_level, now_iso,
            )
            if short_leg is None or short_leg.strike == long_leg.strike:
                play["structure"] = None
                play["skipped_reason"] = "no live snapshot for the short leg"
                plays.append(play)
                continue
            built = build_debit_spread_ticket(
                direction=direction, long_leg=long_leg, short_leg=short_leg,
                spot=spot, today=today, cfg=config, rate=rate,
            )

        if built is None:
            play["structure"] = None
            play["skipped_reason"] = "required live quote missing on a leg (n/a, not fabricated)"
            plays.append(play)
            continue

        ticket, prob = built
        legs = ticket.legs
        # Falsifier: the level that ends the thesis. Coiled: back below the
        # structure low; continuation: loss of the 200 day; extended (puts):
        # a close above the recent high.
        sma200 = None
        if len(hist.closes_daily) >= 200:
            sma200 = sum(hist.closes_daily[-200:]) / 200.0
        dd_frac = 1.0 - float(st_cfg.get("falsifier_drawdown_fraction", 0.10))
        if bullish:
            falsifier_level = round(
                min(spot * dd_frac, sma200 if sma200 else spot * dd_frac), 2
            )
            win_level = max(l.strike for l in legs)
            falsifier_text = (
                f"a daily close below {falsifier_level:g} (the 200 day average / -10% zone): "
                "the trend structure this play rests on is gone -- exit"
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
            closes_monthly=hist.closes_monthly,
            spy_closes_monthly=spy.closes_monthly,
            monthly_indices=hist.monthly_indices,
            target_classification=cls,
            bullish=bullish,
            required_move_pct=move_req,
            forward_months=int(base_cfg.get("forward_months", 6)),
            min_occurrences=int(base_cfg.get("min_occurrences", 10)),
            window_label=hist.window_label,
            cfg=config,
        )

        # Term structure + skew (context).
        ts_cfg = f_cfg.get("term_structure_skew", {}) or {}
        front = atm_now.get(fund)
        target_iv_vals = [l.iv for l in legs if l.iv is not None]
        target_iv = target_iv_vals[0] if target_iv_vals else None
        play["term_structure"] = term_structure_note(
            front, target_iv, cheap_rich_pts=float(ts_cfg.get("cheap_rich_pts", 2.0))
        )
        skew_reading = "n/a"
        if len(legs) >= 1:
            skew_reading = skew_note(
                None, None, cls,
                heavy_put_skew_pts=float(ts_cfg.get("heavy_put_skew_pts", 4.0)),
            )  # 25-delta scan lands in a later pass; n/a until then
        play["skew"] = skew_reading

        # Seasonality (context; tiny N by construction).
        months_labels = [
            f"{d.year:04d}-{d.month:02d}"
            for d in hist.dates_daily
        ]
        # last label per month, aligned with closes_monthly
        seen: list[str] = []
        for lab in months_labels:
            if not seen or seen[-1] != lab:
                seen.append(lab)
        play["seasonality"] = seasonality_note(
            hist.closes_monthly, seen[-len(hist.closes_monthly):], today.month,
            int(base_cfg.get("forward_months", 6)),
        )

        bs_prob = round(prob.prob_profit, 4)
        emp_prob = round(br_rate.rate, 4) if br_rate.occurrences else None
        divergence = None
        if emp_prob is not None:
            divergence = abs(bs_prob - emp_prob) * 100.0

        play.update(
            {
                "structure": ticket.structure,
                "ticket": {
                    **asdict(ticket),
                    # asdict drops properties; the ticket must carry the
                    # bid-ask spread as a percent of mark per leg.
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
                "factor_values": {
                    "breadth_pct_above_200d": (row.get("breadth") or {}).get("pct_above_200d"),
                    "iv_rank": ivr.get("iv_rank"),
                    "rate_beta": row.get("rate_beta"),
                    "continuation_score": cont.get("score"),
                    "rs_pctile": row["extremes"]["rs_pctile"],
                },
            }
        )
        plays.append(play)

    # Rank plays by EV (edge), not comfort; structures without EV sink last.
    plays.sort(key=lambda p: (p.get("expected_value") is not None, p.get("expected_value") or 0), reverse=True)

    # --- earnings calendar inside the window (best-effort Finnhub) ---------------------
    calendar: list[dict[str, Any]] = []
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
                calendar.append({"ticker": sym, "earnings_date": str(edate)})
        calendar.sort(key=lambda r: r["earnings_date"])
    except Exception:
        notes.append("earnings calendar unavailable this run (enrichment errored or no key)")

    return {
        "run_date": today.isoformat(),
        "generated_at": now_iso,
        "window": {"start": win_start.isoformat(), "end": win_end.isoformat()},
        "structures_band": {
            "dte_min": int(st_cfg.get("dte_min", 150)),
            "dte_max": int(st_cfg.get("dte_max", 240)),
        },
        "data_window": spy.window_label,
        "benchmark": benchmark,
        "calibration_note": (
            "Relative strength is the leading axis; absolute price percentile is the "
            "secondary filter (validated 2026-09-04: near index highs only 1 of 34 funds "
            "cleared the absolute price test while 19 of 34 sat at RS percentile 30 or below). "
            "The board sorts by RS ascending."
        ),
        "funds": fund_rows,
        "plays": plays,
        "correlation_matrix": {f"{a}/{b}": v for (a, b), v in matrix.items()},
        "calendar": calendar,
        "notes": notes,
        "snapshot_source": "Massive per-contract snapshots captured this run; timestamps on every leg.",
    }
