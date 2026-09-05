"""The daily change log: what moved since the previous stored run.

A six-month read does not change much day to day; the change log is what
makes a daily send worth opening. Computed against the previous run's stored
table -- pure dict-diff, unit-testable, no I/O.

Sections, in report order:
  * any falsifier that triggered on a live structure (called out at the top)
  * funds that entered or left a classification, and which gate flipped
  * continuation scores that moved, with the component that moved them
  * order tickets re-priced against the prior run

ANALYSIS ONLY -- no order path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ChangeLog:
    prior_run_date: str | None
    falsifier_alerts: list[str] = field(default_factory=list)
    classification_moves: list[str] = field(default_factory=list)
    score_moves: list[str] = field(default_factory=list)
    ticket_reprices: list[str] = field(default_factory=list)

    @property
    def is_first_run(self) -> bool:
        return self.prior_run_date is None

    @property
    def quiet(self) -> bool:
        return not (
            self.falsifier_alerts or self.classification_moves
            or self.score_moves or self.ticket_reprices
        )


def _fund_map(table: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f.get("symbol", ""): f for f in table.get("funds", []) if isinstance(f, dict)}


def _play_map(table: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p.get("fund", ""): p for p in table.get("plays", []) if isinstance(p, dict)}


def _gate_flips(old: dict[str, Any], new: dict[str, Any]) -> str:
    names = {
        "gate_momentum": "momentum alignment",
        "gate_structure": "trend structure",
        "gate_no_exhaustion": "no exhaustion",
    }
    flips = []
    old_g = old.get("continuation") or {}
    new_g = new.get("continuation") or {}
    for key, label in names.items():
        if bool(old_g.get(key)) != bool(new_g.get(key)):
            flips.append(f"{label} {'now passes' if new_g.get(key) else 'now fails'}")
    if flips:
        return "; ".join(flips)
    if new.get("breadth_demoted") or old.get("breadth_demoted"):
        return "breadth demotion"
    return "no gate flipped (percentile move)"


def compute_changelog(
    current: dict[str, Any],
    prior: dict[str, Any] | None,
    *,
    reprice_threshold_pct: float = 10.0,
) -> ChangeLog:
    """Diff two run tables. `prior` None -> first run (an empty, labeled log)."""
    if prior is None:
        return ChangeLog(prior_run_date=None)
    prior_date = str(prior.get("run_date", "unknown"))

    old_funds, new_funds = _fund_map(prior), _fund_map(current)

    classification_moves: list[str] = []
    score_moves: list[str] = []
    for sym, new_row in new_funds.items():
        old_row = old_funds.get(sym)
        if not old_row:
            continue
        old_class = old_row.get("classification")
        new_class = new_row.get("classification")
        if old_class != new_class:
            classification_moves.append(
                f"{sym}: {old_class} -> {new_class} ({_gate_flips(old_row, new_row)})"
            )
        old_score = (old_row.get("continuation") or {}).get("score")
        new_score = (new_row.get("continuation") or {}).get("score")
        if old_score is not None and new_score is not None and old_score != new_score:
            old_c = (old_row.get("continuation") or {}).get("components") or {}
            new_c = (new_row.get("continuation") or {}).get("components") or {}
            moved = [
                k for k in set(old_c) | set(new_c)
                if int(old_c.get(k, 0)) != int(new_c.get(k, 0))
            ]
            why = ", ".join(sorted(moved)) if moved else "components unchanged"
            score_moves.append(f"{sym}: {old_score}/8 -> {new_score}/8 ({why})")

    # Falsifier triggers on live structures: the prior run's plays checked
    # against the current run's fund closes.
    falsifier_alerts: list[str] = []
    for fund, play in _play_map(prior).items():
        level = play.get("falsifier_level")
        direction = play.get("direction")
        row = new_funds.get(fund)
        if level is None or row is None:
            continue
        last = row.get("last")
        if last is None:
            continue
        try:
            level_f, last_f = float(level), float(last)
        except (TypeError, ValueError):
            continue
        breached = last_f <= level_f if direction == "bullish" else last_f >= level_f
        if breached:
            falsifier_alerts.append(
                f"{fund}: falsifier {level_f:g} breached (last {last_f:g}) -- "
                "the thesis behind the open structure has ended"
            )

    # Ticket re-prices: same fund + same structure, limit moved materially.
    # The analyzer stores limit_price inside play["ticket"]; the top-level key
    # is kept as a fallback for older stored tables (review finding: reading
    # only the top level meant this section could never fire).
    def _limit(play: dict[str, Any]) -> Any:
        return (play.get("ticket") or {}).get("limit_price", play.get("limit_price"))

    ticket_reprices: list[str] = []
    new_plays = _play_map(current)
    for fund, old_play in _play_map(prior).items():
        new_play = new_plays.get(fund)
        if not new_play:
            continue
        if old_play.get("structure") != new_play.get("structure"):
            continue
        old_limit, new_limit = _limit(old_play), _limit(new_play)
        try:
            old_l, new_l = float(old_limit), float(new_limit)
        except (TypeError, ValueError):
            continue
        if old_l <= 0:
            continue
        move_pct = (new_l / old_l - 1.0) * 100.0
        if abs(move_pct) >= reprice_threshold_pct:
            word = "cheaper" if move_pct < 0 else "more expensive"
            ticket_reprices.append(
                f"{fund} {new_play.get('structure')}: limit {old_l:.2f} -> {new_l:.2f} "
                f"({move_pct:+.0f}%, materially {word})"
            )

    return ChangeLog(
        prior_run_date=prior_date,
        falsifier_alerts=falsifier_alerts,
        classification_moves=classification_moves,
        score_moves=score_moves,
        ticket_reprices=ticket_reprices,
    )
