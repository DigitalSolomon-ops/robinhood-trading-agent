from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from ..strategy_engine import TradeSignal
from .crypto_market_client import COINGECKO_IDS, CoinGeckoMarketClient
from .intelligence_store import IntelligenceStore
from .macro_client import FredMacroClient
from .macro_risk_engine import macro_risk_score
from .news_client import CryptoPanicNewsClient
from .sentiment_engine import summarize_sentiment


def load_intelligence_config(root: Path) -> dict[str, Any]:
    path = root / "config" / "intelligence.yaml"
    if not path.exists():
        return {"intelligence": {"enabled": False}}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {"intelligence": {"enabled": False}}


def store_for(root: Path) -> IntelligenceStore:
    return IntelligenceStore(root / "data" / "intelligence.db")


def collect_intelligence(root: Path, symbols: list[str]) -> dict[str, Any]:
    load_dotenv(root / ".env", override=True)
    config = load_intelligence_config(root)
    intelligence = config.get("intelligence", {})
    store = store_for(root)
    result: dict[str, Any] = {
        "submitted": False,
        "enabled": bool(intelligence.get("enabled", True)),
        "news_status": "disabled",
        "crypto_market_status": "disabled",
        "macro_status": "disabled",
        "events_stored": 0,
        "macro_observations_stored": 0,
        "market_context_rows_stored": 0,
        "errors": [],
    }

    if not result["enabled"]:
        return result

    if intelligence.get("use_news_filter", True):
        client = CryptoPanicNewsClient()
        result["news_status"] = client.status
        try:
            payload = client.fetch(symbols)
            result["news_status"] = payload.get("status", client.status)
            events = payload.get("events", [])
            for event in events:
                store.save_news_event(event)
            sentiments = summarize_sentiment(events, symbols)
            for symbol, summary in sentiments.items():
                store.save_coin_sentiment(symbol, summary["score"], summary["label"], summary["event_count"], summary)
            result["events_stored"] = len(events)
        except Exception as exc:
            store.log_error("cryptopanic", "collect_news", str(exc))
            result["errors"].append("news collection failed")

    if intelligence.get("use_market_context_filter", True):
        client = CoinGeckoMarketClient()
        result["crypto_market_status"] = client.status
        try:
            payload = client.fetch(symbols)
            result["crypto_market_status"] = payload.get("status", client.status)
            if payload.get("status") == "ok":
                global_data = payload.get("global", {}).get("data", {})
                market_change = global_data.get("market_cap_change_percentage_24h_usd")
                btc_dominance = (global_data.get("market_cap_percentage") or {}).get("btc")
                eth_dominance = (global_data.get("market_cap_percentage") or {}).get("eth")
                rows = [
                    ("market_cap_change_24h_percent", market_change, global_data),
                    ("btc_dominance_percent", btc_dominance, global_data),
                    ("eth_dominance_percent", eth_dominance, global_data),
                ]
                for metric, value, details in rows:
                    store.save_market_context("coingecko", metric, _float_or_none(value), details)
                    result["market_context_rows_stored"] += 1
                for coin in payload.get("coins", []):
                    if not isinstance(coin, dict):
                        continue
                    symbol = _symbol_from_coingecko_id(str(coin.get("id", "")), symbols)
                    if symbol:
                        store.save_market_context("coingecko", f"{symbol}_change_24h_percent", _float_or_none(coin.get("price_change_percentage_24h")), coin)
                        result["market_context_rows_stored"] += 1
        except Exception as exc:
            store.log_error("coingecko", "collect_market_context", str(exc))
            result["errors"].append("crypto market collection failed")

    if intelligence.get("use_macro_filter", True):
        client = FredMacroClient()
        result["macro_status"] = client.status
        try:
            payload = client.fetch()
            result["macro_status"] = payload.get("status", client.status)
            for observation in payload.get("observations", []):
                store.save_macro_observation(observation)
                result["macro_observations_stored"] += 1
        except Exception as exc:
            store.log_error("fred", "collect_macro", str(exc))
            result["errors"].append("macro collection failed")

    return result


