# News sentiment as a pre-trade risk filter (equities lane)

**Status:** implemented. Config: `config/trading_rules.yaml` →
`equities.news_sentiment:`. Code:
`src/equity_intelligence/news_sentiment.py`, consumed by
`StrategyEngine.generate_equity_signal`, wired in `src/equity_runtime.py`.

## What this is

A veto, and only a veto. Before an **entry**, the lane reads recent ticker news
and per-ticker sentiment from the read-only Massive client
(`MassiveClient.get_ticker_news`), counts the sentiment labels published inside
a configured recency window, and either leaves the entry alone, downweights it,
or skips it.

| Input | Source | Used as |
|---|---|---|
| Article `published_utc` | `/v2/reference/news` | the recency window filter |
| Per-ticker `insights[].sentiment` | same | the negative share (negatives ÷ rated) |
| `title` + `insights[].sentiment_reasoning` | same | the headline quoted in the rationale |

Two bands, both config-driven: at/above `strongly_negative_ratio` the entry is
skipped; at/above `negative_ratio` it is downweighted. Below
`min_rated_articles` in the window there is not enough coverage to act on, and
the filter says so and stands down rather than treating one bear note as a
consensus.

It lives in `trading_rules.yaml` rather than `strategy.yaml` because it is a
risk cap, not a signal-generation input — it sits with the account
confinement, the equities kill switch and the extended-hours flag.

## What it can and cannot do

The filter is **block-or-reduce only**, enforced in two places:

* `evaluate_news_sentiment` clamps every confidence multiplier to `[0.0, 1.0]`.
  A config that asks for 1.5 gets 1.0 and a note in the rationale saying it was
  clamped, so a misconfiguration cannot quietly turn a risk filter into a
  signal booster;
* `StrategyEngine.apply_sentiment_filter` may set `side` to `"hold"` and may
  only ever LOWER `confidence` (`min()` against the incoming value). It never
  sets `side` to `buy` or `sell`, so no headline — however glowing — can create
  an order the rules did not already ask for.

It also:

* **never touches an exit.** A rules `sell` is annotated with the sentiment and
  otherwise passed through untouched, so bad news can never trap an open
  position behind a news vendor's uptime;
* **never places an order.** `news_sentiment.py` imports no broker, no
  `OrderManager`, no `RiskManager` and no kill switch, and the only client
  method it can reach is `get_ticker_news`.
  `tests/test_equity_news_sentiment.py` pins that structurally over the
  module's AST;
* **never stands in for a risk gate.** Everything downstream is unchanged and
  still runs, in order: `RiskManager` caps + allowlist, the equities kill
  switch (`STOP_TRADING_EQUITIES` + `TRADING_ENABLED`), regular-hours
  enforcement, the long-only / PDT / settlement guards, and the two human-gate
  flags (`dry_run` off **and** `confirm_live_order` on). The cleanest news in
  the world changes none of them.

## Order of operations

`generate_equity_signal` runs three stages: the rules profile
(`equity_profiles.equities_core`), then the EOD indicator modulation
([`rh-equities-indicators.md`](rh-equities-indicators.md)), then this filter
**last**. Running last means it can veto anything upstream produced and nothing
upstream can undo its veto.

## Timing

News is published data with an unpredictable lag. It is a decision input only —
never an execution-timing or pricing input. The Robinhood connector remains the
sole source of execution-time price, exactly as in
[`rh-equities-binding.md`](rh-equities-binding.md).

## Rationale output

Every filtered decision cites the sentiment **and** the headline behind it. A
skipped entry reads:

```
entry skipped by news sentiment: news_sentiment[massive_news 3/4 rated articles
negative (75%) for AAPL in the last 48h; most recent negative: "Regulator opens
probe into flagship product" [negative, 2026-08-30T09:00:00Z, 3.0h ago]] ->
negative share 0.75 at/above strongly_negative_ratio 0.60 -> strongly negative,
entry skipped; vendor reasoning: an antitrust probe is a material overhang
| rules signal was 'buy' (ema20_above_ema50+rsi_between_35_and_70+momentum_5_positive)
```

A downweighted one keeps the entry and records the cost:

```
ema20_above_ema50+rsi_between_35_and_70+momentum_5_positive | news_sentiment[
massive_news 2/5 rated articles negative (40%) for AAPL in the last 48h; most
recent negative: "Q3 revenue misses consensus" [negative, 2026-08-30T07:00:00Z,
5.0h ago]] -> negative share 0.40 at/above negative_ratio 0.34 -> negative,
downweighted (x0.60); vendor reasoning: a top-line miss pressures the multiple
| confidence 0.60 -> 0.36
```

That string is the signal's `reason`, so it lands in the audit log through the
existing paths (`equity_signal_skipped`, `paper_order_filled`,
`dry_run_order_preview`). `run_equity_cycle` additionally writes an
`equity_sentiment_context` decision per symbol carrying the counts, the
negative share and the headline in structured form.

## Config reference

```yaml
equities:
  news_sentiment:
    enabled: true              # off by default in code; on in the shipped config
    require_news: false        # true = a buy with unreadable news is SKIPPED
    recency_hours: 48          # the window; anything older is not "recent sentiment"
    max_articles: 20
    min_rated_articles: 2      # below this, too little coverage to filter on
    negative_labels: [negative]
    positive_labels: [positive]
    strongly_negative_ratio: 0.6
    negative_ratio: 0.34
    on_strongly_negative: skip_entry   # skip_entry | downweight
    on_negative: downweight            # skip_entry | downweight
    strongly_negative_confidence_multiplier: 0.4
    negative_confidence_multiplier: 0.6
    min_confidence_to_act: 0.35        # a filtered confidence below this becomes a hold
```

Nothing is hardcoded — `src/equity_intelligence/news_sentiment.py:DEFAULTS` is
only the fallback the config section overrides key by key. With
`enabled: false` (the code default) no provider is built, no news vendor is
contacted, and the signal is exactly what it was before this feature existed.

**Why `skip_entry` and not `skip`:** `tests/test_order_symbol_guard.py` builds
its forbidden-ticker vocabulary by scanning `trading_rules.yaml` for
ticker-shaped strings under any equities-ish key. The bare word `skip` is
four letters and would be indistinguishable from a ticker, poisoning that
guard. The code accepts both spellings; the shipped config uses `skip_entry`.

## Failure behaviour

A missing `MASSIVE_API_KEY`, a rate limit, an HTTP error, or a provider that
raises becomes an errored snapshot, not an exception. By default the
rules-based signal is passed through unfiltered with `news sentiment
unavailable (...)` in its rationale — fail-open, because a news outage must not
silently halt the lane. Set `require_news: true` to fail closed instead: entries
are skipped while the feed is down, and also when coverage is below
`min_rated_articles`. Exits are unaffected either way.

## Credentials

The Massive key is the only secret in this path and is resolved env-first
(`MASSIVE_API_KEY`), then Google Secret Manager (`massive-api`), per
`src/equity_intelligence/massive_client.py`. The Robinhood equities side has no
key at all — it is the OAuth connector.
