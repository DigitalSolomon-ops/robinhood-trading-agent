#!/usr/bin/env bash
# VM startup script for Solomon Trader 007 (crypto.digitalsolomon.com).
#
# Idempotent: safe to re-run. Pulls app code from GCS, installs deps, refreshes
# Robinhood secrets from Secret Manager into .env (perms 600), and starts the
# dashboard as a systemd service. The bounded live-run unit and its timer are
# installed but left DISABLED. Arming trading is an operator decision and never
# a side effect of provisioning or redeploying.
#
# Persistent state lives on a separate disk mounted at $STATE_DIR and is
# symlinked into the app tree, so the app directory can be replaced wholesale
# on redeploy without touching databases.
set -euo pipefail

APP_HOME=/opt/solomon-trader
APP_DIR="$APP_HOME/app"
STATE_DIR="$APP_HOME/state"
VENV="$APP_HOME/venv"
CODE_BUCKET="${CODE_BUCKET:?CODE_BUCKET metadata attribute required}"   # gs://.../solomon-trader-app.tar.gz
SECRET_API_KEY="${SECRET_API_KEY:-robinhood-api-key}"
SECRET_PRIVATE_KEY="${SECRET_PRIVATE_KEY:-robinhood-private-key}"
PUBLIC_ORIGIN="${PUBLIC_ORIGIN:-https://crypto.digitalsolomon.com}"
# Trading posture for a FIRST boot only (no .env on disk yet). Defaults to
# paper. Set TRADING_POSTURE=live in instance metadata to provision the VM
# already armed -- an explicit operator choice made at instance-create time,
# never a default and never inherited from the repo config.
#
# Note what this does and does not do: it writes TRADING_MODE/TRADING_ENABLED.
# It does not enable the timer, and the bounded gate still refuses every symbol
# until `validate-symbols` has run on this VM. Arming is necessary for a live
# run; it is not sufficient, by design.
TRADING_POSTURE="${TRADING_POSTURE:-paper}"
case "$TRADING_POSTURE" in
  paper|live) ;;
  *) echo "TRADING_POSTURE must be 'paper' or 'live', got '$TRADING_POSTURE'"; exit 1 ;;
esac

# --- system deps ---
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip

# The image is expected to ship the Cloud SDK. Fail loudly and early rather
# than half way through a redeploy if it does not.
command -v gsutil >/dev/null || { echo "gsutil not on PATH"; exit 1; }
command -v gcloud >/dev/null || { echo "gcloud not on PATH"; exit 1; }

# --- app user + dirs ---
id solomon &>/dev/null || useradd --system --create-home --home-dir "$APP_HOME" --shell /usr/sbin/nologin solomon
mkdir -p "$APP_DIR" "$STATE_DIR/data"

# --- pull code from GCS ---
gsutil cp "$CODE_BUCKET" /tmp/app.tar.gz
rm -rf "$APP_DIR.new" && mkdir -p "$APP_DIR.new"
tar -xzf /tmp/app.tar.gz -C "$APP_DIR.new"

# Preserve operator-owned files across redeploys.
#
# config/ is preserved deliberately: the repo copy of trading_rules.yaml is the
# committed default, and restoring it over a running deployment would silently
# revert the operator's posture. Whatever is on disk wins.
if [ -f "$APP_DIR/.env" ]; then cp -a "$APP_DIR/.env" "$APP_DIR.new/.env"; fi
if [ -d "$APP_DIR/config" ]; then
  cp -a "$APP_DIR/config/." "$APP_DIR.new/config/"
fi

# data/ is a symlink into the state disk, never a real directory in the app
# tree. The tarball ships a tracked data/ (symbol_validation.json), so move its
# contents onto the state disk without overwriting live state, then replace the
# directory with the symlink. This also keeps the "rm -rf $APP_DIR" below off
# any mount point, which would otherwise abort the script under "set -e".
if [ -d "$APP_DIR.new/data" ] && [ ! -L "$APP_DIR.new/data" ]; then
  cp -an "$APP_DIR.new/data/." "$STATE_DIR/data/" 2>/dev/null || true
  rm -rf "$APP_DIR.new/data"
