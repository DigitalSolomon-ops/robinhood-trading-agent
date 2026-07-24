from __future__ import annotations

from collections import defaultdict
from typing import Any


NEGATIVE_WORDS = {"hack", "exploit", "lawsuit", "sec", "ban", "halt", "outage", "rug", "fraud", "probe", "crash"}
POSITIVE_WORDS = {"approval", "partnership", "upgrade", "launch", "adoption", "record", "inflow", "rally", "surge"}


def classify_title(title: str) -> tuple[str, float]:
    lower = title.lower()
    score = 0.0
    score += sum(1 for word in POSITIVE_WORDS if word in lower)
    score -= sum(1 for word in NEGATIVE_WORDS if word in lower)
    if score > 0:
        return "positive", score
    if score < 0:
        return "negative", score
    return "neutral", 0.0


def summarize_sentiment(events: list[dict[str, Any]], symbols: list[str]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        symbol = event.get("symbol")
        if symbol:
            grouped[str(symbol)].append(event)
    output: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        rows = grouped.get(symbol, [])
        raw = 0.0
        severe_negative = False
        for row in rows:
            title_label, title_score = classify_title(str(row.get("title", "")))
            event_score = float(row.get("sentiment_value", 0) or 0) + title_score
            if row.get("important") and (event_score < 0 or title_label == "negative"):
                event_score -= 3
            severe_negative = severe_negative or event_score <= -3
            raw += event_score
        normalized = max(0.0, min(100.0, 50.0 + (raw * 10.0)))
        label = "positive" if normalized >= 60 else "negative" if normalized <= 40 else "neutral"
        output[symbol] = {
            "score": normalized,
            "label": label,
            "event_count": len(rows),
            "severe_negative": severe_negative,
            "raw_score": raw,
        }
    return output
