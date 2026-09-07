# Solomon Trader — Robinhood trading & research agent

A rules-based trading and market-research system for Robinhood, in two halves
with deliberately different privileges:

- **Trading lanes** (crypto, equities, defined-risk options) execute only
  predefined strategy signals that pass a strict risk manager. Nothing here is
  an AI trader, and nothing here is investment advice.
- **Scout lanes** (research only, **no order path at all**) screen the market
  on cloud schedules and email a ranked, written research report each morning.

Trading can lose money quickly. Keep live trading disabled until you have
reviewed the code, tested paper behavior, and confirmed the current Robinhood
API details for your account.

## Safety architecture

The design goal is that a container which should not trade *cannot* trade, and
a lane that may trade must pass through independent brakes:

- **Analysis images are incapable of ordering.** The scout Dockerfiles use an
  explicit COPY allow-list — the broker/order code is physically absent from
  the image, not disabled by a flag (`deploy/../Dockerfile.sector-scout`).
- **Kill switch** (`src/kill_switch.py`): the presence of a `STOP_TRADING`
  file blocks new orders before every action. `python -m src.main stop`
  creates it.
- **Layered enables:** live submission requires `TRADING_MODE=live` in `.env`
  AND `TRADING_ENABLED=true` AND `config/trading_rules.yaml`
  `trading.enabled: true` + `trading.mode: live` AND no `STOP_TRADING` file.
  Default everywhere is paper/off.
- **Fail-closed options gates** (`src/option_risk_gates.py`): every options
  strategy maps to a required Robinhood approval level; an unknown or
  unreadable level refuses, never permits. Opening sells that can't be proven
  covered are classified unsupported.
- **Dry-run mode** (`run-dry`) prepares live order payloads and never submits.
- **Live-readiness gate** (`src/equity_readiness.py`): the equities lane's
  go/no-go — runs the full test suite and evaluates every gate against it
  before a live session is even considered.

## Lanes

Six scheduled Cloud Run Jobs plus an IAP-guarded dashboard service, GCP
project-scoped, all deployed from `deploy/` (one `cloudbuild.*.yaml` +
`deploy-*.sh` pair each):

| Lane | Schedule (ET) | Trades? | What it does |
|---|---|---|---|
| Sector Scout | 13:00 weekdays | never | Sector-first six-month options research: segment → leaders → dated spread per play with probability of profit, EV, and a written strategy. HTML email + DOCX. `docs/sector-scout.md` |
| Options Scout | 08:00 weekdays | never | Daily ranked candidate options plays across 19 liquid single names. `docs/options-scout.md` |
| Small-Cap Scout | 07:30 weekdays | never | The small-cap screen. `docs/smallcap-scout.md` |
| Entry Alerts | every 10 min, 9–16 weekdays | never | Intraday entry-condition watcher. |
| Scout Settlement | 18:00 weekdays | never | Scores predicted vs. realized; feeds `src/scout_calibration/`. |
| Options Trader | every 30 min, 9–16 weekdays | gated | The defined-risk options lane, behind every brake above. |

The spread model (`src/sector_scout/probability.py`) computes probability of
profit and expected value in closed form (Black–Scholes intermediate-region
integration, stdlib only); model numbers are published alongside empirical
base rates, and divergence past threshold is reported as a finding.

Rate-limit discipline against the Massive market-data API is deterministic
pacing, not reactive retry: a minimum request interval sized to the
entitlement, capped-exponential backoff that outlasts the 60s window, and a
task timeout raised because a throttled run legitimately takes 25–45 minutes
(`src/equity_intelligence/massive_client.py`).

## Install

```console
git clone <this repo> && cd agent
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m src.main init
```

On macOS or Linux, activate with `source .venv/bin/activate`.

## Robinhood Crypto API credentials

Robinhood's Crypto Trading API credentials are created from crypto account
settings on web classic (https://robinhood.com/account/crypto). Store the API
key as `ROBINHOOD_API_KEY` and the base64 Ed25519 private key as
`ROBINHOOD_PRIVATE_KEY`. Authenticated requests carry `x-api-key`,
`x-signature`, `x-timestamp`, signing the documented message format:

```text
{api_key}{timestamp}{path}{method}{body}
```

Do not use a Robinhood username or password with this project. Secrets resolve
env-first, then Google Secret Manager by name at runtime; nothing is logged or
written to disk.

## Configure `.env`

```console
copy .env.example .env
```

```dotenv
ROBINHOOD_API_KEY=
ROBINHOOD_PRIVATE_KEY=
ROBINHOOD_BASE_URL=https://trading.robinhood.com
TRADING_MODE=paper
TRADING_ENABLED=false
POLL_INTERVAL_SECONDS=60
```

Leave `TRADING_ENABLED=false` until you intentionally want to allow
rule-approved orders. With defaults, the bot logs decisions and risk blocks
and places no orders.

**Risk caps are not set here.** They live in `config/trading_rules.yaml` under
`risk:`. No code reads `MAX_DAILY_LOSS_USD` etc. from `.env`; they were
removed because they read as authoritative and are not.

When the dashboard runs behind Cloud IAP, `IAP_AUDIENCE` and `PUBLIC_ORIGIN`
switch on the edge guards in `src/web_security.py`. Neither is set locally.

## Run

```console
python -m src.main run-paper --once   # paper mode (default)
python -m src.main run-dry --once     # builds live payloads, never submits
python -m src.main status             # decisions, risk blocks, positions
python -m src.main stop               # creates STOP_TRADING (kill switch)
```

Paper trades land in `data/paper_trades.db`; decisions and risk blocks in
`data/trading_agent.db` (SQLite).

## Enable live trading

Only after paper and dry-run testing: set `TRADING_MODE=live` +
`TRADING_ENABLED=true` in `.env`, set `trading.enabled: true` +
`trading.mode: live` in `config/trading_rules.yaml`, ensure no `STOP_TRADING`
file, confirm risk limits and allowed symbols, then start with one cycle:
`python -m src.main run-live --once`.

## CLI

```console
python -m src.main init | run-paper | run-dry | run-live | status | stop
python -m src.main backtest | test-connection | preview-order BTC-USD buy 5
```

`test-connection` makes a read-only authenticated request and never places an
order. `preview-order` runs the full risk path and prints the payload without
submitting.

## Tests

```console
python -m pytest tests/ -q
```

1,100+ tests across ~70 files, covering the risk gates, signature scheme
(pinned to Robinhood's public docs test vector), lane logic, parsers, and the
readiness gates.

## Robinhood endpoint notes to confirm

The official docs currently describe both v1 and v2 Crypto Trading API
families; this client defaults to v2 (documented `time_in_force` on limit
orders, fee-tier fields). Before enabling live mode, confirm for your account:
v1 vs v2; the exact available-cash field on
`GET /api/v2/crypto/trading/accounts/`; whether limit orders expect
`quote_amount`, `asset_quantity`, or both; and current fee treatment. These
are open questions on purpose — the live path stays off until they are
confirmed.
