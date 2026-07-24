# Digital Solomon Crypto Agent

Local, rules-based Robinhood Crypto Trading API agent. It is designed to execute only predefined strategy signals that pass a strict risk manager. It is not an AI trader and should not be treated as investment advice.

Crypto trading can lose money quickly. Keep live trading disabled until you have reviewed the code, tested paper behavior, and confirmed the official Robinhood API details for your account.

## Safety Defaults

- Default mode is `TRADING_MODE=paper`.
- `.env.example` sets `TRADING_ENABLED=false`.
- `config/trading_rules.yaml` sets `trading.enabled: false`.
- `python -m src.main run-live` refuses to start unless `.env` explicitly contains `TRADING_MODE=live`.
- Live submissions require `TRADING_ENABLED=true`, no `STOP_TRADING` file, valid API credentials, and `config/trading_rules.yaml` with `trading.enabled: true` and `trading.mode: live`.

## Install

```console
cd ~/Desktop/digital-solomon-crypto-agent
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m src.main init
```

On macOS or Linux, activate with `source .venv/bin/activate`.

## Create Robinhood Crypto API Credentials

Robinhood's official Crypto Trading API docs say credentials are created from crypto account settings on web classic:

1. Open Robinhood web classic.
2. Go to crypto account settings: https://robinhood.com/account/crypto
3. Create Crypto Trading API credentials.
4. Store the API key as `ROBINHOOD_API_KEY`.
5. Store your base64 Ed25519 private key as `ROBINHOOD_PRIVATE_KEY`.

Authenticated requests use signed headers:

- `x-api-key`
- `x-signature`
- `x-timestamp`

The implemented signature follows Robinhood's documented message format:

```text
{api_key}{timestamp}{path}{method}{body}
```

Do not use a Robinhood username or password with this project.

## Configure `.env`

```console
copy .env.example .env
```

Then edit `.env`:

```dotenv
ROBINHOOD_API_KEY=
ROBINHOOD_PRIVATE_KEY=
ROBINHOOD_BASE_URL=https://trading.robinhood.com
TRADING_MODE=paper
TRADING_ENABLED=false
MAX_DAILY_LOSS_USD=25
MAX_TRADE_AMOUNT_USD=25
MAX_OPEN_POSITIONS=2
POLL_INTERVAL_SECONDS=60
```

Leave `TRADING_ENABLED=false` until you intentionally want to allow rule-approved orders. With defaults, the bot logs decisions and risk blocks but places no orders.

## Run Paper Mode

```console
python -m src.main run-paper --once
```

To run continuously:

```console
python -m src.main run-paper
```

Paper trades, when enabled and approved, are stored in `data/paper_trades.db`. Decisions and risk blocks are stored in `data/trading_agent.db`.

## Review Logs

```console
python -m src.main status
```

For detailed inspection, open the SQLite databases:

```console
sqlite3 data/trading_agent.db "select timestamp, symbol, action, reason from decisions order by id desc limit 20;"
sqlite3 data/trading_agent.db "select timestamp, symbol, side, reason from risk_blocks order by id desc limit 20;"
sqlite3 data/paper_trades.db "select timestamp, symbol, side, quantity, price, status from paper_trades order by id desc limit 20;"
```

## Dry Run Mode

Dry-run mode prepares live order payloads but does not submit them:

```console
python -m src.main run-dry --once
```

Use this after paper testing and after adding valid Robinhood API credentials.

## Enable Live Trading

Only after paper and dry-run testing:

1. Set `.env`:

```dotenv
TRADING_MODE=live
TRADING_ENABLED=true
```

2. Edit `config/trading_rules.yaml`:

```yaml
trading:
  enabled: true
  mode: live
```

3. Ensure no `STOP_TRADING` file exists.
4. Confirm risk limits and allowed symbols.
5. Start with one cycle:

```console
python -m src.main run-live --once
```

## Stop Immediately

```console
python -m src.main stop
```

This creates `STOP_TRADING`. The risk manager checks the kill switch before every action and blocks new orders while the file exists. You can also set `TRADING_ENABLED=false` in `.env`.

## CLI

```console
python -m src.main init
python -m src.main run-paper
python -m src.main run-dry
python -m src.main run-live
python -m src.main status
python -m src.main stop
python -m src.main backtest
python -m src.main test-connection
python -m src.main preview-order BTC-USD buy 5
```

`test-connection` makes a read-only authenticated account request and never places an order.

`preview-order` fetches market data, estimates quantity, runs risk checks, prints the order payload, and never submits it.

## Robinhood Endpoint Notes To Confirm

The official docs page currently documents both v1 and v2 Crypto Trading API families. This project defaults the client to v2 because v2 order configs document `time_in_force` for limit orders and include fee-tier fields. Before enabling live mode, confirm in Robinhood's current docs for your account:

- Whether you should use v1 or v2.
- The exact available account cash field returned by `GET /api/v2/crypto/trading/accounts/`.
- Whether your account expects `quote_amount`, `asset_quantity`, or both for limit orders.
- The current fee treatment for estimated and submitted orders.
