# Sector Scout (analysis-only)

Sector-first options research on a ~six-month horizon, daily by email. It picks
the market segment first, then the leaders inside it, then a specific dated
options structure per play, with a calculated probability of profit and expected
value and a written strategy for each play. It NEVER trades: no code path in
`src/sector_scout/` can place, review, preview, or cancel an order, and the
container image copies no broker client at all. Every level is on the
underlying; every option figure is a real Massive per-contract snapshot captured
that run, timestamped in the report.

Sibling to the Options Scout (10 trading-day horizon, 19 single names). Same
email path, same analysis-only discipline, different question and a different
clock.

## What it does

1. **Two lenses over 34 funds** (11 GICS sector SPDRs + 23 industry/theme funds,
   benchmark SPY): *Extremes* (rank percentiles of price and fund/SPY relative
   strength, drawdown/run-up, trend confirm) classifies each fund Coiled /
   Falling knife / Extended / Leading and earning it / Mid range. *Continuation*
   (three pass/fail gates, then a score out of 8) finds "already up and likely
   to stay up". Acceleration = 3-month return annualised minus the 12-month
   return, exactly as validated 2026-09-04 (XOP/XLE 7/8, XBI 1/8 despite the
   biggest 12-month move).
2. **Calibration**: relative strength is the leading axis, absolute price
   percentile the secondary filter (near index highs almost nothing clears the
   absolute test). The board sorts by RS ascending and the report says so.
3. **Selection factors**: segment breadth (constituents above their own 200-day;
   below 40% demotes Extended/Continuation), IV rank (self-collected ATM IV
   history; buy premium under 30, credit spreads over 70), empirical base rate
   (same lens replayed over history, shrunk by sample size), correlation guard
   (pairs over 0.80 are ONE position), rate beta vs TLT (three or more selected
   funds over 0.5 are called one duration trade), short interest, term
   structure and 25-delta skew, seasonality. The last four are context only.
4. **Structures**: monthly expiry nearest 180 DTE (150-240 accepted, actual DTE
   always stated). Coiled: call debit spread, ~0.35 delta long leg.
   Continuation: call debit spread, ~0.60 delta long leg, short leg above the
   six-month measured move. Extended: put debit spread. Falling knife: NO
   structure, a falsifier instead. High IV rank re-expresses the same view as a
   credit spread.
5. **The order ticket**: exact contracts, bid/ask/mark/delta/IV/OI and spread as
   a percent of mark per leg, limit price (modeled fill, NOT the midpoint),
   midpoint and worst-case debit beside it, max loss/gain, R:R, breakeven and
   the move required, net delta, take-profit at 65% of max gain, roll-or-close
   date 45 days before expiry, and the standing execution rules.
6. **Probability, twice, always together**: Black-Scholes from the legs' live
   IV (computed locally, spread-level, EV integrates the intermediate region in
   closed form) NEXT TO the empirical base rate with its sample size. A gap
   above 15 points is flagged as a finding in the narrative.
7. **The change log** opens every report: classification moves with the gate
   that flipped, score moves with the component, falsifier triggers on live
   structures at the very top, and material ticket re-prices. Computed against
   the previous run's persisted table.
8. **Settlement**: every published structure is recorded and graded on whichever
   comes first: falsifier (LOSS), max-gain level touched (WIN), roll-or-close
   date or expiry (graded vs breakeven). The target-before-stop math reuses
   `scout_settlement.settlement.settle` (same conservative same-day rule); only
   the state is lane-owned, because a six-month loop cannot ride the 10-day day
   files. The running record prints in every email.

## Run it

Dry run (composes + writes the HTML, never opens an SMTP socket):

```powershell
cd agent
.venv\Scripts\python.exe -m src.main sector-scout-email --dry-run --out scout.html
```

Send (operator address only):

```powershell
.venv\Scripts\python.exe -m src.main sector-scout-email
```

Flags: `--dry-run` (no SMTP), `--top N` (max segment plays), `--out PATH`
(write the HTML body). Container entrypoint: `python -m src.sector_scout` with
`SECTOR_SCOUT_DRY_RUN=1` / `SECTOR_SCOUT_TOP=N`.

