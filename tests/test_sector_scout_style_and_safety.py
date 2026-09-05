"""House rules the Sector Scout must never break:

  * the six-month forward window resolves FORWARD from the run date
  * no em dash and no en dash anywhere in rendered output
  * no order path: no place/review/preview/cancel/exercise call exists
    anywhere in src/sector_scout/
  * the disclaimer is present and prominent
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from src.sector_scout.analyzer import forward_window
from src.sector_scout.report import DISCLAIMER, render_docx_bytes, render_html

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "sector_scout"


def _sample_table() -> dict:
    leg = {
        "action": "buy", "option_type": "call", "ticker": "O:XLE270115C00070000",
        "strike": 70.0, "expiry": "2027-01-15", "bid": 4.0, "ask": 4.4,
        "mark": 4.2, "delta": 0.61, "iv": 0.24, "open_interest": 1200,
        "snapshot_at": "2026-09-04T18:00:00+00:00", "spread_pct_of_mark": 9.5,
    }
    short = {**leg, "action": "sell", "strike": 80.0, "mark": 1.6, "bid": 1.5,
             "ask": 1.7, "delta": 0.28}
    return {
        "run_date": "2026-09-04",
        "generated_at": "2026-09-04T18:00:00+00:00",
        "window": {"start": "2026-09-04", "end": "2027-03-06"},
        "data_window": "2.0y",
        "benchmark": "SPY",
        "calibration_note": "RS leads; price percentile is the secondary filter.",
        "funds": [
            {
                "symbol": "XLE", "layer": "sector", "last": 64.0, "window": "2.0y",
                "classification": "Leading and earning it",
                "extremes": {
                    "price_pctile": 90.0, "rs_pctile": 85.0, "ret_3m": 0.11,
                    "ret_12m": 0.38, "weekly_rsi": 62.0, "above_sma50": True,
                    "above_sma200": True, "window_label": "2.0y", "pos_52w": 0.9,
                    "drawdown_from_high": -0.03, "runup_from_low": 0.45,
                    "ret_6m": 0.12, "stabilising": True,
                },
                "continuation": {"score": 7, "components": {"beat_spy_12m": 2},
                                 "gate_momentum": True, "gate_structure": True,
                                 "gate_no_exhaustion": True, "gates_passed": 3,
                                 "accel": 0.2, "beat_spy_12m": True,
                                 "pct_above_sma200": 12.0},
                "iv_rank": {"iv_rank": 25.0, "current_iv": 0.23, "days_collected": 90,
                            "window_days": 252, "regime": "buy_premium"},
                "rate_beta": -0.1,
            },
        ],
        "plays": [
            {
                "fund": "XLE", "classification": "Leading and earning it",
                "direction": "bullish", "spot": 64.0,
                "iv_rank": {"iv_rank": 25.0, "regime": "buy_premium"},
                "breadth": {"pct_above_200d": 0.72, "coverage": 0.5},
                "rate_beta": -0.1,
                "leaders": [{"ticker": "XOM", "name": "Exxon Mobil", "market_cap": 5.2e11,
                             "pe": 14.0, "pb": 2.1, "pos_52w": 0.8, "last": 120.0}],
                "dropped_seeds": ["CTRA"],
                "correlated_with": ["XOP"],
                "structure": "long call debit spread",
                "ticket": {
                    "structure": "long call debit spread", "direction": "bullish",
                    "legs": [leg, short], "dte_calendar": 182,
                    "limit_price": 2.65, "midpoint": 2.6, "worst_case": 2.9,
                    "order_type": "net debit", "max_loss": 265.0, "max_gain": 735.0,
                    "reward_to_risk": 2.77, "breakeven": 72.65,
                    "move_required_pct": 13.5, "net_delta": 0.33,
                    "take_profit_level": 477.75, "roll_or_close_date": "2026-12-01",
                    "execution_rules": ["Never a market order on a spread."],
                    "fill_model_note": "Limit modeled as mark +/- 40% of the half-spread.",
                },
                "win_level": 80.0, "falsifier_level": 57.0,
                "falsifier": "a daily close below 57 ends the thesis",
                "prob_profit_bs": 0.31, "prob_max_gain_bs": 0.18,
                "prob_profit_empirical": 0.55,
                "base_rate": {"summary": "55% over 11 matching setups in 2.0y of history",
                              "occurrences": 11, "hits": 6, "low_confidence": False,
                              "confidence_weight": 0.52},
                "divergence_points": 24.0, "divergence_flagged": True,
                "expected_value": 42.0, "reward_to_risk": 2.77,
                "short_interest": "1,200,000 shares short (falling over last 3 readings)",
                "term_structure": "target-expiry ATM IV 24.0% vs front-month 23.0%",
                "skew": "n/a", "seasonality": "avg +3.1%, positive 2/2 times (n=2)",
                "factor_values": {"iv_rank": 25.0},
            },
        ],
        "correlation_matrix": {"XLE/XOP": 0.91},
        "calendar": [{"ticker": "XOM", "earnings_date": "2026-10-30"}],
        "notes": ["History window is 2.0y (plan entitlement cap)."],
        "snapshot_source": "Massive per-contract snapshots captured this run.",
    }


CHANGELOG = {
    "is_first_run": False, "quiet": False, "prior_run_date": "2026-09-03",
    "falsifier_alerts": ["XOP: falsifier 180 breached (last 178)"],
    "classification_moves": ["IHI: Mid range -> Coiled (no gate flipped)"],
    "score_moves": ["XLE: 6/8 -> 7/8 (positive_acceleration)"],
    "ticket_reprices": ["XLE long call debit spread: limit 2.90 -> 2.65 (-9%)"],
}


def test_forward_window_resolves_forward() -> None:
    start, end = forward_window(date(2026, 9, 4))
    assert start == date(2026, 9, 4)
    assert end > start
    assert 170 <= (end - start).days <= 195  # ~6 months FORWARD


def test_no_em_or_en_dash_in_html() -> None:
    html = render_html(_sample_table(), CHANGELOG, "3 open, 1 settled")
    assert "—" not in html, "em dash found in rendered HTML"
    assert "–" not in html, "en dash found in rendered HTML"


def test_no_em_or_en_dash_in_docx_text() -> None:
    payload = render_docx_bytes(_sample_table(), CHANGELOG, "3 open, 1 settled")
    if payload is None:  # python-docx absent: HTML-only run, covered above
        return
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        text = zf.read("word/document.xml").decode("utf-8")
    assert "—" not in text
    assert "–" not in text


def test_disclaimer_present_and_prominent() -> None:
    html = render_html(_sample_table(), CHANGELOG, "none settled yet")
    assert "NOT FINANCIAL ADVICE" in html
    assert "DISCLAIMER" in html
    assert "TOTAL LOSS" in DISCLAIMER


def test_report_carries_snapshot_timestamp_and_divergence() -> None:
    html = render_html(_sample_table(), CHANGELOG, "x")
    assert "2026-09-04T18:00:00+00:00" in html  # snapshot provenance
    assert "disagree by 24" in html             # divergence called out
    assert "FALSIFIER TRIGGERED" in html        # changelog alert at top


def test_no_order_path_in_package() -> None:
    """Grep acceptance: no call to any place/review/preview/cancel/exercise
    order path anywhere in src/sector_scout/."""
    forbidden = re.compile(
        r"place_(equity|option|crypto|)_?order|review_(equity|option|)_?order|"
        r"preview_(equity|option|)_?order|cancel_(equity|option|open|)_?orders?\(|"
        r"exercise_option|robinhood_(equity|option|crypto)_(broker|client)|live_broker",
        re.IGNORECASE,
    )
    hits: list[str] = []
    for path in PACKAGE.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in forbidden.finditer(text):
            hits.append(f"{path.name}: {match.group(0)}")
    assert not hits, hits