fi
ln -sfn "$STATE_DIR/data" "$APP_DIR.new/data"

rm -rf "$APP_DIR" && mv "$APP_DIR.new" "$APP_DIR"

# --- python venv ---
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
# Prefer the lock file so the VM runs the exact versions the suite passed
# against. run_project_tests() gates every live launch, so a silent dependency
# upgrade here is a trading outage waiting to happen.
if [ -f "$APP_DIR/requirements.lock.txt" ]; then
  "$VENV/bin/pip" install -r "$APP_DIR/requirements.lock.txt"
else
  "$VENV/bin/pip" install -r "$APP_DIR/requirements.txt"
fi

# --- secrets -> .env, refreshed on every boot ---
# Secret Manager is the source of truth. Writing only when .env is absent would
# mean a rotated credential never reaches the VM. Operator-owned runtime keys
# are carried forward from the existing file so refreshing secrets never
# changes trading posture.
read_existing() {
  local key="$1" default="$2" found=""
  if [ -f "$APP_DIR/.env" ]; then
    found=$(grep -E "^${key}=" "$APP_DIR/.env" | tail -n1 | cut -d= -f2- || true)
  fi
  if [ -n "$found" ]; then echo "$found"; else echo "$default"; fi
}

if [ "$TRADING_POSTURE" = "live" ]; then
  FIRST_BOOT_MODE=live
  FIRST_BOOT_ENABLED=true
else
  FIRST_BOOT_MODE=paper
  FIRST_BOOT_ENABLED=false
fi

# An existing .env always wins. TRADING_POSTURE seeds the first boot only, so a
# later redeploy can never re-arm (or disarm) a running deployment behind the
# operator's back.
TRADING_MODE=$(read_existing TRADING_MODE "$FIRST_BOOT_MODE")
TRADING_ENABLED=$(read_existing TRADING_ENABLED "$FIRST_BOOT_ENABLED")
POLL_INTERVAL_SECONDS=$(read_existing POLL_INTERVAL_SECONDS 60)

API_KEY=$(gcloud secrets versions access latest --secret="$SECRET_API_KEY")
PRIV_KEY=$(gcloud secrets versions access latest --secret="$SECRET_PRIVATE_KEY")

umask 077
# Risk caps are NOT set here. They live in config/trading_rules.yaml under
# risk:. No code reads MAX_* environment keys; writing them here only creates
# the impression that the caps are somewhere they are not.
cat > "$APP_DIR/.env" <<EOF
ROBINHOOD_API_KEY=$API_KEY
ROBINHOOD_PRIVATE_KEY=$PRIV_KEY
ROBINHOOD_BASE_URL=https://trading.robinhood.com
TRADING_MODE=$TRADING_MODE
TRADING_ENABLED=$TRADING_ENABLED
POLL_INTERVAL_SECONDS=$POLL_INTERVAL_SECONDS
PUBLIC_ORIGIN=$PUBLIC_ORIGIN
EOF
unset API_KEY PRIV_KEY

chown -R solomon:solomon "$APP_HOME" "$STATE_DIR"
chmod 600 "$APP_DIR/.env"

# --- systemd units ---
install -m 644 "$APP_DIR/deploy/solomon-trader-dashboard.service" /etc/systemd/system/solomon-trader-dashboard.service
install -m 644 "$APP_DIR/deploy/solomon-trader-loop.service" /etc/systemd/system/solomon-trader-loop.service
install -m 644 "$APP_DIR/deploy/solomon-trader-loop.timer" /etc/systemd/system/solomon-trader-loop.timer
systemctl daemon-reload

systemctl enable solomon-trader-dashboard
systemctl restart solomon-trader-dashboard

# The trading timer is installed but NOT enabled and NOT started. Enabling it
# is a Phase 6 operator action taken after a watched single bounded run.
systemctl disable solomon-trader-loop.timer 2>/dev/null || true

echo "startup.sh complete: dashboard on :8000, trading timer installed and disabled (TRADING_MODE=$TRADING_MODE)"
