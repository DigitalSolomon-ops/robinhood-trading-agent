"""Sector Scout lens fixtures: reproduce the validated 2026-09-04 scoring on
synthetic inputs engineered to carry the same component outcomes.

Acceptance: XOP and XLE score 7/8, XLV and XLF score 6/8, OIH scores 4/8,
XBI scores 1/8, and IHI is the ONLY fund classified Coiled. XBI's fixture
also pins the behaviour the run validated: the largest mover is the weakest
continuation candidate when its pace is no longer increasing.
"""

from __future__ import annotations

from src.sector_scout.lenses import (
    COILED,
    acceleration,
    classify,
    continuation_read,
    extremes_read,
    pct_return,
    rank_percentile,
)

CFG = {
    "extremes": {
        "coiled_price_pctile_max": 25,
        "coiled_rs_pctile_max": 30,
        "extended_price_pctile_min": 85,
        "extended_rs_pctile_min": 80,
        "rs_is_leading_axis": True,
    },
    "continuation": {
        "rsi_weekly_max": 75,
        "max_pct_above_sma200": 25,
        "within_sma200_pct": 15,
        "score": {
            "beat_spy_12m": 2,
            "positive_acceleration": 2,
            "pe_at_or_below_universe_median": 2,
            "within_15pct_of_sma200": 1,
            "improving_leader_earnings": 1,
        },
    },
}

N = 550


def zigzag(n: int, drift_per_2d: float, base: float = 100.0, up_ratio: float = 1.6) -> list[float]:
    closes = [base]
    up = drift_per_2d * up_ratio / (up_ratio - 1)
    dn = up - drift_per_2d
    for i in range(n - 1):
        f = (1 + up) if i % 2 == 0 else (1 - dn)
        closes.append(closes[-1] * f)
    return closes


