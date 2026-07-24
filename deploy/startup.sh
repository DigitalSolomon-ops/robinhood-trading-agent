#!/usr/bin/env bash
# VM startup script for Solomon Trader 007 (crypto.digitalsolomon.com).
# Idempotent: safe to re-run. Pulls app code from GCS, installs deps, loads
# Robinhood secrets from Secret Manager into .env (perms 600), and starts the
# dashboard as a systemd service. Trading stays in PAPER mode / disabled.
set -euo pipefail

APP_HOME=/opt/solomon-trader
APP_DIR="$APP_HOME/app"
VENV="$APP_HOME/venv"
CODE_BUCKET="${CODE_BUCKET:?CODE_BUCKET metadata attribute required}"   # gs://.../solomon-trader-app.tar.gz
SECRET_API_KEY="${SECRET_API_KEY:-robinhood-api-key}"
SECRET_PRIVATE_KEY="${SECRET_PRIVATE_KEY:-robinhood-private-key}"

# --- system deps ---
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip

# --- app user + dirs ---
id solomon &>/dev/null || useradd --system --create-home --home-dir "$APP_HOME" --shell /usr/sbin/nologin solomon
mkdir -p "$APP_DIR"

# --- pull code from GCS ---
gsutil cp "$CODE_BUCKET" /tmp/app.tar.gz
rm -rf "$APP_DIR.new" && mkdir -p "$APP_DIR.new"
tar -xzf /tmp/app.tar.gz -C "$APP_DIR.new"
# preserve persistent state (data/, .env) across redeploys
if [ -d "$APP_DIR/data" ]; then cp -a "$APP_DIR/data" "$APP_DIR.new/data"; fi
if [ -f "$APP_DIR/.env" ]; then cp -a "$APP_DIR/.env" "$APP_DIR.new/.env"; fi
rm -rf "$APP_DIR" && mv "$APP_DIR.new" "$APP_DIR"

# --- python venv ---
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$APP_DIR/requirements.txt"

# --- secrets -> .env (only create if missing, so state persists) ---
if [ ! -f "$APP_DIR/.env" ]; then
  API_KEY=$(gcloud secrets versions access latest --secret="$SECRET_API_KEY")
  PRIV_KEY=$(gcloud secrets versions access latest --secret="$SECRET_PRIVATE_KEY")
  cat > "$APP_DIR/.env" <<EOF
ROBINHOOD_API_KEY=$API_KEY
ROBINHOOD_PRIVATE_KEY=$PRIV_KEY
ROBINHOOD_BASE_URL=https://trading.robinhood.com
TRADING_MODE=paper
TRADING_ENABLED=false
MAX_DAILY_LOSS_USD=25
MAX_TRADE_AMOUNT_USD=25
MAX_OPEN_POSITIONS=2
POLL_INTERVAL_SECONDS=60
EOF
fi

mkdir -p "$APP_DIR/data"
chown -R solomon:solomon "$APP_HOME"
chmod 600 "$APP_DIR/.env"

# --- systemd service ---
install -m 644 "$APP_DIR/deploy/solomon-trader-dashboard.service" /etc/systemd/system/solomon-trader-dashboard.service
systemctl daemon-reload
systemctl enable solomon-trader-dashboard
systemctl restart solomon-trader-dashboard
echo "startup.sh complete: dashboard on :8000 (paper mode)"