A full run takes ~20-30 minutes: the stock-side entitlement is ~5 requests per
minute and the client spaces calls at `MASSIVE_MIN_INTERVAL_SECONDS` (13s
default) rather than tripping 429 backoff.

## Credentials (house env-first-then-Secret-Manager pattern)

- **Massive key**: `MASSIVE_API_KEY`, else Secret Manager `massive-api`.
- **Gmail app password**: `GMAIL_APP_PASSWORD`, else Secret Manager
  `gmail-app-password` (canonical resolver imported from
  `options_scout.config`; never forked, never logged).
- **From/To**: `GMAIL_USER` / `SECTOR_SCOUT_TO`, default
  `digitalsolomon.com@gmail.com`. **The recipient is the operator only** --
  this lane deliberately does not read the report-recipients list.
- **Finnhub** (optional): `FINNHUB_API_KEY` for the earnings calendar; absent
  key degrades to a note, never an error.
- Local pip note: the venv's `pip.exe` shim is broken on this machine; use
  `.venv\Scripts\python.exe -m pip ...`.

## Data entitlements (verified live 2026-09-04, design constraints)

- **Stocks side**: ~5 requests/minute; **history capped at ~2 years** (first
  available daily bar sits exactly 2y back). All "five-year" percentiles are
  computed over the ACTUAL window and labeled with it. Aggregates paginate
  (`next_url`); the data layer follows pages.
- **`/stocks/financials/v1/ratios`: 403 NOT_AUTHORIZED.** The valuation overlay
  is therefore cross-sectional: median trailing P/E of each fund's confirmed
  leaders (quarterly EPS x4), against the universe median, labeled a proxy.
- **`/stocks/v1/short-interest`: authorized.** Days-to-cover + direction; the
  percent-of-float framing is NOT derivable (no float field) and is not shown.
- **Forward estimates: not on the plan.** Earnings direction falls back to the
  trailing signal; the method section says so (factor 7 stays context).
- **Options side (paid plan)**: per-contract snapshots with premium, OI,
  greeks, IV; unthrottled. Greeks/IV are None off market hours; a leg without a
  live quote makes the structure read n/a rather than inventing a number.
- **No historical IV on the plan**: IV rank builds from the lane's own daily
  ATM IV collection (`data/sector_scout/iv_history.json`). Below 60 collected
  days the report shows "collecting (N/252)" and structure choice falls back to
  classification alone.
- **Breadth cache**: one grouped-daily call returns the whole market for one
  day; per-day extracts persist under `data/sector_scout/breadth/`. Backfill is
  bounded (40 days/run), so constituent breadth reaches full coverage across
  ~7 runs and the report states partial coverage until then.

## Factor discipline (the ledger)

A factor carries scoring weight ONLY once the backtest shows it improves the
hit rate on a sample above `min_occurrences`; until then it ships as reported
context with zero weight (`factor_weights_earned` in the config).

| Factor | Status |
|---|---|
| 1 Breadth | context, demotion rule active; **no scoring weight earned yet** |
| 2 IV rank | routes structure type; no scoring weight |
| 3 Base rate | reported beside Black-Scholes; feeds ranking confidence only |
| 4 Correlation guard | position-collapse rule active; not a score |
| 5 Rate beta | context + shared-exposure call-out |
| 6 Short interest | context only |
| 7 Earnings revisions | trailing fallback, context only |
| 8 Term structure / skew | context only |
| 9 Seasonality | context only (n <= window years) |

Nothing is promoted without a settled-sample backtest; record any promotion
here with the run date and the sample it earned it on.

**Operator decree (2026-09-04), recorded distinctly from earned weights:** the
Top 9 opportunity score (CLASS/RS/PRICE/3M/12M/CONT/IV RANK/BETA, thesis-fit,
0 to 100) ranks the report's plays by operator direction. It is a transparent
presentation heuristic with every weight in `opportunity:` in the yaml and the
breakdown printed beside every score; it has NOT passed the backtest bar and
is not part of the factor-scoring machinery above. If settlement data later
shows the score discriminates winners, promote it here with the sample.

## The fill model (read before quoting the limit)

Massive has no broker high-fill-rate field. The ticket's limit price is a
MODEL: mark plus (long leg) / minus (short leg) 40% of that leg's half-spread,
rounded to $0.05. The midpoint and the worst-case debit (long ask minus short
bid) print beside it so the cost of certainty is visible.

