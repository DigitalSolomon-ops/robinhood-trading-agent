from __future__ import annotations

from typing import Any


RISK_OFF_UP = {"FEDFUNDS", "CPIAUCSL", "UNRATE", "DGS10", "DGS2", "DCOILWTICO"}
RISK_OFF_DOWN = {"T10Y2Y"}


def macro_risk_score(observations: list[dict[str, Any]]) -> dict[str, Any]:
    if not observations:
        return {"score": 50.0, "label": "neutral", "reasons": ["macro data missing"], "missing_data": ["macro"]}
    score = 55.0
    reasons: list[str] = []
    for row in observations:
        series_id = str(row.get("series_id", ""))
        direction = row.get("direction")
        if series_id in RISK_OFF_UP and direction == "up":
            score -= 5
            reasons.append(f"{series_id} rising")
        elif series_id in RISK_OFF_UP and direction == "down":
            score += 3
            reasons.append(f"{series_id} falling")
        elif series_id in RISK_OFF_DOWN and direction == "down":
            score -= 5
            reasons.append(f"{series_id} falling")
        elif series_id in RISK_OFF_DOWN and direction == "up":
            score += 3
            reasons.append(f"{series_id} rising")
    score = max(0.0, min(100.0, score))
    label = "risk-on" if score >= 60 else "risk-off" if score < 40 else "neutral"
    return {"score": score, "label": label, "reasons": reasons or ["macro risk neutral"], "missing_data": []}
