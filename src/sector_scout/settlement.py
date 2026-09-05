"""Settlement for Sector Scout's six-month calls: the report grades itself.

Every structure the report names is recorded at publication with its entry
limit, breakeven, target, falsifier level, both probability estimates and its
factor values. It is then graded on whichever comes first:

  * the falsifier level triggering            -> LOSS (thesis ended)
  * the take-profit UNDERLYING level reached  -> WIN
  * the roll-or-close date                    -> graded vs breakeven
  * expiry                                    -> graded vs breakeven

The target-before-stop math REUSES scout_settlement.settlement.settle -- the
same conservative same-day rule, the same outcome dict shape -- so
scout_calibration can read these records with no parallel path. What is
sector-specific is only the state (a six-month loop cannot ride the 10-day
day-file cycle): open calls persist in the lane's own store and re-evaluate
on every daily run.

Grading is on the UNDERLYING's levels (a checkable price event), not on a
modeled spread value; the report says so. The WIN level is the structure's
max-gain level (the short strike), the LOSS level is the falsifier.

ANALYSIS ONLY -- no order path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ..scout_settlement.settlement import BarLite, bar_date, settle
from .state import StateStore

FROZEN = frozenset({"WIN", "LOSS"})


def record_call(play: dict[str, Any], run_date: date) -> dict[str, Any] | None:
    """A settlement record from one selected play's dict (the run-table shape).
    None when the play carries no ticket (e.g. Falling knife: no structure)."""
    ticket = play.get("ticket") or {}
    if not ticket:
        return None
    fund = play.get("fund")
    direction = "call" if play.get("direction") == "bullish" else "put"
    spot = play.get("spot")
    target = play.get("win_level")           # short strike (max-gain level)
    falsifier = play.get("falsifier_level")
    breakeven = ticket.get("breakeven")
    if None in (fund, spot, target, falsifier, breakeven):
        return None
    return {
        "id": f"sector:{fund}:{direction}:{run_date.isoformat()}",
        "source": "sector",
        "fund": fund,
        "direction": direction,
        "published": run_date.isoformat(),
        "entry_spot": float(spot),
        "entry_limit": ticket.get("limit_price"),
        "breakeven": float(breakeven),
        "target": float(target),
        "falsifier": float(falsifier),
        "expiry": (play.get("ticket") or {}).get("legs", [{}])[0].get("expiry"),
        "roll_or_close": ticket.get("roll_or_close_date"),
        "structure": ticket.get("structure"),
        "prob_bs": play.get("prob_profit_bs"),
        "prob_empirical": play.get("prob_profit_empirical"),
        "factors": play.get("factor_values") or {},
        "outcome": {"verdict": "OPEN", "resolved": "new", "metric": "target_before_stop"},
    }


@dataclass
class SettlementSummary:
    open_calls: int = 0
    settled_win: int = 0
    settled_loss: int = 0
    graded_this_run: int = 0
    realized_hit_rate: float | None = None
    predicted_bs_mean: float | None = None
    notes: list[str] = field(default_factory=list)

    def line(self) -> str:
        settled = self.settled_win + self.settled_loss
        if settled == 0:
            return f"{self.open_calls} calls open, none settled yet"
        rate = f"{(self.settled_win / settled) * 100:.0f}%"
        pred = (
            f" vs predicted {self.predicted_bs_mean * 100:.0f}%"
            if self.predicted_bs_mean is not None
            else ""
        )
        return (
            f"{self.open_calls} open, {settled} settled, realized hit rate {rate}{pred}"
        )


def _grade_at_deadline(call: dict[str, Any], last_close: float, deadline_kind: str) -> dict[str, Any]:
    """Roll-or-close date (or expiry) reached with neither level touched:
    grade vs breakeven -- a bullish structure above breakeven is a WIN."""
    bullish = call.get("direction") == "call"
    breakeven = float(call.get("breakeven", 0.0))
    past = last_close >= breakeven if bullish else last_close <= breakeven
    entry = float(call.get("entry_spot") or 0.0)
    ret = None
    if entry:
        move = (last_close - entry) if bullish else (entry - last_close)
        ret = round(100.0 * move / entry, 2)
    return {
        "verdict": "WIN" if past else "LOSS",
        "actual": round(last_close, 4),
        "return_pct": ret,
        "hit_date": None,
        "resolved": deadline_kind,
        "metric": "breakeven_at_deadline",
    }


def evaluate_open_calls(
    store: StateStore,
    client: Any,
    today: date,
) -> SettlementSummary:
    """Re-evaluate every persisted call: frozen verdicts stay frozen; OPEN
    calls settle target-before-stop over the bars since publication, then
    against the roll-or-close deadline. Errors leave a call untouched (never
    silently drop the record)."""
    calls = store.load_open_calls()
    summary = SettlementSummary()
    changed = False

    for call in calls:
        verdict = ((call.get("outcome") or {}).get("verdict") or "OPEN").upper()
        if verdict in FROZEN:
            if verdict == "WIN":
                summary.settled_win += 1
            else:
                summary.settled_loss += 1
            continue

        published = call.get("published")
        fund = call.get("fund")
        try:
            pub_date = date.fromisoformat(str(published))
        except (TypeError, ValueError):
            summary.notes.append(f"{fund}: bad published date {published!r}")
            summary.open_calls += 1  # still an open record; never undercount
            continue

        try:
            bars_raw = client.get_daily_bars(
                str(fund), (pub_date + timedelta(days=1)).isoformat(), today.isoformat()
            )
        except Exception as exc:
            summary.notes.append(f"{fund}: bars unavailable ({type(exc).__name__})")
            summary.open_calls += 1
            continue
        bars = [
            BarLite(date=bar_date(b.timestamp_ms), high=b.high, low=b.low)
            for b in bars_raw
            if b.high > 0 and b.low > 0
        ]

        outcome = settle(
            direction=str(call.get("direction", "call")),
            entry=float(call.get("entry_spot") or 0.0),
            target=float(call.get("target") or 0.0),
            stop=float(call.get("falsifier") or 0.0),
            daily_bars=bars,
            horizon_days=len(bars),  # every session since publication counts
        )

        if outcome.get("verdict") == "OPEN":
            deadline = call.get("roll_or_close") or call.get("expiry")
            try:
                deadline_d = date.fromisoformat(str(deadline))
            except (TypeError, ValueError):
                deadline_d = None
            if deadline_d is not None and today >= deadline_d and bars:
                closes = [b for b in bars_raw if b.close > 0]
                if closes:
                    outcome = _grade_at_deadline(
                        call, closes[-1].close,
                        "roll_or_close" if call.get("roll_or_close") else "expiry",
                    )

        call["outcome"] = outcome
        changed = True
        v = outcome.get("verdict")
        if v == "WIN":
            summary.settled_win += 1
            summary.graded_this_run += 1
        elif v == "LOSS":
            summary.settled_loss += 1
            summary.graded_this_run += 1
        else:
            summary.open_calls += 1

    preds = [
        float(c.get("prob_bs"))
        for c in calls
        if c.get("prob_bs") is not None
        and ((c.get("outcome") or {}).get("verdict") or "").upper() in FROZEN
    ]
    if preds:
        summary.predicted_bs_mean = sum(preds) / len(preds)
    settled = summary.settled_win + summary.settled_loss
    if settled:
        summary.realized_hit_rate = summary.settled_win / settled

    if changed:
        store.save_open_calls(calls)
    return summary


def append_new_calls(store: StateStore, plays: list[dict[str, Any]], run_date: date) -> int:
    """Record this run's structures, skipping ids already present (a re-run of
    the same day must not double-record)."""
    calls = store.load_open_calls()
    existing = {c.get("id") for c in calls}
    added = 0
    for play in plays:
        rec = record_call(play, run_date)
        if rec is not None and rec["id"] not in existing:
            calls.append(rec)
            added += 1
    if added:
        store.save_open_calls(calls)
    return added