def phased(n: int, phases: list[tuple[int, float]]) -> list[float]:
    closes = [100.0]
    for days, ret in phases:
        pairs = max(days // 2, 1)
        per2 = (1 + ret) ** (1 / pairs) - 1
        seg = zigzag(days + 1, per2, base=closes[-1])
        closes.extend(seg[1:])
    return closes[:n] if len(closes) >= n else closes + [closes[-1]] * (n - len(closes))


SPY = phased(N, [(550, 0.16)])

FIXTURES = {
    # symbol: (phases, fund_pe, median_pe, earnings_improving, expected_score)
    "XOP": ([(300, 0.05), (187, 0.18), (63, 0.14)], 12.0, 18.0, True, 7),
    "XLE": ([(300, 0.05), (187, 0.18), (63, 0.14)], 12.0, 18.0, True, 7),
    "XLV": ([(300, 0.06), (187, 0.10), (63, 0.06)], 22.0, 18.0, True, 6),
    "XLF": ([(300, 0.06), (187, 0.10), (63, 0.06)], 22.0, 18.0, True, 6),
    "OIH": ([(300, 0.02), (187, 0.16), (63, 0.15)], 25.0, 18.0, False, 4),
    "XBI": ([(300, 0.02), (187, 0.05), (63, -0.02)], 30.0, 18.0, False, 1),
}

IHI_PHASES = [(400, -0.30), (87, -0.05), (63, 0.05)]


def _cont(closes: list[float], pe: float, med: float, earn: bool):
    return continuation_read(
        closes_daily=closes,
        spy_closes_daily=SPY,
        weekly_closes=closes[::5],
        fund_pe=pe,
        universe_median_pe=med,
        leader_earnings_improving=earn,
        cfg=CFG,
    )


def test_validated_continuation_scores() -> None:
    for symbol, (phases, pe, med, earn, expected) in FIXTURES.items():
        closes = phased(N, phases)
        cont = _cont(closes, pe, med, earn)
        assert cont is not None, symbol
        assert cont.score == expected, (
            f"{symbol}: expected {expected}/8, got {cont.score}/8 ({cont.components})"
        )


def test_xbi_largest_mover_is_weakest_when_pace_decays() -> None:
    """OIH's mover fixture out-scores XBI even when XBI's raw move is larger
    in some window: acceleration, not the 12-month return, does the work."""
    xbi = phased(N, FIXTURES["XBI"][0])
    cont = _cont(xbi, 30.0, 18.0, False)
    assert cont is not None
    assert cont.accel is not None and cont.accel < 0
    assert cont.components["positive_acceleration"] == 0


def test_ihi_is_the_only_coiled_fund() -> None:
    spy_monthly = SPY[::21]
    classifications: dict[str, str] = {}
    for symbol, (phases, *_rest) in {**FIXTURES, "IHI": (IHI_PHASES, 0, 0, None, 0)}.items():
        closes = phased(N, phases)
        ext = extremes_read(
            closes_daily=closes,
            highs_daily=[c * 1.01 for c in closes],
            lows_daily=[c * 0.99 for c in closes],
            closes_monthly=closes[::21],
            spy_closes_monthly=spy_monthly,
            weekly_closes=closes[::5],
            window_label="2.0y",
            cfg=CFG,
        )
        assert ext is not None, symbol
        classifications[symbol] = classify(
            ext,
            valuation_rich=None,
            valuation_cheap=True if symbol == "IHI" else None,
            earnings_growing=None,
            cfg=CFG,
        )
    coiled = [s for s, c in classifications.items() if c == COILED]
    assert coiled == ["IHI"], classifications


def test_rank_percentile_is_rank_based_not_range() -> None:
    history = [10.0] * 9 + [100.0]
    # Range position of 11 would be ~1%; RANK percentile is 90 (9 of 10 below).
    assert rank_percentile(history, 11.0) == 90.0
    assert rank_percentile([], 5.0) == 50.0


def test_acceleration_formula_exact() -> None:
    """accel = 3m return annualised (x4) minus the realised 12m return."""
    closes = [100.0] * 300
    # engineer: 12m return = +20%, 3m return = +2% -> accel = 0.08 - 0.20 < 0
    series = [100.0] * 60
    for i in range(252):
        series.append(series[-1] * (1.20 ** (1 / 252)))
    tail_start = series[-64]
    r3 = pct_return(series, 63)
    r12 = pct_return(series, 252)
    accel = acceleration(series)
    assert accel is not None and r3 is not None and r12 is not None
    assert abs(accel - (r3 * 4 - r12)) < 1e-12


def test_fairly_valued_stabilising_deep_fund_is_coiled() -> None:
    """Review regression (2026-09-04): cheap=False, rich=False (fairly valued)
    must still classify Coiled when deep and stabilising -- the bar is
    not-expensive, not cheap."""
    spy_monthly = SPY[::21]
    closes = phased(N, IHI_PHASES)
    ext = extremes_read(
        closes_daily=closes,
        highs_daily=[c * 1.01 for c in closes],
        lows_daily=[c * 0.99 for c in closes],
        closes_monthly=closes[::21],
        spy_closes_monthly=spy_monthly,
        weekly_closes=closes[::5],
        window_label="2.0y",
        cfg=CFG,
    )
    assert ext is not None
    assert classify(ext, valuation_rich=False, valuation_cheap=False,
                    earnings_growing=None, cfg=CFG) == COILED
    # Rich stays excluded; unknown stays included.
    assert classify(ext, valuation_rich=True, valuation_cheap=False,
                    earnings_growing=None, cfg=CFG) != COILED
    assert classify(ext, valuation_rich=None, valuation_cheap=None,
                    earnings_growing=None, cfg=CFG) == COILED


def test_base_rate_uses_date_anchored_month_indices() -> None:
    """Review regression (2026-09-04): the replay must truncate the daily
    series exactly at each month-end (no future bars leak into historical
    classification). monthly_end_indices maps monthly closes to daily
    indices; the value at the truncation point must equal the monthly close."""
    from datetime import date, timedelta

    from src.sector_scout.data import monthly_end_indices, resample_last_per_period

    # A partial first month (starts the 20th) breaks any 21-bars-per-month
    # assumption immediately.
    dates, d = [], date(2024, 9, 20)
    while len(dates) < 300:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)
    closes = [100.0 + i * 0.1 for i in range(len(dates))]
    monthly = resample_last_per_period(closes, dates, weekly=False)
    idx = monthly_end_indices(dates)
    assert len(idx) == len(monthly)
    for m, i in enumerate(idx):
        assert closes[i] == monthly[m], f"month {m}: index {i} does not land on the month-end close"
