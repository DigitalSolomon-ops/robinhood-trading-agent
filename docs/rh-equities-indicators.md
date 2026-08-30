# Massive indicators as signal inputs (equities lane)

**Status:** implemented. Config: `config/strategy.yaml` → `equity_indicators:`.
Code: `src/equity_intelligence/indicator_signals.py`, consumed by
`StrategyEngine.generate_equity_signal`, wired in `src/equity_runtime.py`.

## What this is

The equities lane's rules-based profile (`equity_profiles.equities_core`)
decides buy / sell / hold from connector price history. On top of that verdict,
the lane reads four EOD technical indicators from the read-only Massive client
and uses them to **modulate** the signal:

| Input | Source | Used as |
|---|---|---|
| SMA50 vs SMA200 (or EMA, config-selectable) | `MassiveClient.get_sma` / `get_ema` | trend filter |
| RSI(14) | `MassiveClient.get_rsi` | oversold / overbought / strongly-overbought bands |
| MACD(12,26,9) value vs signal | `MassiveClient.get_macd` | bullish / bearish cross |

Every threshold, window and policy lives in `equity_indicators:`. Nothing is
hardcoded — `src/equity_intelligence/indicator_signals.py:DEFAULTS` is only the
fallback that the config section overrides key by key.

## What it can and cannot do

The modulation is **one-directional**, enforced in
`StrategyEngine.apply_indicator_modulation`:

* it may turn a rules **buy** into a **hold** (skip), or lower its confidence;
* it may **never** produce a `buy` or `sell` the rules did not already produce —
  a rules hold stays a hold no matter how bullish the readings;
* it **never** touches an **exit**. A rules `sell` is annotated with the
  indicator values and otherwise passed through untouched, so a Massive outage
  or a bad reading can never trap an open position;
* it never places an order. `indicator_signals.py` imports no broker, no
  `OrderManager`, no `RiskManager` and no kill switch, and
  `tests/test_equity_indicator_signals.py` pins that structurally over the
  module's AST.

Everything downstream is unchanged and still runs, in order: `RiskManager`
caps + allowlist, the equities kill switch (`STOP_TRADING_EQUITIES` +
`TRADING_ENABLED`), regular-hours enforcement, the long-only / PDT /
settlement guards, and the two human-gate flags (`dry_run` off **and**
`confirm_live_order` on). A perfect indicator reading changes none of them.

## Timing

Massive data here is **daily / EOD**. It is a signal-generation input only —
never an execution-timing or pricing input. The Robinhood connector remains the
sole source of execution-time price, exactly as in
[`rh-equities-binding.md`](rh-equities-binding.md).

## Rationale output

Every decision the indicators touch cites the values that drove it. A skipped
entry reads:

```
entry skipped by indicators: indicators[massive SMA50=90.00, SMA200=150.00,
RSI14=91.00, MACD=-1.00, MACD_signal=0.50, MACD_hist=-1.50] -> SMA50=90.00
below SMA200=150.00 -> downtrend, entry skipped; RSI14=91.00 at/above
strongly-overbought 80 -> entry skipped; MACD=-1.00 below signal=0.50
(hist -1.50) -> bearish cross, downweighted (x0.60)
| rules signal was 'buy' (ema20_above_ema50+rsi_between_35_and_70+momentum_5_positive)
```

That string is the signal's `reason`, so it lands in the audit log through the
existing paths (`equity_signal_skipped`, `paper_order_filled`,
`dry_run_order_preview`). `run_equity_cycle` additionally writes an
`equity_indicator_context` decision per symbol carrying the same values in
structured form.

## Config reference

```yaml
equity_indicators:
  enabled: true                # off by default in code; on in the shipped config
  require_indicators: false    # true = a buy with unreadable indicator data is SKIPPED
  max_confidence: 0.95
  min_confidence_to_act: 0.35  # a modulated confidence below this becomes a hold
  trend:
    series: sma                # sma | ema
    fast_window: 50
    slow_window: 200
    on_downtrend: skip         # skip | downweight
    downtrend_confidence_multiplier: 0.5
    uptrend_confidence_multiplier: 1.1
  rsi:
    window: 14
    oversold: 30
    overbought: 70
    strongly_overbought: 80
    on_overbought: downweight
    on_strongly_overbought: skip
    overbought_confidence_multiplier: 0.5
    oversold_confidence_multiplier: 1.15
  macd:
    short_window: 12
    long_window: 26
    signal_window: 9
    on_bearish_cross: downweight
    bearish_confidence_multiplier: 0.6
    bullish_confidence_multiplier: 1.1
```

With `enabled: false` (the code default) no provider is built, no second data
vendor is contacted, and the signal is exactly what it was before this feature
existed.

## Failure behaviour

A missing `MASSIVE_API_KEY`, a rate limit, or an HTTP error becomes an errored
snapshot, not an exception. By default the rules-based signal is passed through
unmodulated with `indicator data unavailable (...)` in its rationale. Set
`require_indicators: true` to fail closed instead — entries are skipped while
the feed is down. Exits are unaffected either way.

## Credentials

The Massive key is the only secret in this path and is resolved env-first
(`MASSIVE_API_KEY`), then Google Secret Manager (`massive-api`), per
`src/equity_intelligence/massive_client.py`. The Robinhood equities side has no
key at all — it is the OAuth connector.