def intelligence_status(root: Path) -> dict[str, Any]:
    load_dotenv(root / ".env", override=True)
    config = load_intelligence_config(root)
    store = store_for(root)
    intelligence = config.get("intelligence", {})
    latest_scores = store.latest_rows("intelligence_scores", 10)
    latest_news = store.latest_rows("news_events", 5)
    latest_errors = store.latest_errors()
    market = store.latest_market_context()
    macro = macro_risk_score(store.latest_macro_observations())
    last_error = _error_summary(latest_errors[0]) if latest_errors else None
    return {
        "submitted": False,
        "enabled": bool(intelligence.get("enabled", True)),
        "news_provider_status": "enabled" if os.getenv("CRYPTOPANIC_API_KEY") else "disabled_missing_api_key",
        "macro_provider_status": "enabled" if os.getenv("FRED_API_KEY") else "disabled_missing_api_key",
        "market_context_provider_status": "enabled" if os.getenv("COINGECKO_API_KEY") else "disabled_missing_api_key",
        "provider_key_status": {
            "CRYPTOPANIC_API_KEY": "configured" if os.getenv("CRYPTOPANIC_API_KEY") else "missing",
            "COINGECKO_API_KEY": "configured" if os.getenv("COINGECKO_API_KEY") else "missing",
            "FRED_API_KEY": "configured" if os.getenv("FRED_API_KEY") else "missing",
        },
        "enabled_filters": {
            "news": bool(intelligence.get("use_news_filter", True)),
            "macro": bool(intelligence.get("use_macro_filter", True)),
            "market_context": bool(intelligence.get("use_market_context_filter", True)),
            "sentiment": bool(intelligence.get("use_sentiment_filter", True)),
        },
        "minimum_confidence_to_trade": intelligence.get("minimum_confidence_to_trade", 60),
        "macro_risk": macro,
        "market_context_status": _market_context_label(market, intelligence),
        "latest_score_count": len(latest_scores),
        "recent_news_count": len(latest_news),
        "last_successful_refresh": _last_successful_refresh(store),
        "last_error": last_error,
        "last_errors": [_error_summary(row) for row in latest_errors],
    }


def score_symbol(root: Path, symbol: str, micro_signal: str = "hold", micro_confidence: float = 0.0) -> dict[str, Any]:
    config = load_intelligence_config(root)
    intelligence = config.get("intelligence", {})
    store = store_for(root)
    if not intelligence.get("enabled", True):
        score = _score_payload(symbol, micro_signal, 50, None, None, None, 50, "allow", ["intelligence disabled"], ["intelligence"])
        store.save_score(score)
        return score

    missing: list[str] = []
    reasons: list[str] = [f"micro signal {micro_signal}"]
    micro_score = _micro_score(micro_signal, micro_confidence)
    sentiment = store.latest_coin_sentiment(symbol)
    severe_negative = False
    if sentiment:
        score_value = sentiment.get("score")
        news_score = float(score_value if score_value is not None else 50)
        reasons.append(f"news sentiment {sentiment.get('label')}")
        try:
            severe_negative = bool(json.loads(sentiment.get("details") or "{}").get("severe_negative"))
        except json.JSONDecodeError:
            severe_negative = False
    else:
        news_score = 50.0
        missing.append("news_sentiment")
        reasons.append("news sentiment missing")
    macro = macro_risk_score(store.latest_macro_observations())
    macro_score = float(macro["score"])
    reasons.extend(macro.get("reasons", []))
    if macro.get("missing_data"):
        missing.extend(macro["missing_data"])
    market = store.latest_market_context()
    market_score = _market_context_score(market, intelligence)
    if not market:
        missing.append("market_context")
        reasons.append("market context missing")
    else:
        reasons.append(_market_context_label(market, intelligence))

    weights = intelligence.get("scoring", {})
    weighted = (
        micro_score * float(weights.get("micro_signal_weight", 50))
        + news_score * float(weights.get("news_sentiment_weight", 20))
        + macro_score * float(weights.get("macro_risk_weight", 15))
        + market_score * float(weights.get("market_context_weight", 15))
    )
    denominator = sum(float(weights.get(key, 0)) for key in ("micro_signal_weight", "news_sentiment_weight", "macro_risk_weight", "market_context_weight")) or 100
    combined = round(weighted / denominator, 2)
    minimum = float(intelligence.get("minimum_confidence_to_trade", 60))
    recommendation = "allow" if combined >= minimum else "hold_only" if combined >= 40 else "block"

    severe_threshold = float(intelligence.get("news", {}).get("severe_negative_threshold", -80))
    if intelligence.get("news", {}).get("block_on_severe_negative_news", True) and (severe_negative or news_score <= max(0, 50 + severe_threshold / 2)):
        recommendation = "block"
        reasons.append("severe negative news filter")
    if intelligence.get("macro", {}).get("risk_off_blocks_new_entries", True) and macro_score < 40:
        recommendation = "block"
        reasons.append("macro risk-off filter")
    drawdown_limit = float(intelligence.get("market_context", {}).get("block_if_market_drawdown_24h_below_percent", -5))
    market_change = _market_value(market, "market_cap_change_24h_percent")
    if market_change is not None and market_change <= drawdown_limit:
        recommendation = "block"
        reasons.append("market drawdown filter")

    score = _score_payload(symbol, micro_signal, micro_score, news_score, macro_score, market_score, combined, recommendation, reasons, sorted(set(missing)))
    store.save_score(score)
    return score


def score_all_symbols(root: Path, symbols: list[str]) -> dict[str, Any]:
    scores = [score_symbol(root, symbol) for symbol in symbols]
    return {
        "submitted": False,
        "scores": scores,
        "allowed": [score["symbol"] for score in scores if score["recommendation"] == "allow"],
        "blocked": [score["symbol"] for score in scores if score["recommendation"] == "block"],
        "hold_only": [score["symbol"] for score in scores if score["recommendation"] == "hold_only"],
    }


