"""Change-log diff: a second run correctly names what moved since the prior."""

from __future__ import annotations

from src.sector_scout.changelog import compute_changelog


def _table(run_date: str, classification: str, score: int, components: dict,
           limit: float, last: float, falsifier: float | None = 60.0) -> dict:
    return {
        "run_date": run_date,
        "funds": [
            {
                "symbol": "XLE",
                "classification": classification,
                "last": last,
                "continuation": {
                    "score": score,
                    "components": components,
                    "gate_momentum": True,
                    "gate_structure": score >= 6,
                    "gate_no_exhaustion": True,
                },
                "extremes": {"rs_pctile": 80.0},
            }
        ],
        "plays": [
            {
                "fund": "XLE",
                "direction": "bullish",
                "structure": "long call debit spread",
                "limit_price": limit,
                "falsifier_level": falsifier,
            }
        ],
    }


def test_first_run_has_no_prior() -> None:
    cl = compute_changelog(_table("2026-09-04", "Extended", 6, {}, 2.6, 64.0), None)
    assert cl.is_first_run
    assert cl.quiet


def test_classification_and_score_and_reprice_moves() -> None:
    prior = _table(
        "2026-09-03", "Mid range", 5,
        {"beat_spy_12m": 2, "positive_acceleration": 2, "within_15pct_of_sma200": 1},
        2.00, 64.0,
    )
    current = _table(
        "2026-09-04", "Leading and earning it", 7,
        {"beat_spy_12m": 2, "positive_acceleration": 2, "pe_at_or_below_universe_median": 2,
         "within_15pct_of_sma200": 1},
        2.60, 66.0,
    )
    cl = compute_changelog(current, prior)
    assert not cl.is_first_run and not cl.quiet
    assert any("Mid range -> Leading and earning it" in m for m in cl.classification_moves)
    assert any("trend structure now passes" in m for m in cl.classification_moves)
    assert any("5/8 -> 7/8" in m and "pe_at_or_below_universe_median" in m for m in cl.score_moves)
    assert any("2.00 -> 2.60" in m for m in cl.ticket_reprices)


def test_falsifier_breach_called_out_at_top() -> None:
    prior = _table("2026-09-03", "Extended", 6, {}, 2.6, 64.0, falsifier=60.0)
    current = _table("2026-09-04", "Extended", 6, {}, 2.6, last=58.0, falsifier=60.0)
    cl = compute_changelog(current, prior)
    assert cl.falsifier_alerts
    assert "falsifier 60" in cl.falsifier_alerts[0]


def test_quiet_session_reads_quiet() -> None:
    prior = _table("2026-09-03", "Extended", 6, {"beat_spy_12m": 2}, 2.60, 64.0)
    current = _table("2026-09-04", "Extended", 6, {"beat_spy_12m": 2}, 2.65, 64.5)
    cl = compute_changelog(current, prior)  # 2% reprice: below threshold
    assert cl.quiet


def test_reprice_reads_nested_ticket_limit() -> None:
    """Review regression (2026-09-04): the analyzer stores limit_price inside
    play['ticket']; the diff must read it there."""
    def t(run_date: str, limit: float) -> dict:
        return {
            "run_date": run_date,
            "funds": [{"symbol": "XLE", "classification": "Extended", "last": 64.0,
                       "continuation": {"score": 6, "components": {}},
                       "extremes": {"rs_pctile": 80.0}}],
            "plays": [{"fund": "XLE", "direction": "bullish",
                       "structure": "long call debit spread",
                       "ticket": {"limit_price": limit},
                       "falsifier_level": None}],
        }

    cl = compute_changelog(t("2026-09-05", 3.20), t("2026-09-04", 2.60))
    assert any("2.60 -> 3.20" in m for m in cl.ticket_reprices)