## Deploy notes (operator-gated -- NOT done here)

1. Secrets `massive-api` and `gmail-app-password` already exist in
   `digitalsolomon-creator`; `finnhub` is optional and wired conditionally.
2. `bash deploy/deploy-sector-scout.sh` from `agent/`: builds
   `Dockerfile.sector-scout` via Cloud Build, deploys Cloud Run job
   `sector-scout` (runtime SA `options-scout-sa`, reused per house pattern;
   `DS_VAULT_NO_GCLOUD=1`,
   `MASSIVE_MIN_INTERVAL_SECONDS=13`, 1800s timeout), schedules
   `sector-scout-daily` at **6:00 ET weekdays, pre-market** (operator
   decision 2026-09-04). Accepted trade-off: option bid/ask is dark outside
   market hours (verified live), so the daily report carries the full
   board/lens/probability read with prev-close premiums while ticket quotes
   read n/a; run `sector-scout-email` manually during market hours whenever
   a fillable ticket is wanted.
3. Verify by reading the delivered email in the inbox (DOCX attached, HTML
   renders on a phone), not from a log line.

## Methodology honesty (read before trusting a number)

- The live thesis and the historical replay call the SAME lens functions over
  the SAME series; there is no separate, kinder rule for the backtest.
- Indicators and probabilities are computed locally, never fetched from vendor
  indicator endpoints, so the live rule and any backtested rule cannot drift.
- Every hit-rate travels with its sample size, shrunk by
  `occurrences / (occurrences + min_occurrences)`; below 10 occurrences it is
  labeled LOW CONFIDENCE. The historical replay classifies on price/RS
  structure alone (valuation is unknowable historically on this plan), which
  can only loosen the match, never invent hits.
- A missing quote, chain, or ratio renders n/a and the method section says
  why. A fabricated option quote is the worst failure this lane can produce.
- Educational framing throughout; the disclaimer is verbatim and prominent.
  Nothing in this lane places, reviews, previews, or cancels any order.

## Relationship to the Options Scout

Options Scout answers "what is set up today across 19 liquid single names, on
a 10 trading-day horizon". Sector Scout answers "which industry should I be in
for the next six months, who leads it, and what is the ideal contract". Both
share the branding module, the secret resolvers, the settlement math, and the
honesty rules. Neither modifies the other.

## The Robinhood snapshot handoff (deployed 2026-09-07)

The connector that carries Robinhood equities/options data is session-bound
to a Claude agent (docs/rh-equities-binding.md), so the cloud job never calls
Robinhood. The wiring is a producer/consumer handoff over the state store:

1. **Producer** -- the scheduled agent task `sector-scout-rh-snapshot-fill`
   (local machine, weekdays ~12:07 ET, runs while the Claude desktop app is
   open) services the manifest from `sector-scout-manifest` via the connector,
   assembles the snapshot JSON (schema in `robinhood_source.py`, fields
   verbatim), and publishes it with
   `python -m src.main sector-scout-snapshot-push --snapshot <file>`.
   The push validates the schema first, refuses NaN/Infinity, refuses a blob
   older than the one stored (`--force` overrides), and exits nonzero unless
   the write reached GCS. Auth: `GOOGLE_APPLICATION_CREDENTIALS` in `agent/.env`
   points at the local-runner-sa key (needs `roles/storage.objectAdmin` on the
   entry-alerts bucket -- objectCreator alone cannot overwrite the blob).
2. **Consumer** -- the Cloud Run job (`0 13 * * 1-5` America/New_York) calls
   `run_sector_scout_email` with no snapshot path; the runner pulls
   `sector-scout/rh_snapshot.json` from the store (fresher of GCS and local),
   schema-checks it, and passes it to the analyzer. Missing, stale (>20h,
   `robinhood.snapshot_max_age_hours`), or malformed blobs degrade to the
   Massive-only path with the age/absence stated in the report -- the email
   never crashes on a bad blob and never presents stale fields as live.

Freshness math: 13:00 ET run minus the 20h cap means any push after 17:00 ET
the previous day counts as fresh; the 12:07 ET same-morning fill leaves ~50
minutes of slack. A skipped fill (machine off, connector absent) is a designed
degradation, not an incident.