def apply_intelligence_filter(root: Path, signal: TradeSignal) -> tuple[TradeSignal, dict[str, Any]]:
    if signal.side not in {"buy", "sell"}:
        return signal, {"applied": False, "reason": "not actionable"}
    score = score_symbol(root, signal.symbol, signal.strategy_signal, signal.confidence * 100 if signal.confidence <= 1 else signal.confidence)
    if signal.side == "buy" and score["recommendation"] in {"block", "hold_only"}:
        filtered = TradeSignal(
            symbol=signal.symbol,
            side="hold",
            confidence=signal.confidence,
            reason="intelligence_filter_blocked: " + "; ".join(score["reasons"]),
            stop_loss_percent=signal.stop_loss_percent,
            take_profit_percent=signal.take_profit_percent,
            strategy_signal=signal.strategy_signal,
            history_points=signal.history_points,
            current_mid=signal.current_mid,
            ema_20=signal.ema_20,
            ema_50=signal.ema_50,
            rsi_14=signal.rsi_14,
            momentum_5=signal.momentum_5,
            profile=signal.profile,
            buy_conditions_met=signal.buy_conditions_met,
            sell_conditions_met=signal.sell_conditions_met,
            conditions_failed=signal.conditions_failed,
            final_signal="hold",
        )
        return filtered, score
    return signal, score


def export_intelligence_report(root: Path, output: str | None = None) -> Path:
    store = store_for(root)
    status = intelligence_status(root)
    scores = store.latest_rows("intelligence_scores", 100)
    news = store.latest_rows("news_events", 50)
    target = Path(output) if output else root / "logs" / f"intelligence_report_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.json"
    if not target.is_absolute():
        target = root / target
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "submitted": False,
        "status": status,
        "top_positive_symbols": _symbols_by_score(scores, reverse=True),
        "top_negative_symbols": _symbols_by_score(scores, reverse=False),
        "news_events": news,
        "symbols_blocked_by_intelligence": [row.get("symbol") for row in scores if row.get("recommendation") == "block"],
        "symbols_allowed_by_intelligence": [row.get("symbol") for row in scores if row.get("recommendation") == "allow"],
        "missing_data": sorted({item for row in scores for item in _json_list(row.get("missing_data"))}),
    }
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target


def _score_payload(
    symbol: str,
    micro_signal: str,
    micro_score: float,
    news_score: float | None,
    macro_score: float | None,
    market_score: float | None,
    combined: float,
    recommendation: str,
    reasons: list[str],
    missing_data: list[str],
) -> dict[str, Any]:
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "symbol": symbol,
        "micro_signal": micro_signal,
        "micro_score": micro_score,
        "news_sentiment_score": news_score,
        "macro_risk_score": macro_score,
        "market_context_score": market_score,
        "combined_intelligence_score": combined,
        "recommendation": recommendation,
        "reasons": reasons,
        "missing_data": missing_data,
        "submitted": False,
    }


def _micro_score(micro_signal: str, micro_confidence: float) -> float:
    if micro_signal == "buy":
        return max(50.0, min(100.0, micro_confidence or 65.0))
    if micro_signal == "sell":
        return 45.0
    return 50.0


def _market_context_score(market: dict[str, Any], config: dict[str, Any]) -> float:
    if not market:
        return 50.0
    change = _market_value(market, "market_cap_change_24h_percent")
    score = 55.0
    if change is not None:
        score += max(-20.0, min(20.0, change * 4))
    return max(0.0, min(100.0, score))


def _market_context_label(market: dict[str, Any], config: dict[str, Any]) -> str:
    score = _market_context_score(market, config)
    if score >= 60:
        return "crypto market context positive"
    if score < 45:
        return "crypto market context negative"
    return "crypto market context neutral"


def _market_value(market: dict[str, Any], metric: str) -> float | None:
    row = market.get(metric)
    if not row:
        return None
    return _float_or_none(row.get("value"))


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _symbol_from_coingecko_id(coin_id: str, symbols: list[str]) -> str | None:
    reverse = {value: key for key, value in COINGECKO_IDS.items()}
    symbol = reverse.get(coin_id)
    return symbol if symbol in symbols else None


def _error_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {"timestamp": row.get("timestamp"), "provider": row.get("provider"), "context": row.get("context"), "message": row.get("message")}


def _last_successful_refresh(store: IntelligenceStore) -> str | None:
    timestamps: list[str] = []
    for table in ("news_events", "coin_sentiment", "macro_observations", "market_context", "intelligence_scores"):
        rows = store.latest_rows(table, 1)
        if rows and rows[0].get("timestamp"):
            timestamps.append(str(rows[0]["timestamp"]))
    return max(timestamps) if timestamps else None


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except json.JSONDecodeError:
            return []
    return []


def _symbols_by_score(rows: list[dict[str, Any]], reverse: bool) -> list[str]:
    def score(row: dict[str, Any]) -> float:
        return float(row.get("combined_intelligence_score", 0) or 0)

    return [str(row.get("symbol")) for row in sorted(rows, key=score, reverse=reverse)[:10] if row.get("symbol")]
